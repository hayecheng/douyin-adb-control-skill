import base64
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

UNSAFE_ADB_TEXTS = (
    "hello;rm",
    "a&b",
    "a|b",
    "$HOME",
    "$(id)",
    "`id`",
    "a'b",
    'a"b',
    "a<b",
    "a>b",
    "literal%s",
)

from douyin_core import (
    ADBClient,
    ControlError,
    Device,
    parse_devices,
    ratio_to_pixel,
    select_device,
)


def completed(stdout=b"", stderr=b"", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


class StubRunner:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class CoreHelperTests(unittest.TestCase):
    def test_parse_devices_preserves_state_and_details(self):
        output = (
            "List of devices attached\n"
            "ABC123\tdevice product:p model:Pixel_8 transport_id:1\n"
            "BAD\tunauthorized\n"
        )

        devices = parse_devices(output)

        self.assertEqual(
            [(device.serial, device.state) for device in devices],
            [("ABC123", "device"), ("BAD", "unauthorized")],
        )
        self.assertEqual(
            devices[0].details,
            {"product": "p", "model": "Pixel_8", "transport_id": "1"},
        )

    def test_ratio_to_pixel_clamps_edges(self):
        self.assertEqual(ratio_to_pixel(1.0, 1080), 1079)
        self.assertEqual(ratio_to_pixel(0.5, 1920), 960)

    def test_ratio_to_pixel_rejects_invalid_values(self):
        for ratio, extent in ((-0.1, 1080), (1.1, 1080), (0.5, 0)):
            with self.subTest(ratio=ratio, extent=extent):
                with self.assertRaises(ControlError) as raised:
                    ratio_to_pixel(ratio, extent)
                self.assertEqual(raised.exception.code, "INVALID_COORDINATE")

    def test_select_device_chooses_only_usable_device(self):
        devices = [
            Device("BAD", "offline", {}),
            Device("READY", "device", {"model": "Pixel_8"}),
        ]

        self.assertEqual(select_device(devices), devices[1])

    def test_select_device_requires_serial_when_multiple_are_usable(self):
        devices = [Device("ONE", "device", {}), Device("TWO", "device", {})]

        with self.assertRaises(ControlError) as raised:
            select_device(devices)

        self.assertEqual(raised.exception.code, "MULTIPLE_DEVICES")

    def test_select_device_reports_no_usable_device(self):
        with self.assertRaises(ControlError) as raised:
            select_device([Device("BAD", "unauthorized", {})])

        self.assertEqual(raised.exception.code, "NO_DEVICE")

    def test_select_device_reports_selected_unusable_state(self):
        cases = (("unauthorized", "DEVICE_UNAUTHORIZED"), ("offline", "DEVICE_OFFLINE"))
        for state, code in cases:
            with self.subTest(state=state):
                with self.assertRaises(ControlError) as raised:
                    select_device([Device("TARGET", state, {})], "TARGET")
                self.assertEqual(raised.exception.code, code)


class ADBClientTests(unittest.TestCase):
    def assert_runner_calls(self, runner, *commands):
        self.assertEqual(
            runner.calls,
            [
                (
                    command,
                    {"check": False, "capture_output": True, "timeout": 30.0},
                )
                for command in commands
            ],
        )

    def test_devices_uses_long_listing_without_serial(self):
        runner = StubRunner(
            completed(b"List of devices attached\nABC123\tdevice model:Pixel_8\n")
        )
        client = ADBClient(adb_path="custom-adb", serial="IGNORED", runner=runner)

        devices = client.devices()

        self.assertEqual(devices, [Device("ABC123", "device", {"model": "Pixel_8"})])
        self.assert_runner_calls(runner, ["custom-adb", "devices", "-l"])

    def test_selected_device_discovers_device_when_serial_is_omitted(self):
        runner = StubRunner(
            completed(b"List of devices attached\nABC123\tdevice model:Pixel_8\n")
        )
        client = ADBClient(runner=runner)

        selected = client.selected_device()

        self.assertEqual(selected.serial, "ABC123")
        self.assert_runner_calls(runner, ["adb", "devices", "-l"])

    def test_screen_size_uses_selected_serial(self):
        runner = StubRunner(completed(b"Physical size: 1080x2400\n"))
        client = ADBClient(serial="ABC123", runner=runner)

        self.assertEqual(client.screen_size(), (1080, 2400))
        self.assert_runner_calls(
            runner,
            ["adb", "-s", "ABC123", "shell", "wm", "size"],
        )

    def test_foreground_package_parses_current_focus(self):
        runner = StubRunner(
            completed(
                b"mCurrentFocus=Window{123 u0 com.ss.android.ugc.aweme/.MainActivity}\n"
            )
        )
        client = ADBClient(serial="ABC123", runner=runner)

        self.assertEqual(client.foreground_package(), "com.ss.android.ugc.aweme")
        self.assert_runner_calls(
            runner,
            ["adb", "-s", "ABC123", "shell", "dumpsys", "window", "windows"],
        )

    def test_package_check_uses_pm_path(self):
        runner = StubRunner(completed(b"package:/data/app/base.apk\n"))
        client = ADBClient(serial="ABC123", runner=runner)

        self.assertTrue(client.is_package_installed("com.ss.android.ugc.aweme"))
        self.assert_runner_calls(
            runner,
            [
                "adb",
                "-s",
                "ABC123",
                "shell",
                "pm",
                "path",
                "com.ss.android.ugc.aweme",
            ],
        )

    def test_adb_keyboard_status_checks_install_enable_and_selection(self):
        runner = StubRunner(
            completed(b"package:/data/app/com.android.adbkeyboard/base.apk\n"),
            completed(b"com.android.adbkeyboard/.AdbIME\n"),
            completed(b"com.android.adbkeyboard/.AdbIME\n"),
        )
        client = ADBClient(serial="ABC123", runner=runner)

        status = client.adb_keyboard_status()

        self.assertEqual(
            status,
            {
                "installed": True,
                "enabled": True,
                "selected": True,
                "available": True,
                "component": "com.android.adbkeyboard/.AdbIME",
            },
        )
        self.assert_runner_calls(
            runner,
            [
                "adb", "-s", "ABC123", "shell", "pm", "path",
                "com.android.adbkeyboard",
            ],
            ["adb", "-s", "ABC123", "shell", "ime", "list", "-s"],
            [
                "adb", "-s", "ABC123", "shell", "settings", "get",
                "secure", "default_input_method",
            ],
        )

    def test_adb_keyboard_status_requires_current_enabled_component(self):
        cases = (
            (b"\n", b"com.android.adbkeyboard/.AdbIME\n", b"com.android.adbkeyboard/.AdbIME\n"),
            (b"package:/data/app/base.apk\n", b"com.example/.OtherIME\n", b"com.android.adbkeyboard/.AdbIME\n"),
            (b"package:/data/app/base.apk\n", b"com.android.adbkeyboard/.AdbIME\n", b"com.example/.OtherIME\n"),
        )
        for package_output, enabled_output, selected_output in cases:
            with self.subTest(outputs=(package_output, enabled_output, selected_output)):
                client = ADBClient(
                    serial="ABC123",
                    runner=StubRunner(
                        completed(package_output),
                        completed(enabled_output),
                        completed(selected_output),
                    ),
                )

                self.assertFalse(client.adb_keyboard_status()["available"])

    def test_screenshot_writes_png_bytes_without_conversion(self):
        png = b"\x89PNG\r\n\x1a\n\x00\r\n"
        runner = StubRunner(completed(png))
        client = ADBClient(serial="ABC123", runner=runner)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "screen.png"
            returned = client.screenshot(output)
            self.assertEqual(returned, output)
            self.assertEqual(output.read_bytes(), png)

        self.assert_runner_calls(
            runner,
            ["adb", "-s", "ABC123", "exec-out", "screencap", "-p"],
        )

    def test_screenshot_wraps_local_write_failure(self):
        runner = StubRunner(completed(b"png"))
        client = ADBClient(serial="ABC123", runner=runner)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "missing" / "screen.png"
            with self.assertRaises(ControlError) as raised:
                client.screenshot(output)

        self.assertEqual(raised.exception.code, "SCREENSHOT_FAILED")
        self.assertEqual(
            raised.exception.message,
            "Unable to save the Android screenshot",
        )

    def test_launch_and_stop_package_use_exact_commands(self):
        runner = StubRunner(
            completed(b"package:/data/app/base.apk\n"),
            completed(),
            completed(b"package:/data/app/base.apk\n"),
            completed(),
        )
        client = ADBClient(serial="ABC123", runner=runner)
        package = "com.ss.android.ugc.aweme"

        client.launch_package(package)
        client.stop_package(package)

        self.assert_runner_calls(
            runner,
            ["adb", "-s", "ABC123", "shell", "pm", "path", package],
            [
                "adb",
                "-s",
                "ABC123",
                "shell",
                "monkey",
                "-p",
                package,
                "-c",
                "android.intent.category.LAUNCHER",
                "1",
            ],
            ["adb", "-s", "ABC123", "shell", "pm", "path", package],
            ["adb", "-s", "ABC123", "shell", "am", "force-stop", package],
        )

    def test_launch_and_stop_reject_missing_package_before_lifecycle_write(self):
        for method_name in ("launch_package", "stop_package"):
            with self.subTest(method=method_name):
                runner = StubRunner(completed(b"\n"))
                client = ADBClient(serial="ABC123", runner=runner)

                with self.assertRaises(ControlError) as raised:
                    getattr(client, method_name)("com.example.missing")

                self.assertEqual(raised.exception.code, "PACKAGE_NOT_FOUND")
                self.assert_runner_calls(
                    runner,
                    [
                        "adb", "-s", "ABC123", "shell", "pm", "path",
                        "com.example.missing",
                    ],
                )

    def test_ui_dump_copies_remote_xml_bytes(self):
        xml = b'<?xml version="1.0"?><hierarchy />\n'
        runner = StubRunner(completed(b"UI hierarchy dumped\n"), completed(xml))
        client = ADBClient(serial="ABC123", runner=runner)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "window.xml"
            returned = client.ui_dump(output)
            self.assertEqual(returned, output)
            self.assertEqual(output.read_bytes(), xml)

        self.assert_runner_calls(
            runner,
            [
                "adb",
                "-s",
                "ABC123",
                "shell",
                "uiautomator",
                "dump",
                "/sdcard/window_dump.xml",
            ],
            [
                "adb",
                "-s",
                "ABC123",
                "exec-out",
                "cat",
                "/sdcard/window_dump.xml",
            ],
        )

    def test_ui_dump_wraps_local_write_failure(self):
        runner = StubRunner(completed(), completed(b"<hierarchy />"))
        client = ADBClient(serial="ABC123", runner=runner)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "missing" / "window.xml"
            with self.assertRaises(ControlError) as raised:
                client.ui_dump(output)

        self.assertEqual(raised.exception.code, "UI_DUMP_FAILED")
        self.assertEqual(
            raised.exception.message,
            "Unable to save the Android UI hierarchy dump",
        )

    def test_swipe_and_tap_use_input_commands(self):
        runner = StubRunner(completed(), completed())
        client = ADBClient(serial="ABC123", runner=runner)

        client.swipe((100, 200), (300, 400), 350)
        client.tap(500, 600)

        self.assert_runner_calls(
            runner,
            [
                "adb",
                "-s",
                "ABC123",
                "shell",
                "input",
                "swipe",
                "100",
                "200",
                "300",
                "400",
                "350",
            ],
            ["adb", "-s", "ABC123", "shell", "input", "tap", "500", "600"],
        )

    def test_plain_ascii_input_uses_percent_s_for_spaces(self):
        runner = StubRunner(completed())
        client = ADBClient(serial="ABC123", runner=runner)

        client.input_text("hello world", "adb")

        self.assert_runner_calls(
            runner,
            [
                "adb",
                "-s",
                "ABC123",
                "shell",
                "input",
                "text",
                "hello%sworld",
            ],
        )

    def test_adb_keyboard_input_uses_utf8_base64_broadcast(self):
        runner = StubRunner(
            completed(b"package:/data/app/base.apk\n"),
            completed(b"com.android.adbkeyboard/.AdbIME\n"),
            completed(b"com.android.adbkeyboard/.AdbIME\n"),
            completed(),
        )
        client = ADBClient(serial="ABC123", runner=runner)
        text = "你好"

        client.input_text(text, "adb-keyboard")

        self.assert_runner_calls(
            runner,
            [
                "adb", "-s", "ABC123", "shell", "pm", "path",
                "com.android.adbkeyboard",
            ],
            ["adb", "-s", "ABC123", "shell", "ime", "list", "-s"],
            [
                "adb", "-s", "ABC123", "shell", "settings", "get",
                "secure", "default_input_method",
            ],
            [
                "adb",
                "-s",
                "ABC123",
                "shell",
                "am",
                "broadcast",
                "-a",
                "ADB_INPUT_B64",
                "--es",
                "msg",
                base64.b64encode(text.encode("utf-8")).decode("ascii"),
            ],
        )

    def test_adb_keyboard_input_fails_closed_when_ime_is_not_selected(self):
        runner = StubRunner(
            completed(b"package:/data/app/base.apk\n"),
            completed(b"com.android.adbkeyboard/.AdbIME\n"),
            completed(b"com.example/.OtherIME\n"),
        )
        client = ADBClient(serial="ABC123", runner=runner)

        with self.assertRaises(ControlError) as raised:
            client.input_text("你好", "adb-keyboard")

        self.assertEqual(raised.exception.code, "UNSUPPORTED_TEXT_INPUT")
        self.assertEqual(len(runner.calls), 3)

    def test_auto_input_uses_adb_for_ascii_and_keyboard_for_utf8(self):
        runner = StubRunner(
            completed(),
            completed(b"package:/data/app/base.apk\n"),
            completed(b"com.android.adbkeyboard/.AdbIME\n"),
            completed(b"com.android.adbkeyboard/.AdbIME\n"),
            completed(),
        )
        client = ADBClient(serial="ABC123", runner=runner)

        client.input_text("plain text", "auto")
        client.input_text("你好", "auto")

        self.assertEqual(runner.calls[0][0][4:7], ["input", "text", "plain%stext"])
        self.assertEqual(runner.calls[-1][0][4:7], ["am", "broadcast", "-a"])

    def test_plain_adb_input_rejects_non_ascii(self):
        client = ADBClient(serial="ABC123", runner=StubRunner())

        with self.assertRaises(ControlError) as raised:
            client.input_text("你好", "adb")

        self.assertEqual(raised.exception.code, "UNSUPPORTED_TEXT_INPUT")

    def test_plain_adb_input_rejects_shell_syntax_and_literal_percent_s(self):
        for text in UNSAFE_ADB_TEXTS:
            with self.subTest(text=text):
                runner = StubRunner(completed())
                client = ADBClient(serial="ABC123", runner=runner)

                with self.assertRaises(ControlError) as raised:
                    client.input_text(text, "adb")

                self.assertEqual(raised.exception.code, "UNSUPPORTED_TEXT_INPUT")
                self.assertEqual(runner.calls, [])

    def test_auto_input_routes_unsafe_ascii_through_keyboard_base64(self):
        for text in UNSAFE_ADB_TEXTS:
            with self.subTest(text=text):
                runner = StubRunner(
                    completed(b"package:/data/app/base.apk\n"),
                    completed(b"com.android.adbkeyboard/.AdbIME\n"),
                    completed(b"com.android.adbkeyboard/.AdbIME\n"),
                    completed(),
                )
                client = ADBClient(serial="ABC123", runner=runner)

                client.input_text(text, "auto")

                self.assert_runner_calls(
                    runner,
                    [
                        "adb", "-s", "ABC123", "shell", "pm", "path",
                        "com.android.adbkeyboard",
                    ],
                    ["adb", "-s", "ABC123", "shell", "ime", "list", "-s"],
                    [
                        "adb", "-s", "ABC123", "shell", "settings", "get",
                        "secure", "default_input_method",
                    ],
                    [
                        "adb",
                        "-s",
                        "ABC123",
                        "shell",
                        "am",
                        "broadcast",
                        "-a",
                        "ADB_INPUT_B64",
                        "--es",
                        "msg",
                        base64.b64encode(text.encode("utf-8")).decode("ascii"),
                    ],
                )

    def test_plain_adb_input_allows_minimal_safe_punctuation(self):
        runner = StubRunner(completed())
        client = ADBClient(serial="ABC123", runner=runner)

        client.input_text("Alpha_2, v1.0-beta", "adb")

        self.assert_runner_calls(
            runner,
            [
                "adb",
                "-s",
                "ABC123",
                "shell",
                "input",
                "text",
                "Alpha_2,%sv1.0-beta",
            ],
        )

    def test_nonzero_device_command_raises_stable_error(self):
        runner = StubRunner(completed(stderr=b"device offline", returncode=1))
        client = ADBClient(serial="ABC123", runner=runner)

        with self.assertRaises(ControlError) as raised:
            client.tap(1, 2)

        self.assertEqual(raised.exception.code, "ADB_COMMAND_FAILED")
        self.assertNotIn("device offline", raised.exception.message)

    def test_missing_adb_raises_stable_error(self):
        runner = StubRunner(FileNotFoundError("custom-adb"))
        client = ADBClient(adb_path="custom-adb", runner=runner)

        with self.assertRaises(ControlError) as raised:
            client.devices()

        self.assertEqual(raised.exception.code, "ADB_NOT_FOUND")

    def test_adb_timeout_raises_stable_error_without_command_details(self):
        timeout = subprocess.TimeoutExpired(
            ["/sensitive/custom-adb", "devices"],
            2.5,
            output=b"private stdout",
            stderr=b"private stderr",
        )
        runner = StubRunner(timeout)
        client = ADBClient(
            adb_path="/sensitive/custom-adb",
            runner=runner,
            timeout_seconds=2.5,
        )

        with self.assertRaises(ControlError) as raised:
            client.devices()

        self.assertEqual(raised.exception.code, "ADB_COMMAND_TIMEOUT")
        self.assertNotIn("sensitive", str(raised.exception.as_dict()))
        self.assertNotIn("private", str(raised.exception.as_dict()))
        self.assertEqual(
            runner.calls,
            [
                (
                    ["/sensitive/custom-adb", "devices", "-l"],
                    {"check": False, "capture_output": True, "timeout": 2.5},
                )
            ],
        )

    def test_adb_timeout_is_configurable_and_must_be_positive(self):
        runner = StubRunner(completed(b"List of devices attached\n"))
        client = ADBClient(runner=runner, timeout_seconds=1.25)

        client.devices()

        self.assertEqual(runner.calls[0][1]["timeout"], 1.25)
        for invalid in (0, -1, float("inf"), float("nan"), True):
            with self.subTest(timeout=invalid):
                with self.assertRaises(ControlError) as raised:
                    ADBClient(timeout_seconds=invalid)
                self.assertEqual(raised.exception.code, "INVALID_CONFIG")

    def test_adb_execution_os_errors_are_stable_and_do_not_echo_paths(self):
        errors = (
            PermissionError("/sensitive/permission-denied-adb"),
            IsADirectoryError("/sensitive/directory-adb"),
            OSError("/sensitive/other-adb-error"),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                client = ADBClient(runner=StubRunner(error))

                with self.assertRaises(ControlError) as raised:
                    client.devices()

                self.assertEqual(raised.exception.code, "ADB_NOT_FOUND")
                self.assertEqual(
                    raised.exception.hint,
                    "Install adb or provide its path with --adb",
                )
                self.assertNotIn("sensitive", str(raised.exception.as_dict()))


if __name__ == "__main__":
    unittest.main()
