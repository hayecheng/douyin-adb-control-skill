# Douyin ADB Control Agent Skill Design

## Summary

Build `douyin-adb-control` as a portable Agent Skills-compatible capability inspired by `wangshub/Douyin-Bot`. Keep the useful ADB device-control pattern, but replace the legacy Python dependencies, Tencent face API, hard-coded credentials, fixed OnePlus 5 coordinates, demographic inference, and unattended social automation.

The repository root is the skill root. Any Agent Skills-compatible client can discover `SKILL.md`; any agent with terminal access can run the bundled Python controller.

## Goals

- Support Android device diagnostics, Douyin launch/stop, screenshots, UI hierarchy export, and feed navigation through ADB.
- Let vision-capable agents interpret screenshots without binding the skill to one model vendor.
- Require two-phase confirmation for likes, follows, comments, and arbitrary taps.
- Use normalized coordinates so profiles can work across resolutions.
- Emit stable JSON results and machine-readable errors.
- Run on Python 3.9+ with no third-party Python packages.
- Package and publish the skill as a public GitHub repository with Agent Skill topics.

## Non-goals

- Do not infer beauty, gender, age, identity, or other biometric attributes.
- Do not run an unattended loop or background service.
- Do not bypass Douyin anti-abuse controls or claim protection from bans.
- Do not install APKs or Android input methods automatically.
- Do not depend on Codex, Claude, Copilot, or another vendor-specific tool API.

## Portable skill structure

```text
douyin-adb-control/
├── SKILL.md
├── LICENSE
├── assets/
│   └── config.example.json
├── references/
│   ├── commands.md
│   └── safety.md
├── scripts/
│   ├── douyin_control.py
│   ├── douyin_core.py
│   └── douyin_actions.py
├── tests/
│   ├── test_actions.py
│   ├── test_cli.py
│   └── test_core.py
└── evals/
    └── evals.json
```

`SKILL.md` uses only fields defined by the Agent Skills specification: `name`, `description`, `license`, `compatibility`, and `metadata`. It contains no vendor-specific tool names or UI metadata.

## Architecture

```text
Natural-language request
  -> agent activates SKILL.md
  -> agent runs scripts/douyin_control.py
  -> controller validates config, device, and action policy
  -> controller invokes adb with an explicit device serial
  -> controller returns JSON or a PNG/XML artifact
  -> agent interprets the artifact or asks the user to inspect it
```

The implementation is split into three modules:

- `douyin_core.py`: subprocess execution, device selection, screen discovery, coordinate conversion, screenshots, UI dump, app lifecycle, and swipes.
- `douyin_actions.py`: configuration, pending-action tokens, expiration, replay protection, rate limits, audit events, and semantic actions.
- `douyin_control.py`: `argparse` command surface, human/JSON rendering, error-to-exit-code mapping, and orchestration.

Each CLI invocation is finite. No command creates a daemon.

## CLI contract

```text
python3 scripts/douyin_control.py [--adb PATH] [--serial SERIAL]
  [--config PATH] [--state-dir PATH] [--json] COMMAND
```

Read-oriented commands:

- `doctor`: check Python, ADB, device state, screen, Douyin package, and optional ADB Keyboard.
- `devices`: return connected devices and states.
- `status`: return selected device, screen size, and foreground package.
- `screenshot --output PATH`: save PNG bytes from `adb exec-out screencap -p`.
- `ui-dump --output PATH`: dump Android UI XML and save it locally.
- `swipe next|previous`: move the feed using normalized config coordinates.
- `open`: launch `com.ss.android.ugc.aweme`.
- `stop`: force-stop the package.

Account-mutating commands:

- `prepare-action --type like|follow|comment|tap` with coordinates and optional text: validate and persist an inert pending action, then return its summary and token.
- `execute-action --token TOKEN`: validate the token, consume it exactly once, enforce limits, then perform the bound action.
- `cancel-action --token TOKEN`: consume a pending token without executing it.

The agent must show the exact action summary to the user and receive explicit approval after `prepare-action` and before `execute-action`. The separation is enforced by persistent controller state rather than a vendor-specific approval UI.

## Configuration

Configuration is JSON with schema version `1`. It contains:

- package name;
- normalized swipe start/end points and duration;
- optional named positions for like, follow, comment input, and comment send;
- default limits of 3 likes, 1 follow, 1 comment, and 3 generic taps per session;
- token lifetime of 300 seconds;
- input backend selection: `auto`, `adb`, or `adb-keyboard`.

Coordinates are floats from `0.0` through `1.0`, multiplied by the current physical screen size and clamped to valid pixel coordinates. Explicit CLI coordinates override named profile positions.

Runtime state defaults to `.douyin-adb-control/` in the caller's working directory and can be overridden by `--state-dir` or `DOUYIN_AGENT_STATE_DIR`. The state directory contains pending tokens, used-token markers, counters, and a privacy-minimized JSONL audit log. It does not store screenshots, comment text, or account identifiers.

## Action safety

A pending action binds:

- a cryptographically random token;
- action type;
- selected device serial;
- normalized and resolved pixel coordinates;
- a SHA-256 digest of comment text instead of the text in audit records;
- creation and expiration timestamps;
- configuration fingerprint.

Tokens expire after five minutes and are consumed on the first execution attempt, including failed ADB attempts. A retry requires a new user confirmation. Counters are checked before execution and incremented only after a successful device command.

Plain ADB text input is limited to safe ASCII. UTF-8 comment input requires a separately installed compatible ADB Keyboard package and uses a base64 broadcast. `doctor` reports capability; the skill never installs the APK.

## Output and errors

`--json` writes one JSON object to stdout. For example, success is `{"ok": true, "command": "devices", "data": {"devices": []}}`. A failure is `{"ok": false, "command": "status", "error": {"code": "NO_DEVICE", "message": "No authorized Android device is connected", "hint": "Connect a device and accept the USB debugging prompt"}}` and uses a non-zero exit status.

Stable error codes cover invalid arguments/configuration, missing ADB, no/multiple/unauthorized devices, missing package, screenshot/UI-dump failures, unsupported text input, expired/missing/replayed tokens, exceeded limits, and failed device commands.

## Testing

Offline tests use `unittest` and injected subprocess runners. They cover device parsing and selection, normalized coordinate conversion, PNG preservation, configuration validation, token creation/expiration/replay, per-action limits, privacy-minimized audit events, text input encoding, JSON CLI output, and the guarantee that preparation never sends tap/input commands.

Optional device smoke tests are read-oriented: `doctor`, `status`, `screenshot`, and feed swipe. Automated tests never like, follow, comment, or tap a real account.

Agent behavior evaluation uses three prompts:

1. Diagnose a missing or unauthorized device.
2. Capture a screenshot and navigate to the next feed item without requesting unnecessary write approval.
3. Like a visible item, where the agent must create a pending action and stop for user confirmation before execution.

## Distribution and licensing

The new implementation is written from scratch and licensed under MIT. `SKILL.md` credits `wangshub/Douyin-Bot` as inspiration and links to its MIT-licensed repository without copying its hard-coded credentials, face data, sample images, APKs, or obsolete dependencies.

Publish the repository as `douyin-adb-control-skill` with description "Portable Agent Skill for confirmation-gated Douyin control over Android ADB" and topics `agent-skill`, `agentskills`, `adb`, `android`, `douyin`, and `python`.
