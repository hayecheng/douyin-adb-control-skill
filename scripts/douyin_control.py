#!/usr/bin/env python3

import argparse
import copy
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, TextIO, Tuple

from douyin_actions import ACTION_TYPES, ActionService, ActionStore, load_config
from douyin_core import ADBClient, ControlError, Device, ratio_to_pixel, select_device


DEVICE_ERROR_CODES = {
    "NO_DEVICE",
    "MULTIPLE_DEVICES",
    "DEVICE_UNAUTHORIZED",
    "DEVICE_OFFLINE",
}
OPERATION_ERROR_CODES = {
    "ADB_COMMAND_FAILED",
    "ADB_COMMAND_TIMEOUT",
    "SCREEN_SIZE_FAILED",
    "SCREENSHOT_FAILED",
    "UI_DUMP_FAILED",
    "PACKAGE_NOT_FOUND",
}


class ControlArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ControlError(
            "INVALID_ARGUMENT",
            "Command line arguments are invalid",
            "Run with --help to see supported commands and options",
        )


def success(command: str, data: Dict[str, Any]) -> Dict[str, Any]:
    return {"ok": True, "command": command, "data": data}


def failure(command: str, error: ControlError) -> Dict[str, Any]:
    return {"ok": False, "command": command, "error": error.as_dict()}


def _device_data(device: Device) -> Dict[str, Any]:
    return {
        "serial": device.serial,
        "state": device.state,
        "details": dict(device.details),
    }


def _exit_code(error: ControlError) -> int:
    if error.code in {"INVALID_ARGUMENT", "INVALID_COORDINATE"}:
        return 2
    if error.code == "INVALID_CONFIG":
        return 3
    if error.code == "ADB_NOT_FOUND":
        return 4
    if error.code in DEVICE_ERROR_CODES:
        return 5
    if error.code in OPERATION_ERROR_CODES:
        return 6
    if error.code.startswith("TOKEN_") or error.code.startswith("STATE_"):
        return 7
    if error.code in {
        "INVALID_ACTION",
        "UNSUPPORTED_TEXT_INPUT",
        "ACTION_LIMIT_EXCEEDED",
        "ACTION_TARGET_CHANGED",
        "ACTION_OUTCOME_UNKNOWN",
    }:
        return 7
    return 1


def _render_success(
    command: str,
    data: Dict[str, Any],
    json_mode: bool,
    stdout: TextIO,
) -> None:
    envelope = success(command, data)
    if json_mode:
        stdout.write(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))
        stdout.write("\n")
        return
    stdout.write("%s succeeded\n" % command)
    stdout.write(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))
    stdout.write("\n")


def _render_failure(
    command: str,
    error: ControlError,
    json_mode: bool,
    stdout: TextIO,
    stderr: TextIO,
) -> None:
    if json_mode:
        stdout.write(
            json.dumps(
                failure(command, error),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        stdout.write("\n")
        return
    stderr.write("ERROR [%s] %s\n" % (error.code, error.message))
    stderr.write("Hint: %s\n" % (error.hint or _fallback_hint(error)))


def _fallback_hint(error: ControlError) -> str:
    exit_code = _exit_code(error)
    hints = {
        2: "Run with --help and correct the command arguments",
        3: "Check the JSON configuration against schema version 1",
        4: "Install adb or provide its path with --adb",
        5: "Run devices, authorize one device, and select it with --serial",
        6: "Run doctor and retry after resolving the reported device capability",
        7: "Prepare a fresh action and use its exact unexpired token",
    }
    return hints.get(exit_code, "Run doctor for diagnostics and retry")


def _build_parser() -> argparse.ArgumentParser:
    parser = ControlArgumentParser(description="Control Douyin through Android ADB")
    parser.add_argument("--adb", default="adb", help="Path to the adb executable")
    parser.add_argument("--serial", help="ADB device serial")
    parser.add_argument("--config", help="Path to a JSON configuration file")
    parser.add_argument("--state-dir", help="Directory for pending action state")
    parser.add_argument("--json", action="store_true", help="Emit one JSON object")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="Run read-only environment diagnostics")
    subparsers.add_parser("devices", help="List connected Android devices")
    subparsers.add_parser("status", help="Inspect the selected Android device")
    screenshot = subparsers.add_parser("screenshot", help="Capture the Android screen")
    screenshot.add_argument("--output", required=True, help="Destination PNG path")
    ui_dump = subparsers.add_parser("ui-dump", help="Export the Android UI hierarchy")
    ui_dump.add_argument("--output", required=True, help="Destination XML path")
    swipe = subparsers.add_parser("swipe", help="Navigate the Douyin feed")
    swipe.add_argument("direction", choices=("next", "previous"))
    subparsers.add_parser("open", help="Launch the configured Douyin package")
    subparsers.add_parser("stop", help="Stop the configured Douyin package")
    prepare = subparsers.add_parser(
        "prepare-action",
        help="Prepare an inert confirmation-gated account action",
    )
    prepare.add_argument("--type", required=True, choices=ACTION_TYPES, dest="action_type")
    prepare.add_argument("--x", "--x-ratio", type=float, dest="x_ratio")
    prepare.add_argument("--y", "--y-ratio", type=float, dest="y_ratio")
    text_input = prepare.add_mutually_exclusive_group()
    text_input.add_argument("--text", help="Compatibility-only comment text input")
    text_input.add_argument(
        "--text-stdin",
        action="store_true",
        help="Read private comment text from standard input",
    )
    prepare.add_argument(
        "--token-output",
        help="Write the prepared token to a brand-new private file",
    )
    execute = subparsers.add_parser(
        "execute-action",
        help="Execute one previously prepared action token",
    )
    execute_token = execute.add_mutually_exclusive_group(required=True)
    execute_token.add_argument("--token", help="Compatibility-only token input")
    execute_token.add_argument(
        "--token-stdin",
        action="store_true",
        help="Read the action token from standard input",
    )
    execute_token.add_argument(
        "--token-file",
        help="Read and delete the action token from a private file",
    )
    cancel = subparsers.add_parser(
        "cancel-action",
        help="Cancel one previously prepared action token",
    )
    cancel_token = cancel.add_mutually_exclusive_group(required=True)
    cancel_token.add_argument("--token", help="Compatibility-only token input")
    cancel_token.add_argument(
        "--token-stdin",
        action="store_true",
        help="Read the action token from standard input",
    )
    cancel_token.add_argument(
        "--token-file",
        help="Read and delete the action token from a private file",
    )
    return parser


def _command_hint(argv: Optional[List[str]]) -> str:
    values = list(sys.argv[1:] if argv is None else argv)
    value_options = {
        "--adb",
        "--serial",
        "--config",
        "--state-dir",
        "--text",
        "--token",
        "--token-file",
        "--token-output",
    }
    skip_next = False
    for value in values:
        if skip_next:
            skip_next = False
            continue
        if value in value_options:
            skip_next = True
            continue
        if value.startswith("--"):
            continue
        return value
    return "unknown"


def _handle_devices(client: ADBClient) -> Dict[str, Any]:
    return {"devices": [_device_data(device) for device in client.devices()]}


def _selected_client(client: ADBClient) -> Tuple[Device, ADBClient]:
    device = client.selected_device()
    pinned = copy.copy(client)
    pinned.serial = device.serial
    return device, pinned


def _handle_status(client: ADBClient) -> Dict[str, Any]:
    device, pinned = _selected_client(client)
    width, height = pinned.screen_size()
    return {
        "device": _device_data(device),
        "screen": {"width": width, "height": height},
        "foreground_package": pinned.foreground_package(),
    }


def _absolute_output(value: str, code: str, message: str) -> Path:
    output = Path(value).expanduser()
    if not output.is_absolute():
        output = Path.cwd() / output
    output = output.resolve()
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ControlError(code, message) from error
    return output


def _handle_screenshot(arguments: argparse.Namespace, client: ADBClient) -> Dict[str, Any]:
    _device, pinned = _selected_client(client)
    output = _absolute_output(
        arguments.output,
        "SCREENSHOT_FAILED",
        "Unable to create the screenshot output directory",
    )
    path = pinned.screenshot(output)
    return {"path": str(Path(path).resolve())}


def _handle_ui_dump(arguments: argparse.Namespace, client: ADBClient) -> Dict[str, Any]:
    _device, pinned = _selected_client(client)
    output = _absolute_output(
        arguments.output,
        "UI_DUMP_FAILED",
        "Unable to create the UI dump output directory",
    )
    path = pinned.ui_dump(output)
    return {"path": str(Path(path).resolve())}


def _handle_swipe(
    arguments: argparse.Namespace,
    client: ADBClient,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    _device, pinned = _selected_client(client)
    width, height = pinned.screen_size()
    swipe = config["swipes"][arguments.direction]
    start = (
        ratio_to_pixel(float(swipe["start"][0]), width),
        ratio_to_pixel(float(swipe["start"][1]), height),
    )
    end = (
        ratio_to_pixel(float(swipe["end"][0]), width),
        ratio_to_pixel(float(swipe["end"][1]), height),
    )
    duration_ms = int(swipe["duration_ms"])
    pinned.swipe(start, end, duration_ms)
    return {
        "direction": arguments.direction,
        "start": {"x": start[0], "y": start[1]},
        "end": {"x": end[0], "y": end[1]},
        "duration_ms": duration_ms,
    }


def _handle_package(
    command: str,
    client: ADBClient,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    _device, pinned = _selected_client(client)
    package = config["package"]
    if not pinned.is_package_installed(package):
        raise ControlError(
            "PACKAGE_NOT_FOUND",
            "The configured Android package is not installed",
            "Install the configured package on the selected device",
        )
    if command == "open":
        pinned.launch_package(package)
        status = "launched"
    else:
        pinned.stop_package(package)
        status = "stopped"
    return {"package": package, "status": status}


def _check(
    name: str,
    ok: bool,
    message: str,
    **extra: Any,
) -> Dict[str, Any]:
    result = {"name": name, "ok": ok, "message": message}
    result.update(extra)
    return result


def _error_check(name: str, error: ControlError) -> Dict[str, Any]:
    result = _check(name, False, error.message, error_code=error.code)
    if error.hint:
        result["hint"] = error.hint
    return result


def _skipped_check(name: str, reason: str) -> Dict[str, Any]:
    return _check(name, False, "Check could not run: %s" % reason, skipped=True)


def _lock_check(state_dir: Path) -> Dict[str, Any]:
    if not state_dir.exists():
        return _check("state_locks", True, "No possible stale state locks were found", paths=[])
    try:
        paths = sorted(
            str(path.resolve())
            for path in state_dir.rglob("*.lock")
            if path.is_file()
        )
    except OSError as error:
        return _error_check(
            "state_locks",
            ControlError("STATE_ERROR", "Unable to inspect action state locks"),
        )
    if not paths:
        return _check("state_locks", True, "No possible stale state locks were found", paths=[])
    return _check(
        "state_locks",
        False,
        "Possible stale action state locks were found",
        paths=paths,
        hint=(
            "Remove these lock files only after confirming no controller process "
            "is running"
        ),
    )


def _handle_doctor(
    client: ADBClient,
    config: Dict[str, Any],
    state_dir: Path,
) -> Dict[str, Any]:
    checks = [
        _check(
            "python",
            sys.version_info >= (3, 9),
            "Python %d.%d.%d" % sys.version_info[:3],
        )
    ]
    devices = None
    try:
        devices = client.devices()
        checks.append(
            _check(
                "adb",
                True,
                "ADB is available and returned the device list",
                device_count=len(devices),
            )
        )
    except ControlError as error:
        checks.append(_error_check("adb", error))

    selected = None
    if devices is None:
        checks.append(_skipped_check("device", "ADB device listing failed"))
    else:
        try:
            selected = select_device(devices, client.serial)
            checks.append(
                _check(
                    "device",
                    True,
                    "An authorized Android device is selected",
                    device=_device_data(selected),
                )
            )
        except ControlError as error:
            checks.append(_error_check("device", error))

    if selected is None:
        checks.append(_skipped_check("package", "no usable device is selected"))
        checks.append(_skipped_check("screen", "no usable device is selected"))
        checks.append(_skipped_check("adb_keyboard", "no usable device is selected"))
    else:
        pinned = copy.copy(client)
        pinned.serial = selected.serial
        package = config["package"]
        try:
            installed = pinned.is_package_installed(package)
            checks.append(
                _check(
                    "package",
                    installed,
                    (
                        "Configured package is installed"
                        if installed
                        else "Configured package is not installed"
                    ),
                    package=package,
                    hint=(
                        None
                        if installed
                        else "Install the configured Douyin package on the selected device"
                    ),
                )
            )
        except ControlError as error:
            checks.append(_error_check("package", error))
        try:
            width, height = pinned.screen_size()
            checks.append(
                _check(
                    "screen",
                    True,
                    "Android screen size is available",
                    width=width,
                    height=height,
                )
            )
        except ControlError as error:
            checks.append(_error_check("screen", error))
        try:
            keyboard_status = pinned.adb_keyboard_status()
            keyboard_available = bool(keyboard_status["available"])
            if not keyboard_status["installed"]:
                keyboard_hint = "Install ADB Keyboard manually on the selected device"
            elif not keyboard_status["enabled"]:
                keyboard_hint = "Enable ADB Keyboard manually in Android input settings"
            elif not keyboard_status["selected"]:
                keyboard_hint = "Select ADB Keyboard manually as the current input method"
            else:
                keyboard_hint = None
            checks.append(
                _check(
                    "adb_keyboard",
                    True,
                    (
                        "ADB Keyboard is available for UTF-8 input"
                        if keyboard_available
                        else "ADB Keyboard is not operational for UTF-8 input"
                    ),
                    installed=bool(keyboard_status["installed"]),
                    enabled=bool(keyboard_status["enabled"]),
                    selected=bool(keyboard_status["selected"]),
                    available=keyboard_available,
                    component=keyboard_status["component"],
                    hint=keyboard_hint,
                )
            )
        except ControlError as error:
            checks.append(_error_check("adb_keyboard", error))

    checks.append(_lock_check(state_dir))
    return {"healthy": all(check["ok"] for check in checks), "checks": checks}


def _state_dir(arguments: argparse.Namespace) -> Path:
    value = arguments.state_dir or os.environ.get("DOUYIN_AGENT_STATE_DIR")
    path = Path(value).expanduser() if value else Path.cwd() / ".douyin-adb-control"
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def _invalid_private_input(message: str, hint: str) -> ControlError:
    return ControlError("INVALID_ARGUMENT", message, hint)


def _strip_one_trailing_line_ending(value: bytes) -> bytes:
    if value.endswith(b"\r\n"):
        return value[:-2]
    if value.endswith((b"\r", b"\n")):
        return value[:-1]
    return value


def _decode_bounded_private_input(
    value: bytes,
    *,
    limit: int,
    label: str,
) -> str:
    body = _strip_one_trailing_line_ending(value)
    if not body:
        raise _invalid_private_input(
            "%s input is empty" % label,
            "Provide the private value through exactly one supported input channel",
        )
    if len(body) > limit:
        raise _invalid_private_input(
            "%s input exceeds the %d-byte limit" % (label, limit),
            "Use the exact bounded private value",
        )
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _invalid_private_input(
            "%s input is not valid UTF-8" % label,
            "Provide valid UTF-8 input",
        ) from error


def _read_bounded_private_stdin(
    stdin: TextIO,
    *,
    limit: int,
    label: str,
) -> str:
    source = getattr(stdin, "buffer", stdin)
    try:
        value = source.read(limit + 3)
    except (OSError, UnicodeError) as error:
        raise _invalid_private_input(
            "Unable to read %s input" % label,
            "Provide the private value through standard input",
        ) from error
    if isinstance(value, str):
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise _invalid_private_input(
                "%s input is not valid UTF-8" % label,
                "Provide valid UTF-8 input",
            ) from error
    elif isinstance(value, bytes):
        encoded = value
    else:
        raise _invalid_private_input(
            "Unable to read %s input" % label,
            "Provide the private value through standard input",
        )
    return _decode_bounded_private_input(encoded, limit=limit, label=label)


def _private_input_path(value: str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(value)))


def _supports_posix_permission_bits() -> bool:
    return os.name == "posix"


def _has_broad_posix_permissions(file_status: os.stat_result) -> bool:
    return _supports_posix_permission_bits() and bool(
        stat.S_IMODE(file_status.st_mode) & 0o077
    )


def _validate_private_token_parent(path: Path) -> None:
    try:
        parent_status = os.lstat(path.parent)
    except OSError as error:
        raise _invalid_private_input(
            "Action token parent directory is unavailable",
            "Use an owner-private token directory",
        ) from error
    if stat.S_ISLNK(parent_status.st_mode) or not stat.S_ISDIR(parent_status.st_mode):
        raise _invalid_private_input(
            "Action token parent is not a private directory",
            "Use a non-symlink owner-private token directory",
        )
    if _has_broad_posix_permissions(parent_status):
        raise _invalid_private_input(
            "Action token parent directory permissions are too broad",
            "Restrict the token directory to owner permissions only (0700)",
        )


def _read_and_delete_private_token_file(value: str) -> str:
    path = _private_input_path(value)
    _validate_private_token_parent(path)
    try:
        path_status = os.lstat(path)
    except OSError as error:
        raise _invalid_private_input(
            "Action token file is unavailable",
            "Use a private regular token file with mode 0600",
        ) from error
    if stat.S_ISLNK(path_status.st_mode) or not stat.S_ISREG(path_status.st_mode):
        raise _invalid_private_input(
            "Action token file is not a regular file",
            "Use a private non-symlink regular token file",
        )
    if _has_broad_posix_permissions(path_status):
        raise _invalid_private_input(
            "Action token file permissions are too broad",
            "Restrict the token file to owner permissions only (0600)",
        )

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    opened_status = None
    try:
        descriptor = os.open(path, flags)
        opened_status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_status.st_mode)
            or _has_broad_posix_permissions(opened_status)
            or opened_status.st_dev != path_status.st_dev
            or opened_status.st_ino != path_status.st_ino
        ):
            raise OSError("token file changed during validation")
        chunks = []
        remaining = 65539
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
    except OSError as error:
        raise _invalid_private_input(
            "Unable to read action token file",
            "Use a private non-symlink regular token file with mode 0600",
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)

    try:
        current_status = os.lstat(path)
        if (
            opened_status is None
            or current_status.st_dev != opened_status.st_dev
            or current_status.st_ino != opened_status.st_ino
        ):
            raise OSError("token file changed before deletion")
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as error:
        raise _invalid_private_input(
            "Unable to delete action token file",
            "Use a removable private token file",
        ) from error
    return _decode_bounded_private_input(
        encoded,
        limit=65536,
        label="Action token",
    )


def _validate_private_text(value: str, *, limit: int, label: str) -> None:
    if not value:
        raise _invalid_private_input(
            "%s input is empty" % label,
            "Provide a non-empty private value",
        )
    try:
        length = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise _invalid_private_input(
            "%s input is not valid UTF-8" % label,
            "Provide valid UTF-8 input",
        ) from error
    if length > limit:
        raise _invalid_private_input(
            "%s input exceeds the %d-byte limit" % (label, limit),
            "Use the exact bounded private value",
        )


def _resolve_private_inputs(arguments: argparse.Namespace, stdin: TextIO) -> None:
    if arguments.command == "prepare-action":
        has_text = arguments.text is not None
        reads_text_stdin = bool(arguments.text_stdin)
        if arguments.action_type == "comment":
            if has_text == reads_text_stdin:
                raise _invalid_private_input(
                    "Comment actions require exactly one text input channel",
                    "Use --text-stdin (preferred) or compatibility-only --text",
                )
            if reads_text_stdin:
                arguments.text = _read_bounded_private_stdin(
                    stdin,
                    limit=4096,
                    label="Comment text",
                )
            else:
                _validate_private_text(
                    arguments.text,
                    limit=4096,
                    label="Comment text",
                )
        elif has_text or reads_text_stdin:
            raise _invalid_private_input(
                "Text input is only valid for comment actions",
                "Remove the text input or select the comment action type",
            )
        return

    if arguments.command not in {"execute-action", "cancel-action"}:
        return
    if arguments.token_stdin:
        arguments.token = _read_bounded_private_stdin(
            stdin,
            limit=65536,
            label="Action token",
        )
    elif arguments.token_file is not None:
        arguments.token = _read_and_delete_private_token_file(arguments.token_file)
    else:
        _validate_private_text(
            arguments.token,
            limit=65536,
            label="Action token",
        )


def _utf8_byte_length(text: str) -> int:
    try:
        return len(text.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise ControlError(
            "INVALID_ARGUMENT",
            "Comment text is not valid UTF-8",
            "Use valid Unicode text before preparing the action",
        ) from error


def _validate_input_bounds(arguments: argparse.Namespace) -> None:
    if arguments.command == "prepare-action" and arguments.action_type == "comment":
        _validate_private_text(
            arguments.text,
            limit=4096,
            label="Comment text",
        )
    if arguments.command in {"execute-action", "cancel-action"}:
        _validate_private_text(
            arguments.token,
            limit=65536,
            label="Action token",
        )


def _action_service(
    arguments: argparse.Namespace,
    client: ADBClient,
    config: Dict[str, Any],
) -> ActionService:
    store = ActionStore(
        _state_dir(arguments),
        token_ttl_seconds=float(config["token_ttl_seconds"]),
    )
    return ActionService(client, config, store)


def _private_token_output_path(value: str) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raw = Path.cwd() / raw
    if not raw.name:
        raise _invalid_private_input(
            "Action token output path is invalid",
            "Choose a new private token file path",
        )
    try:
        parent = raw.parent.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise _invalid_private_input(
            "Action token output path is invalid",
            "Choose a new private token file path",
        ) from error
    return parent / raw.name


def _create_private_parent_directories(parent: Path) -> None:
    missing = []
    current = parent
    while True:
        try:
            current_status = os.lstat(current)
        except FileNotFoundError:
            missing.append(current)
            if current.parent == current:
                raise _invalid_private_input(
                    "Unable to create action token output directory",
                    "Choose a writable private token file path",
                )
            current = current.parent
            continue
        except OSError as error:
            raise _invalid_private_input(
                "Unable to inspect action token output directory",
                "Choose a writable private token file path",
            ) from error
        if not stat.S_ISDIR(current_status.st_mode):
            raise _invalid_private_input(
                "Action token output parent is not a directory",
                "Choose a new private token file path",
            )
        break

    for directory in reversed(missing):
        try:
            os.mkdir(directory, 0o700)
            path_chmod = getattr(os, "chmod", None)
            if _supports_posix_permission_bits() and callable(path_chmod):
                path_chmod(directory, 0o700)
        except FileExistsError:
            try:
                directory_status = os.lstat(directory)
            except OSError as error:
                raise _invalid_private_input(
                    "Unable to inspect action token output directory",
                    "Choose a writable private token file path",
                ) from error
            if not stat.S_ISDIR(directory_status.st_mode):
                raise _invalid_private_input(
                    "Action token output parent is not a directory",
                    "Choose a new private token file path",
                )
        except OSError as error:
            raise _invalid_private_input(
                "Unable to create action token output directory",
                "Choose a writable private token file path",
            ) from error


def _set_private_token_output_mode(descriptor: int, output: Path) -> None:
    descriptor_chmod = getattr(os, "fchmod", None)
    if callable(descriptor_chmod):
        try:
            descriptor_chmod(descriptor, 0o600)
            return
        except NotImplementedError:
            pass
    path_chmod = getattr(os, "chmod", None)
    if callable(path_chmod):
        path_chmod(output, 0o600)


def _write_private_token_file(value: str, token: str) -> Path:
    output = _private_token_output_path(value)
    _create_private_parent_directories(output.parent)
    _validate_private_token_parent(output)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = None
    opened_status = None
    try:
        descriptor = os.open(output, flags, 0o600)
        opened_status = os.fstat(descriptor)
        if not stat.S_ISREG(opened_status.st_mode):
            raise OSError("token output is not a regular file")
        _set_private_token_output_mode(descriptor, output)
        encoded = token.encode("utf-8")
        offset = 0
        while offset < len(encoded):
            written = os.write(descriptor, encoded[offset:])
            if written <= 0:
                raise OSError("unable to write token output")
            offset += written
        os.fsync(descriptor)
        current_status = os.lstat(output)
        if (
            current_status.st_dev != opened_status.st_dev
            or current_status.st_ino != opened_status.st_ino
            or not stat.S_ISREG(current_status.st_mode)
        ):
            raise OSError("token output changed during write")
    except (OSError, UnicodeError, NotImplementedError) as error:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        if opened_status is not None:
            try:
                current_status = os.lstat(output)
                if (
                    current_status.st_dev == opened_status.st_dev
                    and current_status.st_ino == opened_status.st_ino
                ):
                    os.unlink(output)
            except OSError:
                pass
        raise _invalid_private_input(
            "Unable to create action token output file",
            "Choose a brand-new private file path; existing files are never overwritten",
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return output


def _handle_prepare_action(
    arguments: argparse.Namespace,
    service: ActionService,
) -> Dict[str, Any]:
    record = service.prepare(
        arguments.action_type,
        x_ratio=arguments.x_ratio,
        y_ratio=arguments.y_ratio,
        text=arguments.text,
    )
    summary = {
        "action_type": record["action_type"],
        "device_serial": record["device_serial"],
        "coordinate": record["coordinate"],
        "created_at": record["created_at"],
        "expires_at": record["expires_at"],
    }
    if record["action_type"] == "comment":
        summary["comment_digest"] = record["comment_digest"]
        summary["comment_length_bytes"] = _utf8_byte_length(arguments.text)
    if arguments.token_output is not None:
        token_path = _write_private_token_file(arguments.token_output, record["token"])
        return {"token_path": str(token_path), "summary": summary}
    return {"token": record["token"], "summary": summary}


def _handle_action(
    arguments: argparse.Namespace,
    client: ADBClient,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    service = _action_service(arguments, client, config)
    if arguments.command == "prepare-action":
        return _handle_prepare_action(arguments, service)
    if arguments.command == "execute-action":
        return service.execute(arguments.token)
    return service.cancel(arguments.token)


def _dispatch(
    arguments: argparse.Namespace,
    client: ADBClient,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    handlers = {
        "doctor": lambda: _handle_doctor(client, config, _state_dir(arguments)),
        "devices": lambda: _handle_devices(client),
        "status": lambda: _handle_status(client),
        "screenshot": lambda: _handle_screenshot(arguments, client),
        "ui-dump": lambda: _handle_ui_dump(arguments, client),
        "swipe": lambda: _handle_swipe(arguments, client, config),
        "open": lambda: _handle_package("open", client, config),
        "stop": lambda: _handle_package("stop", client, config),
        "prepare-action": lambda: _handle_action(arguments, client, config),
        "execute-action": lambda: _handle_action(arguments, client, config),
        "cancel-action": lambda: _handle_action(arguments, client, config),
    }
    return handlers[arguments.command]()


def main(
    argv: Optional[List[str]] = None,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    client_factory=ADBClient,
    stdin: TextIO = sys.stdin,
) -> int:
    parser = _build_parser()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if "--json" in raw_argv and any(value in {"-h", "--help"} for value in raw_argv):
        _render_success("help", {"usage": parser.format_help()}, True, stdout)
        return 0
    try:
        arguments = parser.parse_args(argv)
    except ControlError as error:
        command = _command_hint(argv)
        json_mode = "--json" in (sys.argv[1:] if argv is None else argv)
        _render_failure(command, error, json_mode, stdout, stderr)
        return _exit_code(error)
    command = arguments.command
    try:
        _resolve_private_inputs(arguments, stdin)
        _validate_input_bounds(arguments)
        config = load_config(Path(arguments.config) if arguments.config else None)
        client = client_factory(adb_path=arguments.adb, serial=arguments.serial)
        data = _dispatch(arguments, client, config)
    except ControlError as error:
        _render_failure(command, error, arguments.json, stdout, stderr)
        return _exit_code(error)
    _render_success(command, data, arguments.json, stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
