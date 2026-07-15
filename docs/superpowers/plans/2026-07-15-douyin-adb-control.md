# Douyin ADB Control Agent Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and publish a portable, confirmation-gated Agent Skill for controlling Douyin on an Android device through ADB.

**Architecture:** Keep the repository root Agent Skills-compatible with a vendor-neutral `SKILL.md`. Route all execution through a Python standard-library CLI; isolate ADB operations in `douyin_core.py` and stateful two-phase actions in `douyin_actions.py`.

**Tech Stack:** Python 3.9+ standard library, Android Debug Bridge, `unittest`, Agent Skills `SKILL.md`, JSON.

## Global Constraints

- Use Python 3.9+ and no third-party Python packages.
- Do not infer beauty, gender, age, identity, or other biometric attributes.
- Do not create a daemon, unattended loop, or anti-ban behavior.
- Do not execute likes, follows, comments, or arbitrary taps without a fresh pending-action token.
- Use normalized coordinates and select every ADB target with an explicit serial.
- Keep stdout machine-readable in `--json` mode and send diagnostics to stderr.
- Do not install APKs or Android input methods automatically.
- Keep the skill vendor-neutral and compliant with the Agent Skills specification.

---

## File map

- `scripts/douyin_core.py`: stateless ADB runner, device parsing/selection, screen and app inspection, screenshots, UI dumps, swipes, taps, and text input primitives.
- `scripts/douyin_actions.py`: config models, token store, counters, audit log, action preparation, cancellation, and execution policy.
- `scripts/douyin_control.py`: CLI parser, command handlers, JSON/human rendering, and error mapping.
- `tests/test_core.py`: deterministic tests for core ADB behavior.
- `tests/test_actions.py`: deterministic tests for confirmation and limit policy.
- `tests/test_cli.py`: subprocess-level CLI output and exit-code tests.
- `SKILL.md`: portable activation metadata and operating workflow.
- `references/commands.md`: detailed CLI reference and examples.
- `references/safety.md`: consent, privacy, limits, and platform-compliance guidance.
- `assets/config.example.json`: schema-versioned coordinate and policy example.
- `evals/evals.json`: realistic agent behavior prompts and expected outcomes.
- `.gitignore`: Python caches, runtime state, screenshots, and build artifacts.
- `LICENSE`: MIT license.

---

### Task 1: Core ADB operations

**Files:**
- Create: `tests/test_core.py`
- Create: `scripts/douyin_core.py`

**Interfaces:**
- Produces: `ControlError`, `CommandResult`, `Device`, `parse_devices()`, `select_device()`, `ratio_to_pixel()`, and `ADBClient`.

- [ ] **Step 1: Write failing parsing and coordinate tests**

```python
def test_parse_devices_preserves_state_and_details():
    output = "List of devices attached\nABC123\tdevice product:p model:Pixel_8 transport_id:1\nBAD\tunauthorized\n"
    devices = parse_devices(output)
    assert [(d.serial, d.state) for d in devices] == [("ABC123", "device"), ("BAD", "unauthorized")]

def test_ratio_to_pixel_clamps_edges():
    assert ratio_to_pixel(1.0, 1080) == 1079
    assert ratio_to_pixel(0.5, 1920) == 960
```

- [ ] **Step 2: Run tests and verify they fail because the module is absent**

Run: `python3 -m unittest tests.test_core -v`
Expected: `ModuleNotFoundError: No module named 'douyin_core'`.

- [ ] **Step 3: Implement error/result/device types and pure helpers**

```python
@dataclass(frozen=True)
class Device:
    serial: str
    state: str
    details: dict[str, str]

def ratio_to_pixel(ratio: float, extent: int) -> int:
    if not 0.0 <= ratio <= 1.0 or extent <= 0:
        raise ControlError("INVALID_COORDINATE", "Coordinate ratio or extent is invalid")
    return min(extent - 1, max(0, round(ratio * extent)))
```

Implement `parse_devices()` by splitting tab-separated device lines and parsing trailing `key:value` tokens. Implement `select_device()` so zero usable devices yields `NO_DEVICE`, multiple usable devices without `--serial` yields `MULTIPLE_DEVICES`, and unauthorized/offline selected devices yield `DEVICE_UNAUTHORIZED` or `DEVICE_OFFLINE`.

- [ ] **Step 4: Add failing ADBClient tests with an injected runner**

Test exact command arrays for `devices`, `shell wm size`, `exec-out screencap -p`, package launch/stop, UI dump, swipe, tap, plain ASCII input, and base64 ADB Keyboard input. Assert screenshot bytes are written byte-for-byte.

- [ ] **Step 5: Implement ADBClient**

Implement these exact public methods using Python 3.9-compatible `typing` forms: constructor `(adb_path: str = "adb", serial: Optional[str] = None, runner: Callable = subprocess.run)`, `devices() -> List[Device]`, `selected_device() -> Device`, `screen_size() -> Tuple[int, int]`, `foreground_package() -> Optional[str]`, `is_package_installed(package: str) -> bool`, `screenshot(output: Path) -> Path`, `ui_dump(output: Path) -> Path`, `swipe(start: Tuple[int, int], end: Tuple[int, int], duration_ms: int) -> None`, `tap(x: int, y: int) -> None`, and `input_text(text: str, backend: str) -> None`.

All device commands must include `-s SERIAL`; commands that discover devices must not. Use `subprocess.run(command, check=False, capture_output=True)` and convert non-zero results into stable `ControlError` values.

- [ ] **Step 6: Run core tests**

Run: `python3 -m unittest tests.test_core -v`
Expected: all core tests pass.

- [ ] **Step 7: Commit the core**

```bash
git add scripts/douyin_core.py tests/test_core.py
git commit -m "feat: add portable adb control core"
```

### Task 2: Confirmation-gated action policy

**Files:**
- Create: `tests/test_actions.py`
- Create: `scripts/douyin_actions.py`
- Create: `assets/config.example.json`

**Interfaces:**
- Consumes: `ADBClient`, `ControlError`, and `ratio_to_pixel` from `douyin_core`.
- Produces: `load_config()`, `ActionStore`, `ActionService.prepare()`, `ActionService.cancel()`, and `ActionService.execute()`.

- [ ] **Step 1: Write failing configuration tests**

Test schema version `1`, required package/swipe/limits fields, coordinate range validation, positive duration/token TTL, supported input backends, and deep-copy defaults.

- [ ] **Step 2: Implement validated configuration loading**

```python
def load_config(path: Optional[Path]) -> Dict[str, Any]:
    config = json.loads(path.read_text("utf-8")) if path else default_config()
    validate_config(config)
    return config
```

The example asset must set package `com.ss.android.ugc.aweme`, token TTL `300`, limits `{like: 3, follow: 1, comment: 1, tap: 3}`, and normalized next/previous swipes.

- [ ] **Step 3: Write failing token lifecycle tests**

Cover cryptographically random tokens, stored payload, five-minute expiry, wrong-device rejection, one-time consumption, explicit cancellation, configuration fingerprint mismatch, and no ADB write during preparation.

- [ ] **Step 4: Implement ActionStore with atomic JSON writes**

Implement these exact public methods: `create(payload: Dict[str, Any]) -> Dict[str, Any]`, `consume(token: str, now: float) -> Dict[str, Any]`, `cancel(token: str, now: float) -> Dict[str, Any]`, `count(action_type: str) -> int`, `increment(action_type: str) -> None`, and `audit(event: Dict[str, Any]) -> None`.

Persist pending records under `pending/`, used markers under `used/`, counters in `counters.json`, and privacy-minimized events in `audit.jsonl`. Use `tempfile.NamedTemporaryFile` plus `os.replace` for atomic file replacement.

- [ ] **Step 5: Write failing execution-policy tests**

Assert limits are checked before execution, tokens are consumed before attempting ADB, counters increment only on success, audit records omit comment text, named coordinates fall back to config, explicit ratios override config, and UTF-8 comments require the ADB Keyboard backend.

- [ ] **Step 6: Implement ActionService**

Implement these exact public methods: `prepare(action_type: str, *, x_ratio: Optional[float] = None, y_ratio: Optional[float] = None, text: Optional[str] = None) -> Dict[str, Any]`, `cancel(token: str) -> Dict[str, Any]`, and `execute(token: str) -> Dict[str, Any]`.

`prepare()` resolves and stores both normalized and pixel coordinates plus a comment digest. `execute()` supports like/follow/tap as a single tap and comment as configured comment-button tap, input-field tap, text input, and send-button tap with bounded waits.

- [ ] **Step 7: Run action tests and commit**

Run: `python3 -m unittest tests.test_actions -v`
Expected: all action tests pass.

```bash
git add scripts/douyin_actions.py tests/test_actions.py assets/config.example.json
git commit -m "feat: require confirmation tokens for account actions"
```

### Task 3: Cross-platform CLI

**Files:**
- Create: `tests/test_cli.py`
- Create: `scripts/douyin_control.py`

**Interfaces:**
- Consumes: core and action interfaces from Tasks 1 and 2.
- Produces: executable CLI commands `doctor`, `devices`, `status`, `screenshot`, `ui-dump`, `swipe`, `open`, `stop`, `prepare-action`, `execute-action`, and `cancel-action`.

- [ ] **Step 1: Write failing parser and renderer tests**

Invoke `main(argv, stdout, stderr, client_factory)` directly. Assert global flags work before subcommands, JSON success/failure shapes are stable, human mode contains actionable hints, and exceptions map to documented exit codes.

- [ ] **Step 2: Implement parser and output envelope**

```python
def success(command: str, data: Dict[str, Any]) -> Dict[str, Any]:
    return {"ok": True, "command": command, "data": data}

def failure(command: str, error: ControlError) -> Dict[str, Any]:
    return {"ok": False, "command": command, "error": error.as_dict()}
```

Use `argparse` subparsers and a handler table. Keep the script importable for tests and executable through `if __name__ == "__main__": raise SystemExit(main())`.

- [ ] **Step 3: Implement read and navigation handlers**

`doctor` reports every check without stopping at the first failure. `devices`, `status`, `screenshot`, and `ui-dump` return absolute artifact paths. `swipe` resolves ratios against the current screen. `open` and `stop` operate only on the configured package.

- [ ] **Step 4: Implement action handlers**

`prepare-action` must print a user-displayable summary and token but never execute. `execute-action` and `cancel-action` accept only a token. No direct CLI tap or comment command may bypass `ActionService`.

- [ ] **Step 5: Run CLI and full tests, then commit**

Run: `python3 -m unittest discover -s tests -v`
Expected: all tests pass without an Android device.

```bash
git add scripts/douyin_control.py tests/test_cli.py
git commit -m "feat: expose stable agent-friendly cli"
```

### Task 4: Portable Agent Skill instructions and evaluations

**Files:**
- Create: `SKILL.md`
- Create: `references/commands.md`
- Create: `references/safety.md`
- Create: `evals/evals.json`
- Create: `.gitignore`
- Create: `LICENSE`

**Interfaces:**
- Consumes: the complete CLI from Task 3.
- Produces: an Agent Skills-compliant package and behavioral evaluation set.

- [ ] **Step 1: Write SKILL.md**

Use frontmatter name `douyin-adb-control`, license `MIT`, compatibility `Requires Python 3.9+, Android Debug Bridge (adb), and an authorized Android device with Douyin installed.` The description must trigger for Douyin Android device diagnostics, screenshots, feed navigation, and confirmation-gated interactions.

The workflow must require: run `doctor`; select a device serial; capture/inspect before coordinate actions; use `prepare-action`; quote the returned summary to the user; wait for explicit approval; only then use `execute-action`; report final JSON results.

- [ ] **Step 2: Write focused references**

`commands.md` documents exact invocation forms, output envelopes, exit codes, coordinate calibration, ASCII versus UTF-8 comment input, and troubleshooting. `safety.md` explains consent, rate limits, privacy-minimized logs, platform terms, and why biometric inference and unattended loops are excluded.

- [ ] **Step 3: Add evaluations**

Create three eval entries: disconnected-device diagnosis, screenshot-and-next navigation, and confirmation-gated like. The last expected output explicitly requires stopping before `execute-action` until a new user approval message exists.

- [ ] **Step 4: Validate the skill and run tests**

Run: `python3 /Users/hayecheng/.codex/skills/.system/skill-creator/scripts/quick_validate.py .`
Expected: `Skill is valid!`

Run: `python3 -m unittest discover -s tests -v`
Expected: all tests pass.

- [ ] **Step 5: Commit the portable skill**

```bash
git add SKILL.md LICENSE .gitignore references evals docs
git commit -m "docs: package portable douyin agent skill"
```

### Task 5: Evaluation, packaging, and GitHub publication

**Files:**
- Create outside repository: `outputs/douyin-adb-control-skill.zip`

**Interfaces:**
- Consumes: validated repository state.
- Produces: installable archive and public GitHub repository.

- [ ] **Step 1: Run behavior evaluations**

Use the three prompts in `evals/evals.json`. Compare with-skill behavior to a no-skill baseline where practical. Grade objective assertions: correct diagnostics, no unnecessary write confirmation for swipe, and mandatory stop between prepare and execute.

- [ ] **Step 2: Generate human-reviewable evaluation output**

Run the skill-creator evaluation viewer in static mode and inspect the results. If the harness cannot safely execute ADB commands, use recorded fake-ADB outputs and grade the agent's selected command sequence.

- [ ] **Step 3: Perform final verification**

Run the full unit suite, skill validator, `python3 scripts/douyin_control.py --help`, JSON help/error smoke tests, `git diff --check`, and a secret scan for the legacy AppID/AppKey. Confirm the working tree contains no runtime state, screenshots, APKs, or face images.

- [ ] **Step 4: Package the skill**

Create a deterministic zip whose top-level directory is `douyin-adb-control/`, excluding `.git`, caches, tests' temporary state, and `docs/superpowers` if a minimal install package is desired. Copy it to the workspace `outputs/` directory.

- [ ] **Step 5: Publish to GitHub**

Create public repository `hayecheng/douyin-adb-control-skill`, set description `Portable Agent Skill for confirmation-gated Douyin control over Android ADB`, push branch `main`, and add topics `agent-skill`, `agentskills`, `adb`, `android`, `douyin`, and `python`.

- [ ] **Step 6: Verify the remote**

Fetch the repository metadata and raw `SKILL.md`; confirm the default branch is `main`, topics are present, and the remote commit matches local `HEAD`.
