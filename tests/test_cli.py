import base64
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets"
CONTROL_SCRIPT = SCRIPTS_DIR / "douyin_control.py"
sys.path.insert(0, str(SCRIPTS_DIR))

from douyin_control import main
from douyin_core import ControlError, Device


class FakeClient:
    def __init__(self):
        self.serial = "DEVICE-1"
        self.devices_result = [Device("DEVICE-1", "device", {"model": "Pixel"})]
        self.devices_error = None
        self.selected_error = None
        self.screen = (1080, 2400)
        self.screen_error = None
        self.foreground = "com.ss.android.ugc.aweme"
        self.foreground_error = None
        self.package_results = {
            "com.ss.android.ugc.aweme": True,
            "com.android.adbkeyboard": False,
        }
        self.package_errors = {}
        self.keyboard_status = {
            "installed": False,
            "enabled": False,
            "selected": False,
            "available": False,
            "component": "com.android.adbkeyboard/.AdbIME",
        }
        self.keyboard_error = None
        self.artifact_errors = {}
        self.calls = []
        self.writes = []

    def devices(self):
        self.calls.append("devices")
        if self.devices_error is not None:
            raise self.devices_error
        return self.devices_result

    def selected_device(self):
        self.calls.append(("selected_device", self.serial))
        if self.selected_error is not None:
            raise self.selected_error
        if self.serial is not None:
            for device in self.devices_result:
                if device.serial == self.serial:
                    return device
        return self.devices_result[0]

    def screen_size(self):
        self.calls.append(("screen_size", self.serial))
        if self.screen_error is not None:
            raise self.screen_error
        return self.screen

    def foreground_package(self):
        self.calls.append(("foreground_package", self.serial))
        if self.foreground_error is not None:
            raise self.foreground_error
        return self.foreground

    def is_package_installed(self, package):
        self.calls.append(("is_package_installed", self.serial, package))
        if package in self.package_errors:
            raise self.package_errors[package]
        return self.package_results.get(package, False)

    def adb_keyboard_status(self):
        self.calls.append(("adb_keyboard_status", self.serial))
        if self.keyboard_error is not None:
            raise self.keyboard_error
        return dict(self.keyboard_status)

    def screenshot(self, output):
        self.calls.append(("screenshot", self.serial, output))
        if "screenshot" in self.artifact_errors:
            raise self.artifact_errors["screenshot"]
        output.write_bytes(b"PNG")
        return output

    def ui_dump(self, output):
        self.calls.append(("ui_dump", self.serial, output))
        if "ui_dump" in self.artifact_errors:
            raise self.artifact_errors["ui_dump"]
        output.write_text("<hierarchy/>", encoding="utf-8")
        return output

    def swipe(self, start, end, duration_ms):
        self.writes.append(("swipe", self.serial, start, end, duration_ms))

    def launch_package(self, package):
        self.writes.append(("launch_package", self.serial, package))

    def stop_package(self, package):
        self.writes.append(("stop_package", self.serial, package))

    def tap(self, x, y):
        self.writes.append(("tap", self.serial, x, y))

    def input_text(self, text, backend):
        self.writes.append(("input_text", self.serial, text, backend))


class FakeFactory:
    def __init__(self, client):
        self.client = client
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        self.client.serial = kwargs["serial"]
        return self.client


def write_config(directory, mutate=None):
    config = json.loads((ASSETS_DIR / "config.example.json").read_text("utf-8"))
    if mutate is not None:
        mutate(config)
    path = Path(directory) / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


class ParserAndRendererTests(unittest.TestCase):
    def test_json_help_returns_one_success_envelope_without_system_exit(self):
        factory = FakeFactory(FakeClient())
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = main(
            ["--json", "--help"],
            stdout=stdout,
            stderr=stderr,
            client_factory=factory,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(len(stdout.getvalue().splitlines()), 1)
        envelope = json.loads(stdout.getvalue())
        self.assertEqual(envelope["ok"], True)
        self.assertEqual(envelope["command"], "help")
        self.assertIn("usage:", envelope["data"]["usage"])
        self.assertEqual(factory.calls, [])

    def test_real_json_help_is_machine_readable_and_non_json_help_stays_human(self):
        json_help = subprocess.run(
            [sys.executable, str(CONTROL_SCRIPT), "--json", "--help"],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(json_help.returncode, 0)
        self.assertEqual(json_help.stderr, "")
        self.assertEqual(len(json_help.stdout.splitlines()), 1)
        self.assertEqual(json.loads(json_help.stdout)["command"], "help")

        human_help = subprocess.run(
            [sys.executable, str(CONTROL_SCRIPT), "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(human_help.returncode, 0)
        self.assertEqual(human_help.stderr, "")
        self.assertIn("usage:", human_help.stdout)
        self.assertGreater(len(human_help.stdout.splitlines()), 1)

    def test_global_flags_before_devices_emit_one_json_success_envelope(self):
        client = FakeClient()
        factory = FakeFactory(client)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            exit_code = main(
                [
                    "--adb",
                    "custom-adb",
                    "--serial",
                    "DEVICE-1",
                    "--state-dir",
                    str(state_dir),
                    "--json",
                    "devices",
                ],
                stdout=stdout,
                stderr=stderr,
                client_factory=factory,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(stdout.getvalue().splitlines()), 1)
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {
                "ok": True,
                "command": "devices",
                "data": {
                    "devices": [
                        {
                            "serial": "DEVICE-1",
                            "state": "device",
                            "details": {"model": "Pixel"},
                        }
                    ]
                },
            },
        )
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            factory.calls,
            [{"adb_path": "custom-adb", "serial": "DEVICE-1"}],
        )

    def test_control_error_emits_stable_json_failure_and_device_exit_code(self):
        client = FakeClient()
        client.selected_error = ControlError(
            "NO_DEVICE",
            "No authorized Android device is connected",
            "Connect a device and accept the USB debugging prompt",
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = main(
            ["--json", "status"],
            stdout=stdout,
            stderr=stderr,
            client_factory=FakeFactory(client),
        )

        self.assertEqual(exit_code, 5)
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {
                "ok": False,
                "command": "status",
                "error": {
                    "code": "NO_DEVICE",
                    "message": "No authorized Android device is connected",
                    "hint": "Connect a device and accept the USB debugging prompt",
                },
            },
        )
        self.assertEqual(stderr.getvalue(), "")

    def test_human_failure_prints_actionable_hint_to_stderr(self):
        client = FakeClient()
        client.selected_error = ControlError(
            "DEVICE_UNAUTHORIZED",
            "The selected Android device is not authorized",
            "Accept the USB debugging prompt on the device",
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = main(
            ["--serial", "DEVICE-1", "status"],
            stdout=stdout,
            stderr=stderr,
            client_factory=FakeFactory(client),
        )

        self.assertEqual(exit_code, 5)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("DEVICE_UNAUTHORIZED", stderr.getvalue())
        self.assertIn("Hint: Accept the USB debugging prompt", stderr.getvalue())

    def test_invalid_subcommand_uses_stable_json_usage_error(self):
        factory = FakeFactory(FakeClient())
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = main(
            ["--json", "tap", "--x", "1", "--y", "2"],
            stdout=stdout,
            stderr=stderr,
            client_factory=factory,
        )

        self.assertEqual(exit_code, 2)
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {
                "ok": False,
                "command": "tap",
                "error": {
                    "code": "INVALID_ARGUMENT",
                    "message": "Command line arguments are invalid",
                    "hint": "Run with --help to see supported commands and options",
                },
            },
        )
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(factory.calls, [])

    def test_invalid_config_fails_before_creating_a_client(self):
        factory = FakeFactory(FakeClient())
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "bad.json"
            config.write_text("not-json", encoding="utf-8")
            exit_code = main(
                ["--config", str(config), "--json", "devices"],
                stdout=stdout,
                stderr=io.StringIO(),
                client_factory=factory,
            )

        self.assertEqual(exit_code, 3)
        self.assertEqual(json.loads(stdout.getvalue())["error"]["code"], "INVALID_CONFIG")
        self.assertEqual(factory.calls, [])

    def test_unsafe_package_config_fails_before_client_or_device_calls(self):
        for command in ("doctor", "open", "stop"):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as directory:
                config = write_config(
                    directory,
                    lambda value: value.update(package="com.example.app;input tap 1 1"),
                )
                client = FakeClient()
                factory = FakeFactory(client)
                stdout = io.StringIO()

                exit_code = main(
                    ["--config", str(config), "--json", command],
                    stdout=stdout,
                    stderr=io.StringIO(),
                    client_factory=factory,
                )

                self.assertEqual(exit_code, 3)
                self.assertEqual(
                    json.loads(stdout.getvalue())["error"]["code"],
                    "INVALID_CONFIG",
                )
                self.assertEqual(factory.calls, [])
                self.assertEqual(client.calls, [])
                self.assertEqual(client.writes, [])

    def test_missing_adb_uses_tool_exit_code(self):
        client = FakeClient()
        client.devices_error = ControlError(
            "ADB_NOT_FOUND",
            "Android Debug Bridge was not found",
            "Install adb or provide its path with --adb",
        )
        stdout = io.StringIO()

        exit_code = main(
            ["--json", "devices"],
            stdout=stdout,
            stderr=io.StringIO(),
            client_factory=FakeFactory(client),
        )

        self.assertEqual(exit_code, 4)
        self.assertEqual(json.loads(stdout.getvalue())["error"]["code"], "ADB_NOT_FOUND")

    def test_adb_timeout_uses_operation_exit_code_without_traceback(self):
        client = FakeClient()
        client.devices_error = ControlError(
            "ADB_COMMAND_TIMEOUT",
            "Android Debug Bridge command timed out",
        )
        stdout = io.StringIO()

        exit_code = main(
            ["--json", "devices"],
            stdout=stdout,
            stderr=io.StringIO(),
            client_factory=FakeFactory(client),
        )

        self.assertEqual(exit_code, 6)
        self.assertEqual(
            json.loads(stdout.getvalue())["error"]["code"],
            "ADB_COMMAND_TIMEOUT",
        )
        self.assertNotIn("Traceback", stdout.getvalue())

    def test_real_cli_directory_adb_path_emits_one_stable_json_failure(self):
        completed = subprocess.run(
            [sys.executable, str(CONTROL_SCRIPT), "--json", "--adb", "/", "devices"],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 4)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(len(completed.stdout.splitlines()), 1)
        envelope = json.loads(completed.stdout)
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["command"], "devices")
        self.assertEqual(envelope["error"]["code"], "ADB_NOT_FOUND")
        self.assertNotIn("Traceback", completed.stdout)


class ReadAndNavigationTests(unittest.TestCase):
    def test_status_pins_selected_device_and_returns_serializable_data(self):
        client = FakeClient()
        client.serial = None
        stdout = io.StringIO()

        exit_code = main(
            ["--json", "status"],
            stdout=stdout,
            stderr=io.StringIO(),
            client_factory=FakeFactory(client),
        )

        self.assertEqual(exit_code, 0)
        data = json.loads(stdout.getvalue())["data"]
        self.assertEqual(data["device"]["serial"], "DEVICE-1")
        self.assertEqual(data["screen"], {"width": 1080, "height": 2400})
        self.assertEqual(data["foreground_package"], "com.ss.android.ugc.aweme")
        self.assertIn(("screen_size", "DEVICE-1"), client.calls)
        self.assertIn(("foreground_package", "DEVICE-1"), client.calls)

    def test_screenshot_creates_parent_and_returns_absolute_path(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "nested" / "screen.png"
            stdout = io.StringIO()
            exit_code = main(
                ["--json", "screenshot", "--output", str(output)],
                stdout=stdout,
                stderr=io.StringIO(),
                client_factory=FakeFactory(client),
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(Path(json.loads(stdout.getvalue())["data"]["path"]), output.resolve())
            self.assertEqual(output.read_bytes(), b"PNG")

    def test_ui_dump_creates_parent_and_returns_absolute_path(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "nested" / "window.xml"
            stdout = io.StringIO()
            exit_code = main(
                ["--json", "ui-dump", "--output", str(output)],
                stdout=stdout,
                stderr=io.StringIO(),
                client_factory=FakeFactory(client),
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(Path(json.loads(stdout.getvalue())["data"]["path"]), output.resolve())
            self.assertEqual(output.read_text("utf-8"), "<hierarchy/>")

    def test_swipe_resolves_config_ratios_against_current_screen(self):
        cases = {
            "next": ((20, 160), (80, 40), 444),
            "previous": ((80, 40), (20, 160), 555),
        }
        for direction, expected in cases.items():
            with self.subTest(direction=direction), tempfile.TemporaryDirectory() as directory:
                def mutate(config):
                    config["swipes"]["next"] = {
                        "start": [0.2, 0.8], "end": [0.8, 0.2], "duration_ms": 444
                    }
                    config["swipes"]["previous"] = {
                        "start": [0.8, 0.2], "end": [0.2, 0.8], "duration_ms": 555
                    }

                config = write_config(directory, mutate)
                client = FakeClient()
                client.screen = (100, 200)
                exit_code = main(
                    ["--config", str(config), "--json", "swipe", direction],
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                    client_factory=FakeFactory(client),
                )

                self.assertEqual(exit_code, 0)
                self.assertEqual(
                    client.writes,
                    [("swipe", "DEVICE-1", expected[0], expected[1], expected[2])],
                )

    def test_open_and_stop_use_only_configured_package(self):
        for command, method in (("open", "launch_package"), ("stop", "stop_package")):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as directory:
                config = write_config(
                    directory,
                    lambda value: value.update(package="example.configured.package"),
                )
                client = FakeClient()
                client.package_results["example.configured.package"] = True
                exit_code = main(
                    ["--config", str(config), "--json", command],
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                    client_factory=FakeFactory(client),
                )

                self.assertEqual(exit_code, 0)
                self.assertEqual(
                    client.writes,
                    [(method, "DEVICE-1", "example.configured.package")],
                )

    def test_open_and_stop_report_missing_package_without_lifecycle_write(self):
        for command in ("open", "stop"):
            with self.subTest(command=command):
                client = FakeClient()
                client.package_results["com.ss.android.ugc.aweme"] = False
                stdout = io.StringIO()

                exit_code = main(
                    ["--json", command],
                    stdout=stdout,
                    stderr=io.StringIO(),
                    client_factory=FakeFactory(client),
                )

                self.assertEqual(exit_code, 6)
                self.assertEqual(
                    json.loads(stdout.getvalue())["error"]["code"],
                    "PACKAGE_NOT_FOUND",
                )
                self.assertEqual(client.writes, [])

    def test_doctor_aggregates_checks_and_only_reads_possible_stale_locks(self):
        client = FakeClient()
        client.screen_error = ControlError("SCREEN_SIZE_FAILED", "screen failed")
        client.package_results["com.ss.android.ugc.aweme"] = False
        client.keyboard_error = ControlError(
            "ADB_COMMAND_FAILED", "keyboard check failed"
        )
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            used_dir = state_dir / "used"
            used_dir.mkdir(parents=True)
            execution_lock = state_dir / ".execution.lock"
            token_lock = used_dir / "token.lock"
            execution_lock.write_text("keep", encoding="utf-8")
            token_lock.write_text("keep-too", encoding="utf-8")
            stdout = io.StringIO()

            exit_code = main(
                ["--state-dir", str(state_dir), "--json", "doctor"],
                stdout=stdout,
                stderr=io.StringIO(),
                client_factory=FakeFactory(client),
            )

            self.assertEqual(exit_code, 0)
            data = json.loads(stdout.getvalue())["data"]
            self.assertFalse(data["healthy"])
            checks = {check["name"]: check for check in data["checks"]}
            self.assertEqual(
                set(checks),
                {"python", "adb", "device", "package", "screen", "adb_keyboard", "state_locks"},
            )
            self.assertFalse(checks["package"]["ok"])
            self.assertFalse(checks["screen"]["ok"])
            self.assertFalse(checks["adb_keyboard"]["ok"])
            self.assertFalse(checks["state_locks"]["ok"])
            self.assertIn("confirming no controller process", checks["state_locks"]["hint"])
            self.assertEqual(execution_lock.read_text("utf-8"), "keep")
            self.assertEqual(token_lock.read_text("utf-8"), "keep-too")
            self.assertIn(("is_package_installed", "DEVICE-1", "com.ss.android.ugc.aweme"), client.calls)
            self.assertIn(("adb_keyboard_status", "DEVICE-1"), client.calls)

    def test_doctor_reports_adb_keyboard_operational_fields_and_selection_hint(self):
        client = FakeClient()
        client.keyboard_status.update(installed=True, enabled=True)
        stdout = io.StringIO()

        exit_code = main(
            ["--json", "doctor"],
            stdout=stdout,
            stderr=io.StringIO(),
            client_factory=FakeFactory(client),
        )

        self.assertEqual(exit_code, 0)
        checks = {
            check["name"]: check
            for check in json.loads(stdout.getvalue())["data"]["checks"]
        }
        keyboard = checks["adb_keyboard"]
        self.assertTrue(keyboard["ok"])
        self.assertEqual(
            {key: keyboard[key] for key in ("installed", "enabled", "selected", "available")},
            {"installed": True, "enabled": True, "selected": False, "available": False},
        )
        self.assertEqual(keyboard["component"], "com.android.adbkeyboard/.AdbIME")
        self.assertIn("select", keyboard["hint"].lower())
        self.assertTrue(json.loads(stdout.getvalue())["data"]["healthy"])

    def test_doctor_adb_keyboard_inactive_states_have_actionable_hints(self):
        cases = (
            ({}, "install"),
            ({"installed": True}, "enable"),
        )
        for status, hint_word in cases:
            with self.subTest(status=status):
                client = FakeClient()
                client.keyboard_status.update(status)
                stdout = io.StringIO()

                self.assertEqual(
                    main(
                        ["--json", "doctor"],
                        stdout=stdout,
                        stderr=io.StringIO(),
                        client_factory=FakeFactory(client),
                    ),
                    0,
                )

                keyboard = next(
                    check
                    for check in json.loads(stdout.getvalue())["data"]["checks"]
                    if check["name"] == "adb_keyboard"
                )
                self.assertTrue(keyboard["ok"])
                self.assertFalse(keyboard["available"])
                self.assertIn(hint_word, keyboard["hint"].lower())
                self.assertTrue(json.loads(stdout.getvalue())["data"]["healthy"])


class ActionCommandTests(unittest.TestCase):
    @staticmethod
    def _decode_token_payload(token):
        payload = token.split(".", 1)[0]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))

    def test_prepare_action_target_change_uses_action_policy_exit_code(self):
        client = FakeClient()
        client.foreground = "com.example.other"
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            stdout = io.StringIO()

            exit_code = main(
                ["--state-dir", str(state_dir), "--json", "prepare-action", "--type", "like"],
                stdout=stdout,
                stderr=io.StringIO(),
                client_factory=FakeFactory(client),
            )

            self.assertEqual(list((state_dir / "pending").glob("*.json")), [])
        self.assertEqual(exit_code, 7)
        self.assertEqual(
            json.loads(stdout.getvalue())["error"]["code"],
            "ACTION_TARGET_CHANGED",
        )

    def test_unknown_action_outcome_uses_action_policy_exit_code(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            prepared_stdout = io.StringIO()
            self.assertEqual(
                main(
                    [
                        "--state-dir",
                        str(state_dir),
                        "--json",
                        "prepare-action",
                        "--type",
                        "like",
                    ],
                    stdout=prepared_stdout,
                    stderr=io.StringIO(),
                    client_factory=FakeFactory(client),
                ),
                0,
            )
            token = json.loads(prepared_stdout.getvalue())["data"]["token"]

            def timeout_tap(x, y):
                client.writes.append(("tap", client.serial, x, y))
                raise ControlError(
                    "ADB_COMMAND_TIMEOUT",
                    "Android Debug Bridge command timed out",
                )

            client.tap = timeout_tap
            stdout = io.StringIO()
            exit_code = main(
                [
                    "--state-dir",
                    str(state_dir),
                    "--json",
                    "execute-action",
                    "--token",
                    token,
                ],
                stdout=stdout,
                stderr=io.StringIO(),
                client_factory=FakeFactory(client),
            )

            self.assertEqual(
                json.loads((state_dir / "counters.json").read_text("utf-8"))["like"],
                1,
            )

        self.assertEqual(exit_code, 7)
        envelope = json.loads(stdout.getvalue())
        self.assertEqual(envelope["error"]["code"], "ACTION_OUTCOME_UNKNOWN")
        self.assertIn("do not retry", envelope["error"]["hint"].lower())

    def test_prepare_action_returns_inert_user_displayable_summary(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            stdout = io.StringIO()
            exit_code = main(
                [
                    "--state-dir",
                    str(Path(directory) / "state"),
                    "--json",
                    "prepare-action",
                    "--type",
                    "tap",
                    "--x",
                    "0.1",
                    "--y",
                    "0.2",
                ],
                stdout=stdout,
                stderr=io.StringIO(),
                client_factory=FakeFactory(client),
            )

        self.assertEqual(exit_code, 0)
        data = json.loads(stdout.getvalue())["data"]
        self.assertEqual(set(data), {"token", "summary"})
        self.assertTrue(data["token"])
        self.assertEqual(data["summary"]["action_type"], "tap")
        self.assertEqual(
            data["summary"]["coordinate"]["normalized"],
            {"x": 0.1, "y": 0.2},
        )
        self.assertIn("expires_at", data["summary"])
        self.assertEqual(client.writes, [])

    def test_prepare_comment_accepts_injected_stdin_and_writes_private_token_file(self):
        comment_input = "first line\nsecond line\n\n"
        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            existing_parent = root / "existing"
            existing_parent.mkdir(mode=0o750)
            os.chmod(existing_parent, 0o750)
            token_file = existing_parent / "new-private-dir" / "action.token"
            stdout = io.StringIO()

            exit_code = main(
                [
                    "--state-dir",
                    str(root / "state"),
                    "--json",
                    "prepare-action",
                    "--type",
                    "comment",
                    "--text-stdin",
                    "--token-output",
                    str(token_file),
                ],
                stdin=io.StringIO(comment_input),
                stdout=stdout,
                stderr=io.StringIO(),
                client_factory=FakeFactory(client),
            )

            self.assertEqual(exit_code, 0)
            data = json.loads(stdout.getvalue())["data"]
            self.assertEqual(set(data), {"token_path", "summary"})
            self.assertEqual(Path(data["token_path"]), token_file.resolve())
            token = token_file.read_text("utf-8")
            self.assertEqual(
                self._decode_token_payload(token)["text"],
                "first line\nsecond line\n",
            )
            self.assertNotIn(token, stdout.getvalue())
            self.assertNotIn("first line", stdout.getvalue())
            self.assertEqual(stat.S_IMODE(token_file.stat().st_mode), 0o600)
            self.assertEqual(
                stat.S_IMODE(token_file.parent.stat().st_mode),
                0o700,
            )
            self.assertEqual(stat.S_IMODE(existing_parent.stat().st_mode), 0o750)

    def test_prepare_token_output_keeps_raw_token_off_human_and_json_stdout(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for json_mode in (False, True):
                with self.subTest(json_mode=json_mode):
                    token_file = root / ("json.token" if json_mode else "human.token")
                    stdout = io.StringIO()
                    argv = ["--state-dir", str(root / "state")]
                    if json_mode:
                        argv.append("--json")
                    argv.extend(
                        [
                            "prepare-action",
                            "--type",
                            "like",
                            "--token-output",
                            str(token_file),
                        ]
                    )

                    self.assertEqual(
                        main(
                            argv,
                            stdout=stdout,
                            stderr=io.StringIO(),
                            client_factory=FakeFactory(client),
                        ),
                        0,
                    )
                    token = token_file.read_text("utf-8")
                    self.assertTrue(token)
                    self.assertNotIn(token, stdout.getvalue())
                    self.assertNotIn('"token":', stdout.getvalue())
                    self.assertIn(str(token_file.resolve()), stdout.getvalue())

    def test_prepare_token_output_never_overwrites_file_or_follows_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.write_text("do-not-overwrite", encoding="utf-8")
            link = root / "token-link"
            link.symlink_to(target)
            for token_file in (target, link):
                with self.subTest(token_file=token_file):
                    stdout = io.StringIO()
                    exit_code = main(
                        [
                            "--state-dir",
                            str(root / (token_file.name + "-state")),
                            "--json",
                            "prepare-action",
                            "--type",
                            "like",
                            "--token-output",
                            str(token_file),
                        ],
                        stdout=stdout,
                        stderr=io.StringIO(),
                        client_factory=FakeFactory(FakeClient()),
                    )

                    self.assertEqual(exit_code, 2)
                    self.assertEqual(
                        json.loads(stdout.getvalue())["error"]["code"],
                        "INVALID_ARGUMENT",
                    )
                    self.assertEqual(target.read_text("utf-8"), "do-not-overwrite")

    def test_private_token_flow_without_fchmod_uses_private_directory_acl(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "state"
            token_file = root / "portable.token"
            prepared_stdout = io.StringIO()
            stderr = io.StringIO()

            with patch("douyin_control.os.fchmod", None, create=True), patch(
                "douyin_control._supports_posix_permission_bits",
                return_value=False,
            ):
                prepare_exit = main(
                    [
                        "--state-dir",
                        str(state_dir),
                        "--json",
                        "prepare-action",
                        "--type",
                        "like",
                        "--token-output",
                        str(token_file),
                    ],
                    stdout=prepared_stdout,
                    stderr=stderr,
                    client_factory=FakeFactory(client),
                )
                token = token_file.read_text("utf-8")
                os.chmod(token_file, 0o666)
                executed_stdout = io.StringIO()
                execute_exit = main(
                    [
                        "--state-dir",
                        str(state_dir),
                        "--json",
                        "execute-action",
                        "--token-file",
                        str(token_file),
                    ],
                    stdout=executed_stdout,
                    stderr=stderr,
                    client_factory=FakeFactory(client),
                )

            self.assertEqual(prepare_exit, 0)
            self.assertEqual(execute_exit, 0)
            self.assertEqual(
                json.loads(executed_stdout.getvalue())["data"]["status"],
                "executed",
            )
            self.assertFalse(token_file.exists())
            self.assertNotIn(token, prepared_stdout.getvalue())
            self.assertNotIn('"token":', prepared_stdout.getvalue())
            self.assertNotIn(
                "Traceback",
                prepared_stdout.getvalue() + stderr.getvalue(),
            )

    def test_private_token_fallback_chmod_failure_cleans_partial_file(self):
        client = FakeClient()
        real_chmod = os.chmod
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "state"
            token_file = root / "portable.token"
            stdout = io.StringIO()
            stderr = io.StringIO()

            def fail_token_chmod(path, mode):
                if Path(path).resolve() == token_file.resolve():
                    raise OSError("simulated path chmod failure")
                return real_chmod(path, mode)

            with patch("douyin_control.os.fchmod", None, create=True), patch(
                "douyin_control.os.chmod",
                side_effect=fail_token_chmod,
            ):
                exit_code = main(
                    [
                        "--state-dir",
                        str(state_dir),
                        "--json",
                        "prepare-action",
                        "--type",
                        "like",
                        "--token-output",
                        str(token_file),
                    ],
                    stdout=stdout,
                    stderr=stderr,
                    client_factory=FakeFactory(client),
                )

            self.assertEqual(exit_code, 2)
            self.assertEqual(
                json.loads(stdout.getvalue())["error"]["code"],
                "INVALID_ARGUMENT",
            )
            self.assertFalse(token_file.exists())
            self.assertNotIn('"token":', stdout.getvalue())
            self.assertNotIn("Traceback", stdout.getvalue() + stderr.getvalue())

    def test_comment_requires_exactly_one_private_or_legacy_text_channel(self):
        cases = (
            ["prepare-action", "--type", "comment"],
            [
                "prepare-action",
                "--type",
                "comment",
                "--text",
                "legacy",
                "--text-stdin",
            ],
            ["prepare-action", "--type", "like", "--text-stdin"],
        )
        for command in cases:
            with self.subTest(command=command), tempfile.TemporaryDirectory() as directory:
                factory = FakeFactory(FakeClient())
                state_dir = Path(directory) / "state"
                stdout = io.StringIO()
                exit_code = main(
                    ["--state-dir", str(state_dir), "--json", *command],
                    stdin=io.StringIO("private text"),
                    stdout=stdout,
                    stderr=io.StringIO(),
                    client_factory=factory,
                )

                self.assertEqual(exit_code, 2)
                self.assertEqual(
                    json.loads(stdout.getvalue())["error"]["code"],
                    "INVALID_ARGUMENT",
                )
                self.assertFalse(state_dir.exists())
                self.assertEqual(factory.calls, [])

    def test_private_comment_input_failures_precede_client_and_state(self):
        cases = (
            (io.StringIO(""), "empty"),
            (io.StringIO("\ud800"), "surrogate"),
            (io.StringIO("你" * 1366), "oversized"),
            (io.BytesIO(b"\xff"), "invalid-utf8"),
        )
        for stdin, name in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                state_dir = Path(directory) / "state"
                factory = FakeFactory(FakeClient())
                stdout = io.StringIO()
                exit_code = main(
                    [
                        "--state-dir",
                        str(state_dir),
                        "--json",
                        "prepare-action",
                        "--type",
                        "comment",
                        "--text-stdin",
                    ],
                    stdin=stdin,
                    stdout=stdout,
                    stderr=io.StringIO(),
                    client_factory=factory,
                )

                self.assertEqual(exit_code, 2)
                self.assertEqual(
                    json.loads(stdout.getvalue())["error"]["code"],
                    "INVALID_ARGUMENT",
                )
                self.assertNotIn("Traceback", stdout.getvalue())
                self.assertFalse(state_dir.exists())
                self.assertEqual(factory.calls, [])

    def test_real_cli_rejects_invalid_and_oversized_comment_stdin_before_state(self):
        inputs = (b"\xff", b"x" * 4097)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, input_bytes in enumerate(inputs):
                with self.subTest(index=index):
                    state_dir = root / ("state-%d" % index)
                    completed = subprocess.run(
                        [
                            sys.executable,
                            str(CONTROL_SCRIPT),
                            "--state-dir",
                            str(state_dir),
                            "--json",
                            "prepare-action",
                            "--type",
                            "comment",
                            "--text-stdin",
                        ],
                        input=input_bytes,
                        check=False,
                        capture_output=True,
                    )

                    self.assertEqual(completed.returncode, 2)
                    self.assertEqual(
                        json.loads(completed.stdout)["error"]["code"],
                        "INVALID_ARGUMENT",
                    )
                    self.assertNotIn(b"Traceback", completed.stdout)
                    self.assertFalse(state_dir.exists())

    def test_real_cli_reads_comment_stdin_and_writes_token_without_stdout_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_adb = root / "fake-adb"
            fake_adb.write_text(
                """#!/usr/bin/env python3
import sys

arguments = sys.argv[1:]
joined = " ".join(arguments)
if arguments == ["devices", "-l"]:
    print("List of devices attached")
    print("DEVICE-1\\tdevice model:Pixel")
elif "dumpsys window windows" in joined:
    print("mCurrentFocus=Window{0 u0 com.ss.android.ugc.aweme/.MainActivity}")
elif "wm size" in joined:
    print("Physical size: 1080x2400")
else:
    sys.exit(1)
""",
                encoding="utf-8",
            )
            os.chmod(fake_adb, 0o700)
            token_file = root / "private" / "subprocess.token"
            comment = "subprocess private comment"

            completed = subprocess.run(
                [
                    sys.executable,
                    str(CONTROL_SCRIPT),
                    "--adb",
                    str(fake_adb),
                    "--state-dir",
                    str(root / "state"),
                    "--json",
                    "prepare-action",
                    "--type",
                    "comment",
                    "--text-stdin",
                    "--token-output",
                    str(token_file),
                ],
                input=(comment + "\n").encode("utf-8"),
                check=False,
                capture_output=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            data = json.loads(completed.stdout)["data"]
            token = token_file.read_text("utf-8")
            self.assertEqual(set(data), {"token_path", "summary"})
            self.assertEqual(self._decode_token_payload(token)["text"], comment)
            self.assertNotIn(token.encode("utf-8"), completed.stdout)
            self.assertNotIn(comment.encode("utf-8"), completed.stdout)

    def test_prepare_comment_human_summary_never_prints_comment_body(self):
        comment = "仅用于确认的秘密评论正文"
        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            stdout = io.StringIO()
            stderr = io.StringIO()
            exit_code = main(
                [
                    "--state-dir",
                    str(Path(directory) / "state"),
                    "prepare-action",
                    "--type",
                    "comment",
                    "--text",
                    comment,
                ],
                stdout=stdout,
                stderr=stderr,
                client_factory=FakeFactory(client),
            )

        self.assertEqual(exit_code, 0)
        self.assertNotIn(comment, stdout.getvalue())
        self.assertNotIn(comment, stderr.getvalue())
        self.assertIn("comment_digest", stdout.getvalue())
        self.assertIn("comment_length_bytes", stdout.getvalue())
        self.assertIn(str(len(comment.encode("utf-8"))), stdout.getvalue())
        self.assertEqual(client.writes, [])

    def test_execute_action_accepts_token_and_performs_bound_action_once(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            prepared_stdout = io.StringIO()
            self.assertEqual(
                main(
                    ["--state-dir", str(state_dir), "--json", "prepare-action", "--type", "like"],
                    stdout=prepared_stdout,
                    stderr=io.StringIO(),
                    client_factory=FakeFactory(client),
                ),
                0,
            )
            token = json.loads(prepared_stdout.getvalue())["data"]["token"]
            executed_stdout = io.StringIO()

            exit_code = main(
                ["--state-dir", str(state_dir), "--json", "execute-action", "--token", token],
                stdout=executed_stdout,
                stderr=io.StringIO(),
                client_factory=FakeFactory(client),
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(executed_stdout.getvalue())["data"]["status"], "executed")
        self.assertEqual(len([write for write in client.writes if write[0] == "tap"]), 1)

    def test_cancel_action_accepts_token_without_device_write(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            prepared_stdout = io.StringIO()
            self.assertEqual(
                main(
                    ["--state-dir", str(state_dir), "--json", "prepare-action", "--type", "follow"],
                    stdout=prepared_stdout,
                    stderr=io.StringIO(),
                    client_factory=FakeFactory(client),
                ),
                0,
            )
            token = json.loads(prepared_stdout.getvalue())["data"]["token"]
            canceled_stdout = io.StringIO()

            exit_code = main(
                ["--state-dir", str(state_dir), "--json", "cancel-action", "--token", token],
                stdout=canceled_stdout,
                stderr=io.StringIO(),
                client_factory=FakeFactory(client),
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(canceled_stdout.getvalue())["data"]["status"], "canceled")
        self.assertEqual(client.writes, [])

    def test_execute_and_cancel_read_then_delete_private_token_files(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "state"
            for command, action_type, expected_status in (
                ("execute-action", "like", "executed"),
                ("cancel-action", "follow", "canceled"),
            ):
                with self.subTest(command=command):
                    token_file = root / (command + ".token")
                    self.assertEqual(
                        main(
                            [
                                "--state-dir",
                                str(state_dir),
                                "--json",
                                "prepare-action",
                                "--type",
                                action_type,
                                "--token-output",
                                str(token_file),
                            ],
                            stdout=io.StringIO(),
                            stderr=io.StringIO(),
                            client_factory=FakeFactory(client),
                        ),
                        0,
                    )
                    self.assertTrue(token_file.exists())
                    stdout = io.StringIO()
                    token_file_presence_at_client_creation = []

                    def client_factory(**kwargs):
                        token_file_presence_at_client_creation.append(token_file.exists())
                        client.serial = kwargs["serial"]
                        return client

                    exit_code = main(
                        [
                            "--state-dir",
                            str(state_dir),
                            "--json",
                            command,
                            "--token-file",
                            str(token_file),
                        ],
                        stdout=stdout,
                        stderr=io.StringIO(),
                        client_factory=client_factory,
                    )

                    self.assertEqual(exit_code, 0)
                    self.assertEqual(
                        json.loads(stdout.getvalue())["data"]["status"],
                        expected_status,
                    )
                    self.assertFalse(token_file.exists())
                    self.assertEqual(token_file_presence_at_client_creation, [False])

    def test_execute_accepts_injected_token_stdin_without_argv_secret(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            prepared_stdout = io.StringIO()
            self.assertEqual(
                main(
                    [
                        "--state-dir",
                        str(state_dir),
                        "--json",
                        "prepare-action",
                        "--type",
                        "like",
                    ],
                    stdout=prepared_stdout,
                    stderr=io.StringIO(),
                    client_factory=FakeFactory(client),
                ),
                0,
            )
            token = json.loads(prepared_stdout.getvalue())["data"]["token"]
            stdout = io.StringIO()

            exit_code = main(
                [
                    "--state-dir",
                    str(state_dir),
                    "--json",
                    "execute-action",
                    "--token-stdin",
                ],
                stdin=io.StringIO(token + "\r\n"),
                stdout=stdout,
                stderr=io.StringIO(),
                client_factory=FakeFactory(client),
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["data"]["status"], "executed")

    def test_action_token_requires_exactly_one_input_channel(self):
        cases = (
            ["execute-action"],
            ["cancel-action", "--token", "legacy", "--token-stdin"],
            [
                "execute-action",
                "--token",
                "legacy",
                "--token-file",
                "/tmp/private-token",
            ],
        )
        for command in cases:
            with self.subTest(command=command), tempfile.TemporaryDirectory() as directory:
                factory = FakeFactory(FakeClient())
                state_dir = Path(directory) / "state"
                stdout = io.StringIO()
                exit_code = main(
                    ["--state-dir", str(state_dir), "--json", *command],
                    stdin=io.StringIO("stdin-token"),
                    stdout=stdout,
                    stderr=io.StringIO(),
                    client_factory=factory,
                )

                self.assertEqual(exit_code, 2)
                self.assertEqual(
                    json.loads(stdout.getvalue())["error"]["code"],
                    "INVALID_ARGUMENT",
                )
                self.assertEqual(factory.calls, [])
                self.assertFalse(state_dir.exists())

    def test_private_token_input_rejects_unsafe_files_before_client_or_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unsafe = root / "unsafe.token"
            unsafe.write_text("private-token", encoding="utf-8")
            os.chmod(unsafe, 0o640)
            target = root / "target.token"
            target.write_text("private-token", encoding="utf-8")
            os.chmod(target, 0o600)
            symlink = root / "symlink.token"
            symlink.symlink_to(target)
            token_dir = root / "directory.token"
            token_dir.mkdir()
            shared_parent = root / "shared"
            shared_parent.mkdir()
            os.chmod(shared_parent, 0o777)
            shared_token = shared_parent / "shared.token"
            shared_token.write_text("private-token", encoding="utf-8")
            os.chmod(shared_token, 0o600)

            for token_file in (unsafe, symlink, token_dir, shared_token):
                with self.subTest(token_file=token_file):
                    factory = FakeFactory(FakeClient())
                    state_dir = root / (token_file.name + "-state")
                    stdout = io.StringIO()
                    exit_code = main(
                        [
                            "--state-dir",
                            str(state_dir),
                            "--json",
                            "cancel-action",
                            "--token-file",
                            str(token_file),
                        ],
                        stdout=stdout,
                        stderr=io.StringIO(),
                        client_factory=factory,
                    )

                    self.assertEqual(exit_code, 2)
                    self.assertEqual(
                        json.loads(stdout.getvalue())["error"]["code"],
                        "INVALID_ARGUMENT",
                    )
                    self.assertEqual(factory.calls, [])
                    self.assertFalse(state_dir.exists())

    def test_empty_invalid_utf8_and_oversized_private_tokens_fail_before_client(self):
        cases = (
            (b"", "empty"),
            (b"\xff", "invalid-utf8"),
            (b"t" * 65537, "oversized"),
        )
        for payload, name in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                token_file = root / "private.token"
                token_file.write_bytes(payload)
                os.chmod(token_file, 0o600)
                state_dir = root / "state"
                factory = FakeFactory(FakeClient())
                stdout = io.StringIO()

                exit_code = main(
                    [
                        "--state-dir",
                        str(state_dir),
                        "--json",
                        "execute-action",
                        "--token-file",
                        str(token_file),
                    ],
                    stdout=stdout,
                    stderr=io.StringIO(),
                    client_factory=factory,
                )

                self.assertEqual(exit_code, 2)
                self.assertEqual(
                    json.loads(stdout.getvalue())["error"]["code"],
                    "INVALID_ARGUMENT",
                )
                self.assertFalse(token_file.exists())
                self.assertEqual(factory.calls, [])
                self.assertFalse(state_dir.exists())

    def test_oversized_comment_fails_before_client_or_state_creation(self):
        comment = "你" * 1366
        self.assertGreater(len(comment.encode("utf-8")), 4096)
        factory = FakeFactory(FakeClient())
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            stdout = io.StringIO()
            exit_code = main(
                [
                    "--state-dir",
                    str(state_dir),
                    "--json",
                    "prepare-action",
                    "--type",
                    "comment",
                    "--text",
                    comment,
                ],
                stdout=stdout,
                stderr=io.StringIO(),
                client_factory=factory,
            )

            self.assertFalse(state_dir.exists())
        self.assertEqual(exit_code, 2)
        self.assertEqual(json.loads(stdout.getvalue())["error"]["code"], "INVALID_ARGUMENT")
        self.assertNotIn(comment, stdout.getvalue())
        self.assertEqual(factory.calls, [])

    def test_surrogate_comment_is_stable_invalid_argument_before_client_or_state(self):
        comment = "\ud800"
        factory = FakeFactory(FakeClient())
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            stdout = io.StringIO()
            stderr = io.StringIO()

            exit_code = main(
                [
                    "--state-dir",
                    str(state_dir),
                    "--json",
                    "prepare-action",
                    "--type",
                    "comment",
                    "--text",
                    comment,
                ],
                stdout=stdout,
                stderr=stderr,
                client_factory=factory,
            )

            self.assertFalse(state_dir.exists())
        self.assertEqual(exit_code, 2)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(len(stdout.getvalue().splitlines()), 1)
        envelope = json.loads(stdout.getvalue())
        self.assertEqual(envelope["error"]["code"], "INVALID_ARGUMENT")
        self.assertNotIn(comment, stdout.getvalue())
        self.assertNotIn("Traceback", stdout.getvalue())
        self.assertEqual(factory.calls, [])

    def test_oversized_token_fails_before_client_or_state_creation(self):
        token = "t" * 65537
        factory = FakeFactory(FakeClient())
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            stdout = io.StringIO()
            exit_code = main(
                [
                    "--state-dir",
                    str(state_dir),
                    "--json",
                    "execute-action",
                    "--token",
                    token,
                ],
                stdout=stdout,
                stderr=io.StringIO(),
                client_factory=factory,
            )

            self.assertFalse(state_dir.exists())
        self.assertEqual(exit_code, 2)
        self.assertEqual(json.loads(stdout.getvalue())["error"]["code"], "INVALID_ARGUMENT")
        self.assertNotIn(token, stdout.getvalue())
        self.assertEqual(factory.calls, [])

    def test_invalid_bounded_token_uses_action_policy_exit_code(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            exit_code = main(
                [
                    "--state-dir",
                    str(Path(directory) / "state"),
                    "--json",
                    "execute-action",
                    "--token",
                    "invalid-token",
                ],
                stdout=stdout,
                stderr=io.StringIO(),
                client_factory=FakeFactory(FakeClient()),
            )

        self.assertEqual(exit_code, 7)
        self.assertEqual(json.loads(stdout.getvalue())["error"]["code"], "TOKEN_INVALID")

    def test_human_failure_without_core_hint_gets_category_fallback_hint(self):
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            exit_code = main(
                [
                    "--state-dir",
                    str(Path(directory) / "state"),
                    "execute-action",
                    "--token",
                    "invalid-token",
                ],
                stdout=io.StringIO(),
                stderr=stderr,
                client_factory=FakeFactory(FakeClient()),
            )

        self.assertEqual(exit_code, 7)
        self.assertIn("ERROR [TOKEN_INVALID]", stderr.getvalue())
        self.assertIn("Hint:", stderr.getvalue())

    def test_state_dir_flag_overrides_environment_and_environment_overrides_default(self):
        with tempfile.TemporaryDirectory() as directory:
            environment_state = Path(directory) / "environment-state"
            flag_state = Path(directory) / "flag-state"
            with patch.dict(
                os.environ,
                {"DOUYIN_AGENT_STATE_DIR": str(environment_state)},
                clear=False,
            ):
                self.assertEqual(
                    main(
                        ["--state-dir", str(flag_state), "--json", "prepare-action", "--type", "like"],
                        stdout=io.StringIO(),
                        stderr=io.StringIO(),
                        client_factory=FakeFactory(FakeClient()),
                    ),
                    0,
                )
                self.assertTrue(any((flag_state / "pending").glob("*.json")))
                self.assertFalse(environment_state.exists())

                self.assertEqual(
                    main(
                        ["--json", "prepare-action", "--type", "follow"],
                        stdout=io.StringIO(),
                        stderr=io.StringIO(),
                        client_factory=FakeFactory(FakeClient()),
                    ),
                    0,
                )
                self.assertTrue(any((environment_state / "pending").glob("*.json")))

    def test_default_state_dir_is_under_callers_working_directory(self):
        original_directory = Path.cwd()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DOUYIN_AGENT_STATE_DIR", None)
            try:
                os.chdir(directory)
                exit_code = main(
                    ["--json", "prepare-action", "--type", "like"],
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                    client_factory=FakeFactory(FakeClient()),
                )
            finally:
                os.chdir(str(original_directory))

            self.assertEqual(exit_code, 0)
            default_state = Path(directory) / ".douyin-adb-control"
            self.assertTrue(any((default_state / "pending").glob("*.json")))

    def test_direct_comment_command_and_execute_text_bypass_are_rejected(self):
        cases = (
            ["--json", "comment", "--text", "do-not-print"],
            ["--json", "execute-action", "--token", "short", "--text", "do-not-print"],
        )
        for argv in cases:
            with self.subTest(argv=argv):
                stdout = io.StringIO()
                exit_code = main(
                    argv,
                    stdout=stdout,
                    stderr=io.StringIO(),
                    client_factory=FakeFactory(FakeClient()),
                )
                self.assertEqual(exit_code, 2)
                self.assertEqual(json.loads(stdout.getvalue())["error"]["code"], "INVALID_ARGUMENT")
                self.assertNotIn("do-not-print", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
