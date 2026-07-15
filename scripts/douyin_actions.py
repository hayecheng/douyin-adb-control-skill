import base64
import copy
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from douyin_core import ADBClient, ControlError, ratio_to_pixel


ACTION_TYPES = ("like", "follow", "comment", "tap")
INPUT_BACKENDS = ("auto", "adb", "adb-keyboard")
POSITION_NAMES = (
    "like",
    "follow",
    "comment_button",
    "comment_input",
    "comment_send",
)

DEFAULT_CONFIG = {
    "schema_version": 1,
    "package": "com.ss.android.ugc.aweme",
    "swipes": {
        "next": {
            "start": [0.5, 0.75],
            "end": [0.5, 0.25],
            "duration_ms": 350,
        },
        "previous": {
            "start": [0.5, 0.25],
            "end": [0.5, 0.75],
            "duration_ms": 350,
        },
    },
    "positions": {
        "like": [0.91, 0.48],
        "follow": [0.91, 0.35],
        "comment_button": [0.91, 0.58],
        "comment_input": [0.5, 0.94],
        "comment_send": [0.93, 0.94],
    },
    "limits": {"like": 3, "follow": 1, "comment": 1, "tap": 3},
    "token_ttl_seconds": 300,
    "input_backend": "auto",
    "comment_wait_seconds": 0.25,
}


def default_config() -> Dict[str, Any]:
    return copy.deepcopy(DEFAULT_CONFIG)


def load_config(path: Optional[Path]) -> Dict[str, Any]:
    if path is None:
        config = default_config()
    else:
        try:
            config = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ControlError(
                "INVALID_CONFIG", "Unable to read valid JSON configuration"
            ) from error
    validate_config(config)
    return config


def validate_config(config: Dict[str, Any]) -> None:
    if not isinstance(config, dict):
        _invalid("Configuration must be a JSON object")
    if config.get("schema_version") != 1 or isinstance(
        config.get("schema_version"), bool
    ):
        _invalid("Configuration schema_version must be 1")
    package = config.get("package")
    if not isinstance(package, str) or re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+",
        package,
    ) is None:
        _invalid("Configuration package must be a safe Android application ID")

    swipes = config.get("swipes")
    if not isinstance(swipes, dict) or not all(
        name in swipes for name in ("next", "previous")
    ):
        _invalid("Configuration requires next and previous swipes")
    for name in ("next", "previous"):
        swipe = swipes[name]
        if not isinstance(swipe, dict):
            _invalid("Swipe configuration must be an object")
        _validate_coordinate(swipe.get("start"), "%s swipe start" % name)
        _validate_coordinate(swipe.get("end"), "%s swipe end" % name)
        duration = swipe.get("duration_ms")
        if isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0:
            _invalid("Swipe duration_ms must be a positive integer")

    positions = config.get("positions", {})
    if not isinstance(positions, dict):
        _invalid("Configuration positions must be an object")
    for name, coordinate in positions.items():
        if name not in POSITION_NAMES:
            _invalid("Configuration contains an unsupported named position")
        _validate_coordinate(coordinate, "%s position" % name)

    limits = config.get("limits")
    if not isinstance(limits, dict) or set(limits) != set(ACTION_TYPES):
        _invalid("Configuration limits must define every supported action")
    for limit in limits.values():
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            _invalid("Action limits must be non-negative integers")

    ttl = config.get("token_ttl_seconds")
    if not _is_positive_number(ttl):
        _invalid("Configuration token_ttl_seconds must be positive")
    if config.get("input_backend") not in INPUT_BACKENDS:
        _invalid("Configuration input_backend is not supported")

    wait = config.get("comment_wait_seconds", 0.25)
    if not _is_number(wait) or not 0.0 <= float(wait) <= 5.0:
        _invalid("Configuration comment_wait_seconds must be between 0 and 5")


def _validate_coordinate(value: Any, name: str) -> None:
    if not isinstance(value, list) or len(value) != 2:
        _invalid("%s must contain two coordinate ratios" % name)
    if not all(_is_number(part) and 0.0 <= float(part) <= 1.0 for part in value):
        _invalid("%s ratios must be between 0 and 1" % name)


def _is_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _is_positive_number(value: Any) -> bool:
    return _is_number(value) and float(value) > 0.0


def _invalid(message: str) -> None:
    raise ControlError("INVALID_CONFIG", message)


class ActionStore:
    def __init__(
        self,
        state_dir: Path,
        token_ttl_seconds: float = 300,
        clock: Callable[[], float] = time.time,
    ):
        if not _is_positive_number(token_ttl_seconds):
            raise ControlError("INVALID_CONFIG", "Token lifetime must be positive")
        self.state_dir = Path(state_dir)
        self.pending_dir = self.state_dir / "pending"
        self.used_dir = self.state_dir / "used"
        self.counters_path = self.state_dir / "counters.json"
        self.audit_path = self.state_dir / "audit.jsonl"
        self.key_path = self.state_dir / "hmac.key"
        self.policy_lock_path = self.state_dir / ".execution-policy.lock"
        self.token_ttl_seconds = float(token_ttl_seconds)
        self.clock = clock
        self._policy_thread_lock = threading.Lock()
        try:
            self.state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
            os.chmod(self.state_dir, 0o700)
            self.pending_dir.mkdir(mode=0o700, exist_ok=True)
            self.used_dir.mkdir(mode=0o700, exist_ok=True)
        except OSError as error:
            raise ControlError("STATE_ERROR", "Unable to initialize action state") from error
        self._hmac_key = self._load_or_create_key()

    def create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise ControlError("INVALID_ACTION", "Action payload must be an object")
        for _attempt in range(10):
            jti = secrets.token_urlsafe(32)
            if not self._pending_path(jti).exists() and not self._used_path(jti).exists():
                break
        else:
            raise ControlError("STATE_ERROR", "Unable to allocate an action token")
        created_at = float(self.clock())
        signed_payload = copy.deepcopy(payload)
        for reserved in ("token", "token_digest", "jti", "created_at", "expires_at"):
            signed_payload.pop(reserved, None)
        signed_payload.update(
            {
                "jti": jti,
                "created_at": created_at,
                "expires_at": created_at + self.token_ttl_seconds,
            }
        )
        token = self._encode_token(signed_payload)
        record = dict(signed_payload)
        record["token"] = token
        record["token_digest"] = self._token_digest(token)
        self._atomic_write_json(
            self._pending_path(jti), self._state_marker(record, "pending")
        )
        return record

    def consume(self, token: str, now: float) -> Dict[str, Any]:
        return self._take(token, now, "consumed")

    def cancel(self, token: str, now: float) -> Dict[str, Any]:
        record = self._take(token, now, "canceled")
        return {
            "jti": record["jti"],
            "token_digest": record["token_digest"],
            "action_type": record.get("action_type"),
            "status": "canceled",
            "canceled_at": float(now),
        }

    def count(self, action_type: str) -> int:
        with self.execution_policy():
            return self._count_unlocked(action_type)

    def increment(self, action_type: str) -> None:
        with self.execution_policy():
            self._increment_unlocked(action_type)

    @contextmanager
    def execution_policy(self):
        with self._policy_thread_lock:
            descriptor = self._acquire_policy_file_lock()
            try:
                yield
            finally:
                os.close(descriptor)
                self.policy_lock_path.unlink(missing_ok=True)

    def _count_unlocked(self, action_type: str) -> int:
        counters = self._read_counters()
        value = counters.get(action_type, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ControlError("STATE_ERROR", "Action counters are invalid")
        return value

    def _increment_unlocked(self, action_type: str) -> int:
        counters = self._read_counters()
        value = counters.get(action_type, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ControlError("STATE_ERROR", "Action counters are invalid")
        counters[action_type] = value + 1
        self._atomic_write_json(self.counters_path, counters)
        return counters[action_type]

    def _decrement_unlocked(self, action_type: str) -> int:
        counters = self._read_counters()
        value = counters.get(action_type, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ControlError("STATE_ERROR", "Action counter reservation is invalid")
        counters[action_type] = value - 1
        self._atomic_write_json(self.counters_path, counters)
        return counters[action_type]

    def audit(self, event: Dict[str, Any]) -> None:
        if not isinstance(event, dict):
            raise ControlError("STATE_ERROR", "Audit event must be an object")
        minimized = self._minimize_event(event)
        encoded = (
            json.dumps(minimized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        try:
            descriptor = os.open(
                str(self.audit_path),
                os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                0o600,
            )
            try:
                os.write(descriptor, encoded)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as error:
            raise ControlError("STATE_ERROR", "Unable to append the audit log") from error

    def _take(self, token: str, now: float, status: str) -> Dict[str, Any]:
        record = self._decode_token(token)
        self._validate_pending_state(record)
        jti = record["jti"]
        pending_path = self._pending_path(jti)

        claim_path = self.used_dir / (jti + ".lock")
        claim_descriptor = self._acquire_lock(
            claim_path,
            code="TOKEN_REPLAYED",
            message="Action token is already being consumed",
        )
        try:
            self._validate_pending_state(record)
            if float(now) >= float(record["expires_at"]):
                self._write_used_marker(record, "expired", float(now))
                pending_path.unlink(missing_ok=True)
                raise ControlError("TOKEN_EXPIRED", "Action token has expired")
            self._write_used_marker(record, status, float(now))
            pending_path.unlink(missing_ok=True)
            return record
        finally:
            os.close(claim_descriptor)
            claim_path.unlink(missing_ok=True)

    def _read_pending(self, token: str) -> Dict[str, Any]:
        record = self._decode_token(token)
        self._validate_pending_state(record)
        return record

    def _write_used_marker(
        self, record: Dict[str, Any], status: str, used_at: float
    ) -> None:
        marker = {
            "jti": record["jti"],
            "token_digest": record["token_digest"],
            "action_type": record.get("action_type"),
            "status": status,
            "used_at": used_at,
            "created_at": record["created_at"],
            "expires_at": record["expires_at"],
        }
        if record.get("comment_digest") is not None:
            marker["comment_digest"] = record["comment_digest"]
        self._atomic_write_json(self._used_path(record["jti"]), marker)

    def _validate_pending_state(self, record: Dict[str, Any]) -> None:
        jti = record["jti"]
        if self._used_path(jti).exists():
            raise ControlError("TOKEN_REPLAYED", "Action token was already consumed")
        path = self._pending_path(jti)
        if not path.exists():
            raise ControlError("TOKEN_NOT_FOUND", "Action token was not found")
        state = self._read_json(path)
        expected = self._state_marker(record, "pending")
        if state != expected:
            raise ControlError("TOKEN_INVALID", "Action token state is inconsistent")

    @staticmethod
    def _state_marker(record: Dict[str, Any], status: str) -> Dict[str, Any]:
        marker = {
            "jti": record["jti"],
            "token_digest": record["token_digest"],
            "action_type": record.get("action_type"),
            "status": status,
            "created_at": record["created_at"],
            "expires_at": record["expires_at"],
        }
        if record.get("comment_digest") is not None:
            marker["comment_digest"] = record["comment_digest"]
        return marker

    def _encode_token(self, payload: Dict[str, Any]) -> str:
        payload_bytes = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        payload_segment = self._encode_segment(payload_bytes)
        signature = hmac.new(
            self._hmac_key, payload_segment.encode("ascii"), hashlib.sha256
        ).digest()
        return payload_segment + "." + self._encode_segment(signature)

    def _decode_token(self, token: str) -> Dict[str, Any]:
        if not isinstance(token, str) or token.count(".") != 1:
            raise ControlError("TOKEN_INVALID", "Action token is invalid")
        payload_segment, signature_segment = token.split(".")
        try:
            signature = self._decode_segment(signature_segment)
            payload_ascii = payload_segment.encode("ascii")
        except (UnicodeError, ValueError, TypeError) as error:
            raise ControlError("TOKEN_INVALID", "Action token is invalid") from error
        expected = hmac.new(
            self._hmac_key, payload_ascii, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(signature, expected):
            raise ControlError("TOKEN_INVALID", "Action token signature is invalid")
        try:
            payload = json.loads(self._decode_segment(payload_segment).decode("utf-8"))
        except (UnicodeError, ValueError, TypeError) as error:
            raise ControlError("TOKEN_INVALID", "Action token payload is invalid") from error
        if not isinstance(payload, dict):
            raise ControlError("TOKEN_INVALID", "Action token payload is invalid")
        jti = payload.get("jti")
        if not isinstance(jti, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,256}", jti) is None:
            raise ControlError("TOKEN_INVALID", "Action token payload is invalid")
        if not _is_number(payload.get("created_at")) or not _is_number(
            payload.get("expires_at")
        ):
            raise ControlError("TOKEN_INVALID", "Action token payload is invalid")
        if payload.get("action_type") not in ACTION_TYPES:
            raise ControlError("TOKEN_INVALID", "Action token payload is invalid")
        record = dict(payload)
        record["token"] = token
        record["token_digest"] = self._token_digest(token)
        return record

    @staticmethod
    def _encode_segment(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    @staticmethod
    def _decode_segment(value: str) -> bytes:
        if not isinstance(value, str):
            raise ValueError("Token segment must be text")
        padding = "=" * (-len(value) % 4)
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)

    @staticmethod
    def _token_digest(token: str) -> str:
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def _load_or_create_key(self) -> bytes:
        if not self.key_path.exists():
            temporary_name = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=str(self.state_dir),
                    prefix=".hmac.key.",
                    delete=False,
                ) as temporary:
                    temporary_name = temporary.name
                    os.chmod(temporary_name, 0o600)
                    temporary.write(secrets.token_bytes(32))
                    temporary.flush()
                    os.fsync(temporary.fileno())
                try:
                    os.link(temporary_name, self.key_path)
                except FileExistsError:
                    pass
            except OSError as error:
                raise ControlError("STATE_ERROR", "Unable to create HMAC key") from error
            finally:
                if temporary_name is not None:
                    Path(temporary_name).unlink(missing_ok=True)
        try:
            os.chmod(self.key_path, 0o600)
            key = self.key_path.read_bytes()
        except OSError as error:
            raise ControlError("STATE_ERROR", "Unable to read HMAC key") from error
        if len(key) < 32:
            raise ControlError("STATE_ERROR", "Stored HMAC key is invalid")
        return key

    def _read_counters(self) -> Dict[str, Any]:
        if not self.counters_path.exists():
            return {}
        counters = self._read_json(self.counters_path)
        if not isinstance(counters, dict):
            raise ControlError("STATE_ERROR", "Action counters are invalid")
        return counters

    def _read_json(self, path: Path) -> Dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ControlError("STATE_ERROR", "Stored action state is invalid") from error
        if not isinstance(value, dict):
            raise ControlError("STATE_ERROR", "Stored action state is invalid")
        return value

    @staticmethod
    def _minimize_event(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: ActionStore._minimize_event(item)
                for key, item in value.items()
                if key not in ("text", "comment_text", "token")
            }
        if isinstance(value, list):
            return [ActionStore._minimize_event(item) for item in value]
        return value

    @staticmethod
    def _atomic_write_json(path: Path, value: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_name = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(path.parent),
                prefix="." + path.name + ".",
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                json.dump(
                    value,
                    temporary,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, path)
        except OSError as error:
            raise ControlError("STATE_ERROR", "Unable to persist action state") from error
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)

    @staticmethod
    def _acquire_lock(
        path: Path,
        code: str = "STATE_BUSY",
        message: str = "Action state is busy",
    ) -> int:
        try:
            return os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as error:
            raise ControlError(code, message) from error
        except OSError as error:
            raise ControlError("STATE_ERROR", "Unable to lock action state") from error

    def _acquire_policy_file_lock(self) -> int:
        deadline = time.monotonic() + 10.0
        while True:
            try:
                return os.open(
                    str(self.policy_lock_path),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError as error:
                if time.monotonic() >= deadline:
                    raise ControlError(
                        "STATE_BUSY", "Action execution policy is busy"
                    ) from error
                time.sleep(0.01)
            except OSError as error:
                raise ControlError(
                    "STATE_ERROR", "Unable to lock action execution policy"
                ) from error

    def _pending_path(self, jti: str) -> Path:
        return self.pending_dir / (jti + ".json")

    def _used_path(self, jti: str) -> Path:
        return self.used_dir / (jti + ".json")


class ActionService:
    def __init__(
        self,
        client: ADBClient,
        config: Dict[str, Any],
        store: ActionStore,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        validate_config(config)
        self.client = client
        self.config = config
        self.store = store
        self.store.token_ttl_seconds = float(config["token_ttl_seconds"])
        self.clock = clock
        self.sleeper = sleeper

    def prepare(
        self,
        action_type: str,
        *,
        x_ratio: Optional[float] = None,
        y_ratio: Optional[float] = None,
        text: Optional[str] = None,
    ) -> Dict[str, Any]:
        if action_type not in ACTION_TYPES:
            raise ControlError("INVALID_ACTION", "Action type is not supported")
        if (x_ratio is None) != (y_ratio is None):
            raise ControlError(
                "INVALID_COORDINATE", "Both coordinate ratios must be provided together"
            )
        if action_type == "comment":
            if not isinstance(text, str) or not text:
                raise ControlError("INVALID_ACTION", "Comment text is required")
            if self.config["input_backend"] == "adb" and not self._safe_adb_text(text):
                raise ControlError(
                    "UNSUPPORTED_TEXT_INPUT",
                    "UTF-8 or shell-sensitive comment text requires ADB Keyboard",
                )
        elif text is not None:
            raise ControlError("INVALID_ACTION", "Text is only valid for comment actions")

        serial = self._selected_serial()
        pinned_client = self._pinned_client(serial)
        self._validate_target_package(pinned_client, self.config["package"])
        width, height = pinned_client.screen_size()
        position_name = "comment_button" if action_type == "comment" else action_type
        coordinate = self._coordinate(
            position_name, width, height, x_ratio=x_ratio, y_ratio=y_ratio
        )
        payload = {
            "action_type": action_type,
            "device_serial": serial,
            "target_package": self.config["package"],
            "config_fingerprint": self._config_fingerprint(),
            "coordinate": coordinate,
        }
        if action_type == "comment":
            payload.update(
                {
                    "text": text,
                    "comment_digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "comment_input": self._coordinate(
                        "comment_input", width, height
                    ),
                    "comment_send": self._coordinate("comment_send", width, height),
                }
            )
        record = self.store.create(payload)
        self.store.audit(
            {
                "event": "prepared",
                "action_type": action_type,
                "jti": record["jti"],
                "token_digest": record["token_digest"],
                "created_at": record["created_at"],
                "expires_at": record["expires_at"],
                "comment_digest": record.get("comment_digest"),
            }
        )
        return record

    def cancel(self, token: str) -> Dict[str, Any]:
        result = self.store.cancel(token, now=float(self.clock()))
        self.store.audit(
            {
                "event": "canceled",
                "action_type": result.get("action_type"),
                "jti": result["jti"],
                "token_digest": result["token_digest"],
                "canceled_at": result["canceled_at"],
            }
        )
        return result

    def execute(self, token: str) -> Dict[str, Any]:
        with self.store.execution_policy():
            return self._execute_with_policy(token)

    def _execute_with_policy(self, token: str) -> Dict[str, Any]:
        pending = self.store._read_pending(token)
        action_type = pending.get("action_type")
        if action_type not in ACTION_TYPES:
            raise ControlError("INVALID_ACTION", "Stored action type is not supported")
        if self.store._count_unlocked(action_type) >= self.config["limits"][action_type]:
            raise ControlError("ACTION_LIMIT_EXCEEDED", "Action limit has been reached")
        record = self.store.consume(token, now=float(self.clock()))
        if action_type == "comment":
            actual_digest = hashlib.sha256(record["text"].encode("utf-8")).hexdigest()
            if not hmac.compare_digest(actual_digest, record.get("comment_digest", "")):
                raise ControlError("TOKEN_INVALID", "Comment digest does not match token")
        token_serial = record.get("device_serial")
        if self.client.serial is not None and token_serial != self.client.serial:
            raise ControlError(
                "TOKEN_DEVICE_MISMATCH",
                "Action token belongs to a different Android device",
            )
        if record.get("config_fingerprint") != self._config_fingerprint():
            raise ControlError(
                "TOKEN_CONFIG_MISMATCH",
                "Action token belongs to a different configuration",
            )
        target_package = record.get("target_package")
        if target_package != self.config["package"]:
            raise ControlError(
                "TOKEN_CONFIG_MISMATCH",
                "Action token belongs to a different target package",
            )
        pinned_client = self._pinned_client(token_serial)
        pinned_client.selected_device()
        self._validate_target_package(pinned_client, target_package)
        comment_backend = None
        if action_type == "comment":
            comment_backend = self._comment_backend(record, pinned_client)
        new_count = self.store._increment_unlocked(action_type)
        try:
            self.store.audit(
                {
                    "event": "execution_reserved",
                    "action_type": action_type,
                    "jti": record["jti"],
                    "token_digest": record["token_digest"],
                    "reserved_at": float(self.clock()),
                    "count": new_count,
                    "comment_digest": record.get("comment_digest"),
                }
            )
        except ControlError:
            self.store._decrement_unlocked(action_type)
            raise
        try:
            if action_type == "comment":
                self._execute_comment(record, pinned_client, comment_backend)
            else:
                pixel = record["coordinate"]["pixel"]
                pinned_client.tap(pixel["x"], pixel["y"])
        except ControlError as error:
            if error.code == "ADB_COMMAND_TIMEOUT":
                try:
                    self.store.audit(
                        {
                            "event": "execution_outcome_unknown",
                            "action_type": action_type,
                            "jti": record["jti"],
                            "token_digest": record["token_digest"],
                            "error_code": error.code,
                            "observed_at": float(self.clock()),
                            "comment_digest": record.get("comment_digest"),
                        }
                    )
                except ControlError:
                    pass
                raise ControlError(
                    "ACTION_OUTCOME_UNKNOWN",
                    "The Android action outcome is unknown after an ADB timeout",
                    "The token was consumed and quota is held; do not retry. "
                    "Inspect the device before taking further action",
                ) from error
            self.store._decrement_unlocked(action_type)
            self.store.audit(
                {
                    "event": "execution_failed",
                    "action_type": action_type,
                    "jti": record["jti"],
                    "token_digest": record["token_digest"],
                    "error_code": error.code,
                    "failed_at": float(self.clock()),
                    "comment_digest": record.get("comment_digest"),
                }
            )
            raise

        executed_at = float(self.clock())
        result = {
            "jti": record["jti"],
            "token_digest": record["token_digest"],
            "action_type": action_type,
            "status": "executed",
            "executed_at": executed_at,
            "count": new_count,
            "audit_status": "recorded",
        }
        try:
            self.store.audit(
                {
                    "event": "executed",
                    "action_type": action_type,
                    "jti": record["jti"],
                    "token_digest": record["token_digest"],
                    "executed_at": executed_at,
                    "comment_digest": record.get("comment_digest"),
                }
            )
        except ControlError:
            result["audit_status"] = "failed"
            result["state_warning"] = (
                "The device action executed, but its final audit record was not persisted"
            )
        return result

    def _execute_comment(
        self,
        record: Dict[str, Any],
        client: ADBClient,
        backend: str,
    ) -> None:
        coordinate = record["coordinate"]["pixel"]
        comment_input = record["comment_input"]["pixel"]
        comment_send = record["comment_send"]["pixel"]
        wait_seconds = float(self.config.get("comment_wait_seconds", 0.25))

        client.tap(coordinate["x"], coordinate["y"])
        self.sleeper(wait_seconds)
        client.tap(comment_input["x"], comment_input["y"])
        self.sleeper(wait_seconds)
        client.input_text(record["text"], backend)
        self.sleeper(wait_seconds)
        client.tap(comment_send["x"], comment_send["y"])

    def _comment_backend(self, record: Dict[str, Any], client: ADBClient) -> str:
        backend = self.config["input_backend"]
        if backend == "auto":
            backend = "adb" if self._safe_adb_text(record["text"]) else "adb-keyboard"
        if backend == "adb-keyboard" and not client.adb_keyboard_status()["available"]:
            raise ControlError(
                "UNSUPPORTED_TEXT_INPUT",
                "ADB Keyboard is not installed, enabled, and selected",
                "Install, enable, and select ADB Keyboard manually before retrying",
            )
        return backend

    def _coordinate(
        self,
        position_name: str,
        width: int,
        height: int,
        *,
        x_ratio: Optional[float] = None,
        y_ratio: Optional[float] = None,
    ) -> Dict[str, Dict[str, Any]]:
        if x_ratio is None:
            position = self.config.get("positions", {}).get(position_name)
            if position is None:
                raise ControlError(
                    "INVALID_COORDINATE",
                    "Action requires explicit or configured coordinates",
                )
            x_ratio, y_ratio = position
        x = ratio_to_pixel(float(x_ratio), width)
        y = ratio_to_pixel(float(y_ratio), height)
        return {
            "normalized": {"x": float(x_ratio), "y": float(y_ratio)},
            "pixel": {"x": x, "y": y},
        }

    def _selected_serial(self) -> str:
        if self.client.serial is not None:
            return self.client.serial
        return self.client.selected_device().serial

    def _pinned_client(self, serial: str) -> ADBClient:
        pinned = copy.copy(self.client)
        pinned.serial = serial
        return pinned

    @staticmethod
    def _validate_target_package(client: ADBClient, target_package: str) -> None:
        if client.foreground_package() != target_package:
            raise ControlError(
                "ACTION_TARGET_CHANGED",
                "The configured Android package is not in the foreground",
                "Return to the configured package and prepare the action again",
            )

    def _config_fingerprint(self) -> str:
        encoded = json.dumps(
            self.config,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _safe_adb_text(text: str) -> bool:
        return re.fullmatch(r"[A-Za-z0-9 .,_-]*", text) is not None
