import base64
import math
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple


ADB_KEYBOARD_PACKAGE = "com.android.adbkeyboard"
ADB_KEYBOARD_COMPONENT = "com.android.adbkeyboard/.AdbIME"


class ControlError(Exception):
    def __init__(self, code: str, message: str, hint: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint

    def as_dict(self) -> Dict[str, str]:
        result = {"code": self.code, "message": self.message}
        if self.hint:
            result["hint"] = self.hint
        return result


@dataclass(frozen=True)
class CommandResult:
    command: List[str]
    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True)
class Device:
    serial: str
    state: str
    details: Dict[str, str]


def parse_devices(output: str) -> List[Device]:
    devices = []
    for line in output.splitlines():
        if "\t" not in line:
            continue
        serial, description = line.split("\t", 1)
        fields = description.split()
        if not serial or not fields:
            continue
        details = {}
        for field in fields[1:]:
            if ":" in field:
                key, value = field.split(":", 1)
                details[key] = value
        devices.append(Device(serial=serial, state=fields[0], details=details))
    return devices


def select_device(devices: List[Device], serial: Optional[str] = None) -> Device:
    if serial is not None:
        selected = next((device for device in devices if device.serial == serial), None)
        if selected is None:
            raise ControlError(
                "NO_DEVICE",
                "The requested Android device is not connected",
                "Run the devices command and choose a listed serial",
            )
        if selected.state == "unauthorized":
            raise ControlError(
                "DEVICE_UNAUTHORIZED",
                "The selected Android device is not authorized",
                "Accept the USB debugging prompt on the device",
            )
        if selected.state == "offline":
            raise ControlError(
                "DEVICE_OFFLINE",
                "The selected Android device is offline",
                "Reconnect the device and restart ADB if necessary",
            )
        if selected.state != "device":
            raise ControlError(
                "NO_DEVICE",
                "The selected Android device is not usable",
                "Reconnect the device and check its ADB state",
            )
        return selected

    usable = [device for device in devices if device.state == "device"]
    if not usable:
        raise ControlError(
            "NO_DEVICE",
            "No authorized Android device is connected",
            "Connect a device and accept the USB debugging prompt",
        )
    if len(usable) > 1:
        raise ControlError(
            "MULTIPLE_DEVICES",
            "Multiple authorized Android devices are connected",
            "Choose one device with --serial",
        )
    return usable[0]


def ratio_to_pixel(ratio: float, extent: int) -> int:
    if not 0.0 <= ratio <= 1.0 or extent <= 0:
        raise ControlError(
            "INVALID_COORDINATE",
            "Coordinate ratio or extent is invalid",
        )
    return min(extent - 1, max(0, round(ratio * extent)))


class ADBClient:
    def __init__(
        self,
        adb_path: str = "adb",
        serial: Optional[str] = None,
        runner: Callable = subprocess.run,
        timeout_seconds: float = 30.0,
    ):
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or float(timeout_seconds) <= 0.0
        ):
            raise ControlError(
                "INVALID_CONFIG", "ADB command timeout must be a positive number"
            )
        self.adb_path = adb_path
        self.serial = serial
        self.runner = runner
        self.timeout_seconds = float(timeout_seconds)

    def _run(
        self,
        command: List[str],
        error_code: str = "ADB_COMMAND_FAILED",
        error_message: str = "ADB device command failed",
    ) -> CommandResult:
        try:
            completed = self.runner(
                command,
                check=False,
                capture_output=True,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise ControlError(
                "ADB_COMMAND_TIMEOUT",
                "Android Debug Bridge command timed out",
                "Check the device connection and retry the command",
            ) from error
        except FileNotFoundError as error:
            raise ControlError(
                "ADB_NOT_FOUND",
                "Android Debug Bridge was not found",
                "Install adb or provide its path with --adb",
            ) from error
        except OSError as error:
            raise ControlError(
                "ADB_NOT_FOUND",
                "Android Debug Bridge could not be executed",
                "Install adb or provide its path with --adb",
            ) from error

        stdout = completed.stdout or b""
        stderr = completed.stderr or b""
        if isinstance(stdout, str):
            stdout = stdout.encode("utf-8")
        if isinstance(stderr, str):
            stderr = stderr.encode("utf-8")
        result = CommandResult(
            command=list(command),
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
        )
        if result.returncode != 0:
            raise ControlError(error_code, error_message)
        return result

    def _device_command(self, *arguments: str) -> List[str]:
        serial = self.serial
        if serial is None:
            serial = self.selected_device().serial
        return [self.adb_path, "-s", serial, *arguments]

    def devices(self) -> List[Device]:
        result = self._run(
            [self.adb_path, "devices", "-l"],
            error_message="Unable to list Android devices",
        )
        return parse_devices(result.stdout.decode("utf-8", errors="replace"))

    def selected_device(self) -> Device:
        return select_device(self.devices(), self.serial)

    def screen_size(self) -> Tuple[int, int]:
        result = self._run(
            self._device_command("shell", "wm", "size"),
            error_code="SCREEN_SIZE_FAILED",
            error_message="Unable to read the Android screen size",
        )
        output = result.stdout.decode("utf-8", errors="replace")
        match = re.search(r"Physical size:\s*(\d+)x(\d+)", output)
        if match is None:
            match = re.search(r"\b(\d+)x(\d+)\b", output)
        if match is None:
            raise ControlError(
                "SCREEN_SIZE_FAILED",
                "Unable to parse the Android screen size",
            )
        return int(match.group(1)), int(match.group(2))

    def foreground_package(self) -> Optional[str]:
        result = self._run(
            self._device_command("shell", "dumpsys", "window", "windows"),
            error_message="Unable to inspect the foreground Android package",
        )
        output = result.stdout.decode("utf-8", errors="replace")
        for line in output.splitlines():
            if "mCurrentFocus" not in line and "mFocusedApp" not in line:
                continue
            match = re.search(r"\bu\d+\s+([A-Za-z0-9_.$]+)/\S+", line)
            if match is not None:
                return match.group(1)
        return None

    def is_package_installed(self, package: str) -> bool:
        result = self._run(
            self._device_command("shell", "pm", "path", package),
            error_message="Unable to inspect the Android package",
        )
        return any(
            line.startswith("package:")
            for line in result.stdout.decode("utf-8", errors="replace").splitlines()
        )

    def adb_keyboard_status(self) -> Dict[str, object]:
        installed = self.is_package_installed(ADB_KEYBOARD_PACKAGE)
        enabled_result = self._run(
            self._device_command("shell", "ime", "list", "-s"),
            error_message="Unable to inspect enabled Android input methods",
        )
        enabled = ADB_KEYBOARD_COMPONENT in {
            line.strip()
            for line in enabled_result.stdout.decode(
                "utf-8", errors="replace"
            ).splitlines()
        }
        selected_result = self._run(
            self._device_command(
                "shell",
                "settings",
                "get",
                "secure",
                "default_input_method",
            ),
            error_message="Unable to inspect the selected Android input method",
        )
        selected = (
            selected_result.stdout.decode("utf-8", errors="replace").strip()
            == ADB_KEYBOARD_COMPONENT
        )
        return {
            "installed": installed,
            "enabled": enabled,
            "selected": selected,
            "available": installed and enabled and selected,
            "component": ADB_KEYBOARD_COMPONENT,
        }

    def screenshot(self, output: Path) -> Path:
        output = Path(output)
        result = self._run(
            self._device_command("exec-out", "screencap", "-p"),
            error_code="SCREENSHOT_FAILED",
            error_message="Unable to capture the Android screen",
        )
        try:
            output.write_bytes(result.stdout)
        except OSError as error:
            raise ControlError(
                "SCREENSHOT_FAILED",
                "Unable to save the Android screenshot",
            ) from error
        return output

    def launch_package(self, package: str) -> None:
        self._require_package_installed(package)
        self._run(
            self._device_command(
                "shell",
                "monkey",
                "-p",
                package,
                "-c",
                "android.intent.category.LAUNCHER",
                "1",
            ),
            error_message="Unable to launch the Android package",
        )

    def stop_package(self, package: str) -> None:
        self._require_package_installed(package)
        self._run(
            self._device_command("shell", "am", "force-stop", package),
            error_message="Unable to stop the Android package",
        )

    def _require_package_installed(self, package: str) -> None:
        if not self.is_package_installed(package):
            raise ControlError(
                "PACKAGE_NOT_FOUND",
                "The configured Android package is not installed",
                "Install the configured package on the selected device",
            )

    def ui_dump(self, output: Path) -> Path:
        output = Path(output)
        remote_path = "/sdcard/window_dump.xml"
        self._run(
            self._device_command("shell", "uiautomator", "dump", remote_path),
            error_code="UI_DUMP_FAILED",
            error_message="Unable to create an Android UI hierarchy dump",
        )
        result = self._run(
            self._device_command("exec-out", "cat", remote_path),
            error_code="UI_DUMP_FAILED",
            error_message="Unable to read the Android UI hierarchy dump",
        )
        try:
            output.write_bytes(result.stdout)
        except OSError as error:
            raise ControlError(
                "UI_DUMP_FAILED",
                "Unable to save the Android UI hierarchy dump",
            ) from error
        return output

    def swipe(
        self,
        start: Tuple[int, int],
        end: Tuple[int, int],
        duration_ms: int,
    ) -> None:
        self._run(
            self._device_command(
                "shell",
                "input",
                "swipe",
                str(start[0]),
                str(start[1]),
                str(end[0]),
                str(end[1]),
                str(duration_ms),
            )
        )

    def tap(self, x: int, y: int) -> None:
        self._run(
            self._device_command("shell", "input", "tap", str(x), str(y))
        )

    def input_text(self, text: str, backend: str) -> None:
        if backend == "auto":
            backend = "adb" if self._is_safe_adb_text(text) else "adb-keyboard"
        if backend == "adb":
            if not self._is_safe_adb_text(text):
                raise ControlError(
                    "UNSUPPORTED_TEXT_INPUT",
                    "Plain ADB text input contains unsupported characters",
                    "Use the adb-keyboard backend for UTF-8 text",
                )
            self._run(
                self._device_command(
                    "shell",
                    "input",
                    "text",
                    text.replace(" ", "%s"),
                )
            )
            return
        if backend == "adb-keyboard":
            status = self.adb_keyboard_status()
            if not status["available"]:
                raise ControlError(
                    "UNSUPPORTED_TEXT_INPUT",
                    "ADB Keyboard is not installed, enabled, and selected",
                    "Install, enable, and select ADB Keyboard manually before retrying",
                )
            encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
            self._run(
                self._device_command(
                    "shell",
                    "am",
                    "broadcast",
                    "-a",
                    "ADB_INPUT_B64",
                    "--es",
                    "msg",
                    encoded,
                )
            )
            return
        raise ControlError(
            "UNSUPPORTED_TEXT_INPUT",
            "The requested text input backend is not supported",
        )

    @staticmethod
    def _is_safe_adb_text(text: str) -> bool:
        return re.fullmatch(r"[A-Za-z0-9 .,_-]*", text) is not None
