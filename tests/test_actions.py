import base64
import copy
import hashlib
import json
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from douyin_actions import ActionService, ActionStore, load_config
from douyin_core import ControlError, Device


class FakeADBClient:
    def __init__(self, serial="DEVICE-1", screen=(1080, 2400), timeline=None):
        self.serial = serial
        self.screen = screen
        self.foreground = "com.ss.android.ugc.aweme"
        self.keyboard_status = {
            "installed": True,
            "enabled": True,
            "selected": True,
            "available": True,
            "component": "com.android.adbkeyboard/.AdbIME",
        }
        self.reads = []
        self.writes = []
        self.timeline = timeline
        self.write_observer = None
        self.write_error = None

    def screen_size(self):
        self.reads.append("screen_size")
        return self.screen

    def selected_device(self):
        self.reads.append(("selected_device", self.serial))
        return Device(self.serial or "DEVICE-1", "device", {})

    def foreground_package(self):
        self.reads.append(("foreground_package", self.serial))
        return self.foreground

    def adb_keyboard_status(self):
        self.reads.append(("adb_keyboard_status", self.serial))
        return dict(self.keyboard_status)

    def tap(self, x, y):
        if self.write_observer is not None:
            self.write_observer()
        self.writes.append(("tap", x, y))
        if self.timeline is not None:
            self.timeline.append(("tap", x, y))
        if self.write_error is not None:
            raise self.write_error

    def input_text(self, text, backend):
        if self.write_observer is not None:
            self.write_observer()
        self.writes.append(("input_text", text, backend))
        if self.timeline is not None:
            self.timeline.append(("input_text", text, backend))
        if self.write_error is not None:
            raise self.write_error


class BarrierADBClient(FakeADBClient):
    def __init__(self, tap_barrier):
        super().__init__()
        self.tap_barrier = tap_barrier

    def tap(self, x, y):
        super().tap(x, y)
        try:
            self.tap_barrier.wait(timeout=1.0)
        except threading.BrokenBarrierError:
            pass


class ChangingSelectionClient:
    def __init__(self):
        self.serial = None
        self.default_serials = ["DEVICE-A", "DEVICE-B", "DEVICE-B", "DEVICE-B"]
        self.reads = []
        self.writes = []

    def selected_device(self):
        if self.serial is not None:
            self.reads.append(("validate", self.serial))
            return Device(self.serial, "device", {})
        serial = self.default_serials.pop(0)
        self.reads.append(("select", serial))
        return Device(serial, "device", {})

    def screen_size(self):
        serial = self.serial
        if serial is None:
            serial = self.selected_device().serial
        self.reads.append(("screen_size", serial))
        return 1080, 2400

    def foreground_package(self):
        self.reads.append(("foreground_package", self.serial))
        return "com.ss.android.ugc.aweme"

    def tap(self, x, y):
        serial = self.serial
        if serial is None:
            serial = self.selected_device().serial
        self.writes.append(("tap", serial, x, y))

class ConfigurationTests(unittest.TestCase):
    def test_default_config_has_versioned_required_policy(self):
        config = load_config(None)

        self.assertEqual(config["schema_version"], 1)
        self.assertEqual(config["package"], "com.ss.android.ugc.aweme")
        self.assertEqual(set(config["swipes"]), {"next", "previous"})
        self.assertEqual(
            config["limits"],
            {"like": 3, "follow": 1, "comment": 1, "tap": 3},
        )
        self.assertEqual(config["token_ttl_seconds"], 300)
        self.assertIn(config["input_backend"], {"auto", "adb", "adb-keyboard"})

    def test_default_config_is_deep_copied(self):
        first = load_config(None)
        first["swipes"]["next"]["start"][0] = 0.0
        first["limits"]["like"] = 999

        second = load_config(None)

        self.assertNotEqual(second["swipes"]["next"]["start"][0], 0.0)
        self.assertEqual(second["limits"]["like"], 3)

    def test_example_config_is_valid(self):
        path = Path(__file__).resolve().parents[1] / "assets" / "config.example.json"

        config = load_config(path)

        self.assertEqual(config["schema_version"], 1)
        self.assertEqual(config["package"], "com.ss.android.ugc.aweme")

    def test_rejects_missing_required_sections(self):
        for key in ("package", "swipes", "limits"):
            with self.subTest(key=key):
                config = load_config(None)
                del config[key]
                self.assert_invalid_config(config)

    def test_rejects_unsupported_schema_version_and_backend(self):
        cases = (("schema_version", 2), ("input_backend", "ime-magic"))
        for key, value in cases:
            with self.subTest(key=key):
                config = load_config(None)
                config[key] = value
                self.assert_invalid_config(config)

    def test_rejects_package_names_that_are_not_safe_android_application_ids(self):
        unsafe_packages = (
            "single",
            "com..example",
            "3com.example",
            "com.3example",
            "com.example.app ",
            "com.example.app;id",
            "com.example.app\ninput tap 1 1",
            "com.example.'app'",
            'com.example."app"',
            "com.example.$(id)",
        )
        for package in unsafe_packages:
            with self.subTest(package=repr(package)):
                config = load_config(None)
                config["package"] = package
                self.assert_invalid_config(config)

    def test_rejects_invalid_coordinate_ratios(self):
        cases = (
            ("swipe start", lambda config: config["swipes"]["next"].update(start=[-0.1, 0.5])),
            ("swipe end", lambda config: config["swipes"]["previous"].update(end=[0.5, 1.1])),
            ("named position", lambda config: config["positions"].update(like=[0.5, 2.0])),
        )
        for name, mutate in cases:
            with self.subTest(name=name):
                config = load_config(None)
                mutate(config)
                self.assert_invalid_config(config)

    def test_rejects_nonpositive_duration_and_token_ttl(self):
        cases = (
            lambda config: config["swipes"]["next"].update(duration_ms=0),
            lambda config: config.update(token_ttl_seconds=0),
        )
        for mutate in cases:
            config = load_config(None)
            mutate(config)
            self.assert_invalid_config(config)

    def test_rejects_missing_or_invalid_limits(self):
        cases = (
            lambda config: config["limits"].pop("comment"),
            lambda config: config["limits"].update(like=-1),
            lambda config: config["limits"].update(tap=1.5),
        )
        for mutate in cases:
            config = load_config(None)
            mutate(config)
            self.assert_invalid_config(config)

    def assert_invalid_config(self, config):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(copy.deepcopy(config)), encoding="utf-8")
            with self.assertRaises(ControlError) as raised:
                load_config(path)
        self.assertEqual(raised.exception.code, "INVALID_CONFIG")


class ActionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temporary_directory.name)
        self.now = 1_000.0
        self.store = ActionStore(
            self.state_dir,
            token_ttl_seconds=300,
            clock=lambda: self.now,
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_create_uses_cryptographic_random_token_and_stores_payload(self):
        with patch("douyin_actions.secrets.token_urlsafe", return_value="secure-token") as random_token:
            record = self.store.create(
                {"action_type": "like", "device_serial": "DEVICE-1"}
            )

        random_token.assert_called_once_with(32)
        self.assertEqual(record["jti"], "secure-token")
        self.assertIn(".", record["token"])
        self.assertEqual(record["created_at"], 1_000.0)
        self.assertEqual(record["expires_at"], 1_300.0)
        pending = self.state_dir / "pending" / "secure-token.json"
        state = json.loads(pending.read_text("utf-8"))
        self.assertEqual(state["jti"], "secure-token")
        self.assertEqual(state["token_digest"], record["token_digest"])
        self.assertNotIn("device_serial", state)

    def test_created_tokens_are_distinct(self):
        first = self.store.create({"action_type": "like"})
        second = self.store.create({"action_type": "like"})

        self.assertNotEqual(first["token"], second["token"])
        self.assertGreaterEqual(len(first["token"]), 40)

    def test_consume_is_one_time_and_writes_used_marker(self):
        created = self.store.create({"action_type": "follow"})

        consumed = self.store.consume(created["token"], now=1_100.0)

        self.assertEqual(consumed, created)
        self.assertFalse(
            (self.state_dir / "pending" / (created["jti"] + ".json")).exists()
        )
        used_path = self.state_dir / "used" / (created["jti"] + ".json")
        used = json.loads(used_path.read_text("utf-8"))
        self.assertEqual(used["status"], "consumed")
        self.assertNotIn("text", used)
        with self.assertRaises(ControlError) as raised:
            self.store.consume(created["token"], now=1_101.0)
        self.assertEqual(raised.exception.code, "TOKEN_REPLAYED")

    def test_expired_token_is_rejected_and_cannot_be_reused(self):
        created = self.store.create({"action_type": "tap"})

        with self.assertRaises(ControlError) as raised:
            self.store.consume(created["token"], now=1_300.0)
        self.assertEqual(raised.exception.code, "TOKEN_EXPIRED")

        with self.assertRaises(ControlError) as replayed:
            self.store.consume(created["token"], now=1_299.0)
        self.assertEqual(replayed.exception.code, "TOKEN_REPLAYED")

    def test_cancel_consumes_pending_token(self):
        created = self.store.create({"action_type": "comment", "text": "private"})

        canceled = self.store.cancel(created["token"], now=1_050.0)

        self.assertEqual(canceled["status"], "canceled")
        marker = json.loads(
            (self.state_dir / "used" / (created["jti"] + ".json")).read_text(
                "utf-8"
            )
        )
        self.assertNotIn("text", marker)
        with self.assertRaises(ControlError) as raised:
            self.store.consume(created["token"], now=1_051.0)
        self.assertEqual(raised.exception.code, "TOKEN_REPLAYED")

    def test_increment_persists_per_action_counters(self):
        self.assertEqual(self.store.count("like"), 0)

        self.store.increment("like")
        self.store.increment("like")
        self.store.increment("tap")

        self.assertEqual(self.store.count("like"), 2)
        self.assertEqual(self.store.count("tap"), 1)
        counters = json.loads((self.state_dir / "counters.json").read_text("utf-8"))
        self.assertEqual(counters, {"like": 2, "tap": 1})

    def test_audit_appends_json_lines_and_removes_comment_text(self):
        self.store.audit(
            {
                "event": "prepared",
                "action_type": "comment",
                "text": "private body",
                "comment_digest": "digest",
            }
        )
        self.store.audit({"event": "executed", "action_type": "comment"})

        events = [
            json.loads(line)
            for line in (self.state_dir / "audit.jsonl")
            .read_text("utf-8")
            .splitlines()
        ]
        self.assertEqual(len(events), 2)
        self.assertNotIn("text", events[0])
        self.assertEqual(events[0]["comment_digest"], "digest")


class TokenBindingTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.now = 2_000.0
        self.config = load_config(None)
        self.client = FakeADBClient()
        self.store = ActionStore(
            Path(self.temporary_directory.name),
            token_ttl_seconds=self.config["token_ttl_seconds"],
            clock=lambda: self.now,
        )
        self.service = ActionService(
            self.client,
            self.config,
            self.store,
            clock=lambda: self.now,
            sleeper=lambda _seconds: None,
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_prepare_binds_device_and_config_without_adb_write(self):
        prepared = self.service.prepare("like")

        self.assertEqual(prepared["device_serial"], "DEVICE-1")
        self.assertEqual(prepared["target_package"], self.config["package"])
        self.assertEqual(len(prepared["config_fingerprint"]), 64)
        self.assertEqual(
            self.client.reads,
            [("foreground_package", "DEVICE-1"), "screen_size"],
        )
        self.assertEqual(self.client.writes, [])

    def test_prepare_rejects_changed_foreground_without_creating_token(self):
        self.client.foreground = "com.example.other"

        with self.assertRaises(ControlError) as raised:
            self.service.prepare("like")

        self.assertEqual(raised.exception.code, "ACTION_TARGET_CHANGED")
        self.assertEqual(self.client.writes, [])
        self.assertEqual(list((self.store.pending_dir).glob("*.json")), [])
        self.assertFalse(self.store.audit_path.exists())

    def test_execute_rejects_changed_foreground_after_consuming_token(self):
        prepared = self.service.prepare("like")
        self.client.foreground = "com.example.other"

        with self.assertRaises(ControlError) as raised:
            self.service.execute(prepared["token"])

        self.assertEqual(raised.exception.code, "ACTION_TARGET_CHANGED")
        self.assertEqual(self.client.writes, [])
        self.assertEqual(self.store.count("like"), 0)
        with self.assertRaises(ControlError) as replayed:
            self.service.execute(prepared["token"])
        self.assertEqual(replayed.exception.code, "TOKEN_REPLAYED")

    def test_prepare_uses_token_ttl_from_config(self):
        config = load_config(None)
        config["token_ttl_seconds"] = 45
        store = ActionStore(Path(self.temporary_directory.name) / "custom-ttl")
        service = ActionService(
            self.client,
            config,
            store,
            clock=lambda: self.now,
            sleeper=lambda _seconds: None,
        )

        prepared = service.prepare("like")

        self.assertEqual(prepared["expires_at"] - prepared["created_at"], 45)

    def test_wrong_device_is_rejected_and_token_stays_consumed(self):
        prepared = self.service.prepare("like")
        self.client.serial = "DEVICE-2"

        with self.assertRaises(ControlError) as raised:
            self.service.execute(prepared["token"])
        self.assertEqual(raised.exception.code, "TOKEN_DEVICE_MISMATCH")
        self.assertEqual(self.client.writes, [])

        self.client.serial = "DEVICE-1"
        with self.assertRaises(ControlError) as replayed:
            self.service.execute(prepared["token"])
        self.assertEqual(replayed.exception.code, "TOKEN_REPLAYED")

    def test_config_fingerprint_mismatch_is_rejected(self):
        prepared = self.service.prepare("follow")
        self.config["package"] = "example.changed"

        with self.assertRaises(ControlError) as raised:
            self.service.execute(prepared["token"])

        self.assertEqual(raised.exception.code, "TOKEN_CONFIG_MISMATCH")
        self.assertEqual(self.client.writes, [])

    def test_prepare_and_execute_pin_first_selected_serial_when_default_changes(self):
        client = ChangingSelectionClient()
        store = ActionStore(
            Path(self.temporary_directory.name) / "changing-device",
            token_ttl_seconds=self.config["token_ttl_seconds"],
            clock=lambda: self.now,
        )
        service = ActionService(
            client,
            self.config,
            store,
            clock=lambda: self.now,
            sleeper=lambda _seconds: None,
        )

        prepared = service.prepare("like")
        result = service.execute(prepared["token"])

        self.assertEqual(prepared["device_serial"], "DEVICE-A")
        self.assertEqual(result["status"], "executed")
        self.assertEqual(client.writes, [("tap", "DEVICE-A", 983, 1152)])
        self.assertEqual(
            client.reads,
            [
                ("select", "DEVICE-A"),
                ("foreground_package", "DEVICE-A"),
                ("screen_size", "DEVICE-A"),
                ("validate", "DEVICE-A"),
                ("foreground_package", "DEVICE-A"),
            ],
        )


class TokenSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temporary_directory.name)
        self.now = 2_500.0
        self.config = load_config(None)
        self.client = FakeADBClient()
        self.store = ActionStore(
            self.state_dir,
            token_ttl_seconds=self.config["token_ttl_seconds"],
            clock=lambda: self.now,
        )
        self.service = ActionService(
            self.client,
            self.config,
            self.store,
            clock=lambda: self.now,
            sleeper=lambda _seconds: None,
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_token_is_self_contained_and_pending_is_minimized(self):
        body = "private self contained comment"
        prepared = self.service.prepare("comment", text=body)
        parts = prepared["token"].split(".")

        self.assertEqual(len(parts), 2)
        payload = json.loads(self._decode_segment(parts[0]).decode("utf-8"))
        self.assertEqual(payload["jti"], prepared["jti"])
        self.assertEqual(payload["text"], body)
        self.assertEqual(payload["comment_digest"], hashlib.sha256(body.encode()).hexdigest())
        self.assertEqual(payload["device_serial"], "DEVICE-1")
        self.assertEqual(payload["target_package"], self.config["package"])
        self.assertIn("coordinate", payload)
        self.assertIn("created_at", payload)
        self.assertIn("expires_at", payload)

        pending_paths = list((self.state_dir / "pending").glob("*.json"))
        self.assertEqual(len(pending_paths), 1)
        pending_bytes = pending_paths[0].read_bytes()
        self.assertNotIn(body.encode("utf-8"), pending_bytes)
        self.assertNotIn(prepared["token"].encode("ascii"), pending_bytes)
        pending = json.loads(pending_bytes.decode("utf-8"))
        self.assertEqual(pending["jti"], prepared["jti"])
        self.assertEqual(pending["token_digest"], prepared["token_digest"])
        self.assertEqual(pending["expires_at"], prepared["expires_at"])
        self.assertEqual(pending["comment_digest"], prepared["comment_digest"])

        audit_bytes = (self.state_dir / "audit.jsonl").read_bytes()
        self.assertNotIn(body.encode("utf-8"), audit_bytes)
        self.assertNotIn(prepared["token"].encode("ascii"), audit_bytes)

    def test_hmac_key_and_state_directory_have_private_permissions(self):
        key_path = self.state_dir / "hmac.key"

        self.assertTrue(key_path.exists())
        self.assertEqual(stat.S_IMODE(self.state_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(key_path.stat().st_mode), 0o600)
        self.assertGreaterEqual(len(key_path.read_bytes()), 32)

    def test_payload_or_signature_tampering_is_rejected_before_adb(self):
        for segment_index in (0, 1):
            with self.subTest(segment_index=segment_index):
                prepared = self.service.prepare("like")
                parts = prepared["token"].split(".")
                self.assertEqual(len(parts), 2)
                parts[segment_index] = self._flip_first_character(parts[segment_index])
                tampered = ".".join(parts)
                writes_before = list(self.client.writes)

                with self.assertRaises(ControlError) as raised:
                    self.service.execute(tampered)

                self.assertEqual(raised.exception.code, "TOKEN_INVALID")
                self.assertEqual(self.client.writes, writes_before)

    def test_non_ascii_token_tampering_has_stable_error(self):
        prepared = self.service.prepare("like")
        _payload, signature = prepared["token"].split(".")

        with self.assertRaises(ControlError) as raised:
            self.service.execute("篡改." + signature)

        self.assertEqual(raised.exception.code, "TOKEN_INVALID")
        self.assertEqual(self.client.writes, [])

    def test_pending_metadata_tampering_is_rejected_before_adb(self):
        mutations = (
            lambda state: state.update(token_digest="0" * 64),
            lambda state: state.update(expires_at=state["expires_at"] + 60),
            lambda state: state.update(comment_digest="0" * 64),
            lambda state: state.update(action_type="tap"),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                prepared = self.service.prepare("comment", text="signed body")
                pending_path = next((self.state_dir / "pending").glob("*.json"))
                pending = json.loads(pending_path.read_text("utf-8"))
                mutate(pending)
                pending_path.write_text(json.dumps(pending), encoding="utf-8")
                writes_before = list(self.client.writes)

                with self.assertRaises(ControlError) as raised:
                    self.service.execute(prepared["token"])

                self.assertEqual(raised.exception.code, "TOKEN_INVALID")
                self.assertEqual(self.client.writes, writes_before)
                pending_path.unlink(missing_ok=True)

    def test_signed_comment_digest_mismatch_is_rejected_before_adb(self):
        valid = self.service.prepare("comment", text="digest checked body")
        payload = {
            key: valid[key]
            for key in (
                "action_type",
                "device_serial",
                "config_fingerprint",
                "coordinate",
                "text",
                "comment_input",
                "comment_send",
            )
        }
        payload["comment_digest"] = "0" * 64
        invalid = self.store.create(payload)

        with self.assertRaises(ControlError) as raised:
            self.service.execute(invalid["token"])

        self.assertEqual(raised.exception.code, "TOKEN_INVALID")
        self.assertEqual(self.client.writes, [])

    def test_used_and_all_audit_events_omit_body_and_raw_token(self):
        body = "body absent from durable logs"
        executed = self.service.prepare("comment", text=body)
        self.service.execute(executed["token"])
        canceled = self.service.prepare("tap", x_ratio=0.2, y_ratio=0.3)
        self.service.cancel(canceled["token"])

        used_bytes = b"\n".join(
            path.read_bytes() for path in (self.state_dir / "used").glob("*.json")
        )
        audit_bytes = (self.state_dir / "audit.jsonl").read_bytes()
        for durable_bytes in (used_bytes, audit_bytes):
            self.assertNotIn(body.encode("utf-8"), durable_bytes)
            self.assertNotIn(executed["token"].encode("ascii"), durable_bytes)
            self.assertNotIn(canceled["token"].encode("ascii"), durable_bytes)

    @staticmethod
    def _decode_segment(segment):
        padding = "=" * (-len(segment) % 4)
        return base64.urlsafe_b64decode(segment + padding)

    @staticmethod
    def _flip_first_character(segment):
        replacement = "A" if segment[0] != "A" else "B"
        return replacement + segment[1:]


class ExecutionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temporary_directory.name)
        self.now = 3_000.0
        self.config = load_config(None)
        self.timeline = []
        self.client = FakeADBClient(timeline=self.timeline)
        self.store = ActionStore(
            self.state_dir,
            token_ttl_seconds=self.config["token_ttl_seconds"],
            clock=lambda: self.now,
        )
        self.service = ActionService(
            self.client,
            self.config,
            self.store,
            clock=lambda: self.now,
            sleeper=lambda seconds: self.timeline.append(("wait", seconds)),
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_limit_is_checked_before_token_consumption(self):
        prepared = self.service.prepare("like")
        for _unused in range(self.config["limits"]["like"]):
            self.store.increment("like")

        with self.assertRaises(ControlError) as raised:
            self.service.execute(prepared["token"])

        self.assertEqual(raised.exception.code, "ACTION_LIMIT_EXCEEDED")
        self.assertEqual(self.client.writes, [])
        self.assertTrue(
            (self.state_dir / "pending" / (prepared["jti"] + ".json")).exists()
        )
        self.assertFalse(
            (self.state_dir / "used" / (prepared["jti"] + ".json")).exists()
        )

    def test_token_is_consumed_before_failed_adb_and_failure_does_not_increment(self):
        prepared = self.service.prepare("follow")
        used_path = self.state_dir / "used" / (prepared["jti"] + ".json")
        marker_seen_during_write = []
        self.client.write_observer = lambda: marker_seen_during_write.append(
            used_path.exists()
        )
        self.client.write_error = ControlError("ADB_COMMAND_FAILED", "tap failed")

        with self.assertRaises(ControlError) as raised:
            self.service.execute(prepared["token"])

        self.assertEqual(raised.exception.code, "ADB_COMMAND_FAILED")
        self.assertEqual(marker_seen_during_write, [True])
        self.assertEqual(self.store.count("follow"), 0)
        events = [
            json.loads(line)["event"]
            for line in self.store.audit_path.read_text("utf-8").splitlines()
        ]
        self.assertEqual(
            events,
            ["prepared", "execution_reserved", "execution_failed"],
        )
        with self.assertRaises(ControlError) as replayed:
            self.service.execute(prepared["token"])
        self.assertEqual(replayed.exception.code, "TOKEN_REPLAYED")

    def test_like_timeout_keeps_quota_and_reports_unknown_outcome(self):
        prepared = self.service.prepare("like")
        self.client.write_error = ControlError(
            "ADB_COMMAND_TIMEOUT",
            "Android Debug Bridge command timed out",
        )

        with self.assertRaises(ControlError) as raised:
            self.service.execute(prepared["token"])

        self.assertEqual(raised.exception.code, "ACTION_OUTCOME_UNKNOWN")
        hint = raised.exception.hint.lower()
        self.assertIn("token", hint)
        self.assertIn("consumed", hint)
        self.assertIn("quota", hint)
        self.assertIn("held", hint)
        self.assertIn("do not retry", hint)
        self.assertIn("inspect the device", hint)
        self.assertEqual(self.store.count("like"), 1)
        events = [
            json.loads(line)["event"]
            for line in self.store.audit_path.read_text("utf-8").splitlines()
        ]
        self.assertEqual(
            events,
            ["prepared", "execution_reserved", "execution_outcome_unknown"],
        )
        self.assertNotIn("execution_failed", events)
        with self.assertRaises(ControlError) as replayed:
            self.service.execute(prepared["token"])
        self.assertEqual(replayed.exception.code, "TOKEN_REPLAYED")

    def test_comment_send_timeout_keeps_quota_and_private_unknown_audit(self):
        body = "private timeout comment"
        prepared = self.service.prepare("comment", text=body)
        original_tap = self.client.tap
        tap_count = 0

        def timeout_on_send(x, y):
            nonlocal tap_count
            tap_count += 1
            original_tap(x, y)
            if tap_count == 3:
                raise ControlError(
                    "ADB_COMMAND_TIMEOUT",
                    "Android Debug Bridge command timed out",
                )

        self.client.tap = timeout_on_send

        with self.assertRaises(ControlError) as raised:
            self.service.execute(prepared["token"])

        self.assertEqual(raised.exception.code, "ACTION_OUTCOME_UNKNOWN")
        self.assertEqual(tap_count, 3)
        self.assertEqual(self.store.count("comment"), 1)
        audit_text = self.store.audit_path.read_text("utf-8")
        self.assertNotIn(body, audit_text)
        self.assertNotIn(prepared["token"], audit_text)
        events = [json.loads(line)["event"] for line in audit_text.splitlines()]
        self.assertEqual(
            events,
            ["prepared", "execution_reserved", "execution_outcome_unknown"],
        )

    def test_unknown_outcome_audit_failure_keeps_quota_and_unknown_error(self):
        prepared = self.service.prepare("like")
        self.client.write_error = ControlError(
            "ADB_COMMAND_TIMEOUT",
            "Android Debug Bridge command timed out",
        )
        audit = self.store.audit
        attempted_events = []

        def fail_unknown_audit(event):
            attempted_events.append(event["event"])
            if event.get("event") == "execution_outcome_unknown":
                raise ControlError("STATE_ERROR", "unknown outcome audit failed")
            return audit(event)

        with patch.object(self.store, "audit", side_effect=fail_unknown_audit):
            with self.assertRaises(ControlError) as raised:
                self.service.execute(prepared["token"])

        self.assertEqual(raised.exception.code, "ACTION_OUTCOME_UNKNOWN")
        self.assertEqual(
            attempted_events,
            ["execution_reserved", "execution_outcome_unknown"],
        )
        self.assertEqual(self.store.count("like"), 1)
        events = [
            json.loads(line)["event"]
            for line in self.store.audit_path.read_text("utf-8").splitlines()
        ]
        self.assertEqual(events, ["prepared", "execution_reserved"])
        with self.assertRaises(ControlError) as replayed:
            self.service.execute(prepared["token"])
        self.assertEqual(replayed.exception.code, "TOKEN_REPLAYED")

    def test_counter_prewrite_failure_prevents_any_adb_write(self):
        prepared = self.service.prepare("like")
        atomic_write = self.store._atomic_write_json

        def fail_counter(path, value):
            if path == self.store.counters_path:
                raise ControlError("STATE_ERROR", "counter prewrite failed")
            return atomic_write(path, value)

        with patch.object(self.store, "_atomic_write_json", side_effect=fail_counter):
            with self.assertRaises(ControlError) as raised:
                self.service.execute(prepared["token"])

        self.assertEqual(raised.exception.code, "STATE_ERROR")
        self.assertEqual(self.client.writes, [])
        with self.assertRaises(ControlError) as replayed:
            self.service.execute(prepared["token"])
        self.assertEqual(replayed.exception.code, "TOKEN_REPLAYED")

    def test_execution_reservation_is_audited_before_adb_write(self):
        prepared = self.service.prepare("like")
        events_seen_during_write = []

        def observe_audit():
            events_seen_during_write.append(
                [
                    json.loads(line)["event"]
                    for line in self.store.audit_path.read_text("utf-8").splitlines()
                ]
            )

        self.client.write_observer = observe_audit

        result = self.service.execute(prepared["token"])

        self.assertEqual(events_seen_during_write, [["prepared", "execution_reserved"]])
        self.assertEqual(result["count"], 1)

    def test_reservation_audit_prewrite_failure_rolls_back_without_adb(self):
        prepared = self.service.prepare("like")
        audit = self.store.audit

        def fail_reserved(event):
            if event.get("event") == "execution_reserved":
                raise ControlError("STATE_ERROR", "reservation audit failed")
            return audit(event)

        with patch.object(self.store, "audit", side_effect=fail_reserved):
            with self.assertRaises(ControlError) as raised:
                self.service.execute(prepared["token"])

        self.assertEqual(raised.exception.code, "STATE_ERROR")
        self.assertEqual(self.client.writes, [])
        self.assertEqual(self.store.count("like"), 0)
        with self.assertRaises(ControlError) as replayed:
            self.service.execute(prepared["token"])
        self.assertEqual(replayed.exception.code, "TOKEN_REPLAYED")

    def test_successful_adb_with_final_audit_failure_returns_executed_warning(self):
        prepared = self.service.prepare("comment", text="private success body")
        audit = self.store.audit

        def fail_executed(event):
            if event.get("event") == "executed":
                raise ControlError("STATE_ERROR", "final audit failed")
            return audit(event)

        with patch.object(self.store, "audit", side_effect=fail_executed):
            result = self.service.execute(prepared["token"])

        self.assertEqual(result["status"], "executed")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["audit_status"], "failed")
        self.assertIn("audit", result["state_warning"].lower())
        self.assertNotIn(prepared["token"], result["state_warning"])
        self.assertNotIn("private success body", result["state_warning"])
        self.assertEqual(self.store.count("comment"), 1)

    def test_unexpected_crash_after_reservation_keeps_conservative_count(self):
        prepared = self.service.prepare("like")
        self.client.write_error = RuntimeError("simulated process crash")

        with self.assertRaises(RuntimeError):
            self.service.execute(prepared["token"])

        self.assertEqual(self.store.count("like"), 1)
        events = [
            json.loads(line)["event"]
            for line in self.store.audit_path.read_text("utf-8").splitlines()
        ]
        self.assertEqual(events, ["prepared", "execution_reserved"])
        with self.assertRaises(ControlError) as replayed:
            self.service.execute(prepared["token"])
        self.assertEqual(replayed.exception.code, "TOKEN_REPLAYED")

    def test_adb_failure_with_rollback_error_is_fail_closed(self):
        prepared = self.service.prepare("follow")
        self.client.write_error = ControlError("ADB_COMMAND_FAILED", "tap failed")

        with patch.object(
            self.store,
            "_decrement_unlocked",
            side_effect=ControlError("STATE_ERROR", "rollback failed"),
        ):
            with self.assertRaises(ControlError) as raised:
                self.service.execute(prepared["token"])

        self.assertEqual(raised.exception.code, "STATE_ERROR")
        self.assertEqual(self.store.count("follow"), 1)
        with self.assertRaises(ControlError) as replayed:
            self.service.execute(prepared["token"])
        self.assertEqual(replayed.exception.code, "TOKEN_REPLAYED")

    def test_adb_failure_with_failure_audit_error_is_fail_closed(self):
        prepared = self.service.prepare("follow")
        self.client.write_error = ControlError("ADB_COMMAND_FAILED", "tap failed")
        audit = self.store.audit

        def fail_execution_failed(event):
            if event.get("event") == "execution_failed":
                raise ControlError("STATE_ERROR", "failure audit failed")
            return audit(event)

        with patch.object(self.store, "audit", side_effect=fail_execution_failed):
            with self.assertRaises(ControlError) as raised:
                self.service.execute(prepared["token"])

        self.assertEqual(raised.exception.code, "STATE_ERROR")
        self.assertEqual(self.store.count("follow"), 0)
        with self.assertRaises(ControlError) as replayed:
            self.service.execute(prepared["token"])
        self.assertEqual(replayed.exception.code, "TOKEN_REPLAYED")

    def test_like_follow_and_tap_each_execute_one_tap(self):
        cases = (
            ("like", {}, (983, 1152)),
            ("follow", {}, (983, 840)),
            ("tap", {"x_ratio": 0.25, "y_ratio": 0.75}, (270, 1800)),
        )
        for action_type, arguments, expected in cases:
            with self.subTest(action_type=action_type):
                before = len(self.client.writes)
                prepared = self.service.prepare(action_type, **arguments)

                result = self.service.execute(prepared["token"])

                self.assertEqual(
                    self.client.writes[before:], [("tap", expected[0], expected[1])]
                )
                self.assertEqual(result["status"], "executed")
                self.assertEqual(result["action_type"], action_type)
                self.assertEqual(self.store.count(action_type), 1)

    def test_prepare_uses_named_coordinate_and_explicit_ratios_override_it(self):
        named = self.service.prepare("like")
        explicit = self.service.prepare("like", x_ratio=0.25, y_ratio=1.0)

        self.assertEqual(
            named["coordinate"],
            {
                "normalized": {"x": 0.91, "y": 0.48},
                "pixel": {"x": 983, "y": 1152},
            },
        )
        self.assertEqual(
            explicit["coordinate"],
            {
                "normalized": {"x": 0.25, "y": 1.0},
                "pixel": {"x": 270, "y": 2399},
            },
        )

    def test_comment_executes_configured_sequence_with_bounded_waits(self):
        prepared = self.service.prepare("comment", text="hello world")

        result = self.service.execute(prepared["token"])

        wait = self.config["comment_wait_seconds"]
        self.assertEqual(
            self.timeline,
            [
                ("tap", 983, 1392),
                ("wait", wait),
                ("tap", 540, 2256),
                ("wait", wait),
                ("input_text", "hello world", "adb"),
                ("wait", wait),
                ("tap", 1004, 2256),
            ],
        )
        self.assertEqual(result["status"], "executed")
        self.assertEqual(self.store.count("comment"), 1)

    def test_comment_stores_digest_and_audit_never_stores_body(self):
        prepared = self.service.prepare("comment", text="private comment body")

        self.service.execute(prepared["token"])

        self.assertEqual(
            prepared["comment_digest"],
            hashlib.sha256(b"private comment body").hexdigest(),
        )
        audit_text = (self.state_dir / "audit.jsonl").read_text("utf-8")
        self.assertNotIn("private comment body", audit_text)
        events = [json.loads(line) for line in audit_text.splitlines()]
        self.assertEqual(
            [event["event"] for event in events],
            ["prepared", "execution_reserved", "executed"],
        )
        self.assertTrue(all("text" not in event for event in events))

    def test_utf8_comment_auto_backend_resolves_to_adb_keyboard(self):
        prepared = self.service.prepare("comment", text="你好，抖音")

        self.service.execute(prepared["token"])

        self.assertIn(("input_text", "你好，抖音", "adb-keyboard"), self.client.writes)

    def test_comment_rejects_unavailable_adb_keyboard_before_first_tap(self):
        self.client.keyboard_status["selected"] = False
        self.client.keyboard_status["available"] = False
        prepared = self.service.prepare("comment", text="你好，抖音")

        with self.assertRaises(ControlError) as raised:
            self.service.execute(prepared["token"])

        self.assertEqual(raised.exception.code, "UNSUPPORTED_TEXT_INPUT")
        self.assertEqual(self.client.writes, [])
        self.assertEqual(self.store.count("comment"), 0)
        events = [
            json.loads(line)["event"]
            for line in self.store.audit_path.read_text("utf-8").splitlines()
        ]
        self.assertEqual(events, ["prepared"])

    def test_utf8_comment_is_rejected_for_plain_adb_backend(self):
        self.config["input_backend"] = "adb"

        with self.assertRaises(ControlError) as raised:
            self.service.prepare("comment", text="你好")

        self.assertEqual(raised.exception.code, "UNSUPPORTED_TEXT_INPUT")
        self.assertEqual(list((self.state_dir / "pending").iterdir()), [])

    def test_cancel_marks_token_used_without_adb_write(self):
        prepared = self.service.prepare("tap", x_ratio=0.1, y_ratio=0.2)

        result = self.service.cancel(prepared["token"])

        self.assertEqual(result["status"], "canceled")
        self.assertEqual(self.client.writes, [])
        with self.assertRaises(ControlError) as replayed:
            self.service.execute(prepared["token"])
        self.assertEqual(replayed.exception.code, "TOKEN_REPLAYED")

    def test_concurrent_limit_one_allows_at_most_one_adb_write(self):
        config = load_config(None)
        config["limits"]["like"] = 1
        tap_barrier = threading.Barrier(2)
        start_barrier = threading.Barrier(3)
        client = BarrierADBClient(tap_barrier)
        store = ActionStore(
            self.state_dir / "concurrent",
            token_ttl_seconds=config["token_ttl_seconds"],
            clock=lambda: self.now,
        )
        service = ActionService(
            client,
            config,
            store,
            clock=lambda: self.now,
            sleeper=lambda _seconds: None,
        )
        tokens = [service.prepare("like")["token"] for _unused in range(2)]
        results = []
        errors = []

        def execute(token):
            start_barrier.wait()
            try:
                results.append(service.execute(token))
            except ControlError as error:
                errors.append(error)

        threads = [threading.Thread(target=execute, args=(token,)) for token in tokens]
        for thread in threads:
            thread.start()
        start_barrier.wait()
        for thread in threads:
            thread.join(timeout=3.0)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(client.writes), 1)
        self.assertEqual(len(results), 1)
        self.assertEqual([error.code for error in errors], ["ACTION_LIMIT_EXCEEDED"])
        self.assertEqual(store.count("like"), 1)


if __name__ == "__main__":
    unittest.main()
