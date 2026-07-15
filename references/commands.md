# Command reference

## Invocation contract

Resolve `SKILL_DIR` to the absolute skill root and use the bundled controller:

```sh
python3 "$SKILL_DIR/scripts/douyin_control.py" [GLOBAL_FLAGS] COMMAND [COMMAND_FLAGS]
```

All global flags must appear before `COMMAND`:

| Global flag | Meaning |
| --- | --- |
| `--adb PATH` | ADB executable; default `adb`. |
| `--serial SERIAL` | Device serial. Pin this after selecting a device. |
| `--config PATH` | Schema-version-1 JSON configuration; defaults to the built-in configuration. |
| `--state-dir PATH` | Pending-action state. Precedence is flag, `DOUYIN_AGENT_STATE_DIR`, then `$PWD/.douyin-adb-control`. |
| `--json` | Emit exactly one JSON object to stdout. Prefer this for agent use. |

For example, this placement is valid:

```sh
python3 "$SKILL_DIR/scripts/douyin_control.py" --serial "$SERIAL" --json screenshot --output "$OUTPUT"
```

`screenshot --json ...` is invalid because `--json` is a global flag placed after the subcommand. `--json --help` returns a success envelope with command `help`; ordinary `--help` remains human-readable.

## The 11 commands

| Command | Exact command arguments | Result in `data` | Account-write approval |
| --- | --- | --- | --- |
| `doctor` | none | `healthy` plus `checks` for Python, ADB, selected device, Douyin package, screen, ADB Keyboard, and possible state locks | No |
| `devices` | none | `devices[]`, each with `serial`, `state`, and `details` | No |
| `status` | none | selected `device`, `screen.width`, `screen.height`, and `foreground_package` | No |
| `screenshot` | `--output PATH` | absolute local PNG `path` | No |
| `ui-dump` | `--output PATH` | absolute local XML `path` | No |
| `swipe` | positional `next` or `previous` | direction, pixel start/end, and `duration_ms` | No |
| `open` | none | configured `package` and status `launched` | No |
| `stop` | none | configured `package` and status `stopped` | No |
| `prepare-action` | `--type like|follow|comment|tap`; optional paired `--x/--x-ratio RATIO` and `--y/--y-ratio RATIO`; comments use `--text-stdin`; agent preparations use `--token-output PATH` | with `--token-output`, absolute `token_path` and public `summary`; preparation sends no tap or text input | Yes, after summary |
| `execute-action` | exactly one token input channel; agent path is `--token-file PATH`, private alternate is `--token-stdin` | action type, status `executed`, execution time, privacy-safe token digest/JTI, and new count | Only in a later turn after approval |
| `cancel-action` | exactly one token input channel; agent path is `--token-file PATH`, private alternate is `--token-stdin` | action type, status `canceled`, cancellation time, privacy-safe token digest/JTI | No execution occurs |

Always use `python3 "$SKILL_DIR/scripts/douyin_control.py"` for controller actions. Do not replace account actions with naked `adb tap` or `adb input` commands.

## JSON and exit status

With `--json`, success has this envelope:

```json
{"ok":true,"command":"devices","data":{"devices":[]}}
```

Failure has this envelope and a nonzero exit status:

```json
{"ok":false,"command":"status","error":{"code":"NO_DEVICE","message":"No authorized Android device is connected","hint":"Connect a device and accept the USB debugging prompt"}}
```

| Exit | Meaning and error families |
| ---: | --- |
| `0` | Command completed. For `doctor`, still require `data.healthy == true`. |
| `1` | Unclassified controller error. |
| `2` | `INVALID_ARGUMENT` or `INVALID_COORDINATE`. |
| `3` | `INVALID_CONFIG`. |
| `4` | `ADB_NOT_FOUND`. |
| `5` | `NO_DEVICE`, `MULTIPLE_DEVICES`, `DEVICE_UNAUTHORIZED`, or `DEVICE_OFFLINE`. |
| `6` | ADB/device operation, screen-size, screenshot, UI-dump, or missing-package failure. |
| `7` | Token/state errors, invalid actions, unsupported text input, or action-limit exhaustion. |

Without `--json`, success is written to stdout as a heading followed by pretty JSON. Failures go to stderr as `ERROR [CODE] ...` plus a hint.

## Doctor and device selection

`doctor` aggregates checks and normally exits `0` even when one fails. Parse `data.healthy`; if false, report each failed or skipped check and do not proceed. If the ADB Keyboard query succeeds, that check has `ok: true` even when one or more of `installed`, `enabled`, or `selected` is false, so `available: false` does not independently make the overall result unhealthy. The state fields and remediation hint remain present. A query-command failure has `ok: false` and does make the result unhealthy. Separately require `available: true` before executing UTF-8 or shell-sensitive comments.

Use `devices` to choose a serial whose state is exactly `device`. `unauthorized` requires accepting the USB-debugging prompt; `offline` requires reconnecting or restarting ADB. When multiple usable devices exist, select one explicitly and put `--serial SERIAL` before every later subcommand.

## Artifacts and visual grounding

`screenshot` saves raw PNG bytes; `ui-dump` saves the Android UI XML. Relative output paths are resolved against the caller's current directory, parent directories are created, and the JSON result returns an absolute path.

Before a swipe or account action, capture a current screenshot and inspect the returned artifact. Do not infer the target from the default profile, a previous frame, filenames, or UI XML alone. A swipe is feed navigation and may run on request without account-write approval after this inspection.

## Coordinates and calibration

Action ratios are normalized values from `0.0` through `1.0`. Both X and Y must be provided together. The controller resolves a ratio with `round(ratio * extent)` and clamps it into `0..extent-1`. The prepared summary records both forms:

```json
{"normalized":{"x":0.91,"y":0.48},"pixel":{"x":983,"y":1152}}
```

Explicit `--x/--y` values override the named configured position. Default named positions and swipes are starting profiles, not evidence of the current UI. Calibrate against a freshly inspected screenshot and current screen dimensions. For comments, preparation also binds configured input and send positions even though the public summary exposes the initial comment-button coordinate.

## Comment input

Comments accept at most 4096 UTF-8 bytes. Plain ADB input accepts only ASCII matching `[A-Za-z0-9 .,_-]*`; spaces are sent as `%s`. With input backend `auto`, safe ASCII uses plain ADB and other text uses the `adb-keyboard` broadcast with base64-encoded UTF-8.

The skill never installs or enables an input method. For UTF-8 or shell-sensitive text, require a compatible ADB Keyboard to be manually installed/enabled and confirm the `doctor` ADB Keyboard check reports `available: true`. `input_backend: adb` rejects unsupported text instead of falling back.

Never display or log a comment body. The preparation summary exposes only its SHA-256 digest and UTF-8 byte length.

## Private comment and token transport

The agent path uses `--text-stdin` for comments and `--token-output PATH` for every preparation. Comment input is non-empty valid UTF-8, at most 4096 bytes after the controller removes one trailing line ending. `--token-output` must name a brand-new file: the controller never overwrites a file or follows a final symlink and calls `fsync`. On POSIX, it enforces mode `0600` for the file and `0700` for new parent directories; token input also rejects group/world permissions on the file or immediate parent. On Windows, POSIX mode bits do not represent ACL privacy: use a per-user private directory with inherited ACLs, run prepare and consume as the same Windows user, and never choose a shared or other-user-writable directory. The result contains only `token_path` and the public summary; it never prints the raw token.

In the later approval turn, pass only that path with `execute-action --token-file PATH` or `cancel-action --token-file PATH`. The token input limit is 65536 bytes. A token input file must be a regular non-symlink file in the private directory described above; the controller reads and unlinks it before constructing action state/service objects, including when its contents are empty, invalid UTF-8, or oversized. This makes the file single-attempt transport: never recreate it to retry.

Compatibility warning: legacy `--text TEXT` may replace `--text-stdin`, and legacy `--token TOKEN` may replace either token channel, but each command still accepts exactly one relevant input channel. These legacy flags expose secrets through process arguments, shell history, and logs; agents must never use them. `--token-stdin` avoids argv exposure but is not the approval workflow because it cannot provide the short-lived, consume-on-use token-file lifecycle. Never place a raw token or comment body in chat, argv, command narration, telemetry, or persistent logs.

## State, locking, and token privacy

The state directory contains pending and used markers, counters, `audit.jsonl`, an HMAC key, and transient locks. It uses restrictive permissions where supported. The privacy-minimized audit omits raw tokens and comment text; it records digests and action metadata. Screenshots, UI dumps, and account identifiers are not written to the state directory.

Tokens default to a 300-second lifetime, are bound to the action type, serial, coordinates, configuration fingerprint, and comment details, and are single-use. The HMAC signature prevents undetected modification but does not encrypt the decodable payload; a comment token can reveal comment text. Keep the token file short-lived. POSIX enforces mode `0600` for it and `0700` for new parent directories; Windows relies on same-user inherited ACLs in a per-user private directory, never a shared or other-user-writable directory. Successful `execute-action --token-file` and `cancel-action --token-file` inputs are read and deleted before action processing.

Preparation and execution both require the configured package to remain in the foreground; otherwise `ACTION_TARGET_CHANGED` requires returning to the app and preparing again. `open` and `stop` first require the configured package to be installed and report `PACKAGE_NOT_FOUND` if it is absent. ADB Keyboard comment execution requires all three state fields—`installed`, `enabled`, and `selected`—so that `available` is true. Every ADB subprocess has a 30-second timeout.

Execution consumes the token and reserves quota before the first device mutation. A known pre-mutation/device failure rolls the reservation back, but an ADB timeout can occur after the device acted. The controller therefore returns `ACTION_OUTCOME_UNKNOWN`, keeps the quota reservation, and consumes the token. Never retry it: manually inspect the device, capture a fresh screenshot, and determine the visible state before considering a separately prepared action.

`doctor` reports `*.lock` files as possible stale locks. Remove one only after confirming no controller process is running. Do not delete or rotate state to evade counters. Token/state/action failures require a fresh `prepare-action`, a newly displayed summary, and a new user approval message.

## Troubleshooting

| Symptom | Response |
| --- | --- |
| `ADB_NOT_FOUND` | Install Android platform tools or supply `--adb PATH`; rerun `doctor`. |
| `NO_DEVICE` / `DEVICE_UNAUTHORIZED` / `DEVICE_OFFLINE` | Run `devices`, reconnect, unlock and authorize the device, then pin its serial. |
| `MULTIPLE_DEVICES` | Ask the user to choose a listed serial; do not guess. |
| `data.healthy` is false | Stop, report failed checks and their hints, fix them, then rerun `doctor`. |
| `PACKAGE_NOT_FOUND` or package check false | Have the user install the configured Douyin package; this skill does not install APKs. |
| Screenshot or UI-dump failure | Check storage/output permissions and device connectivity; capture a new artifact after fixing. |
| `UNSUPPORTED_TEXT_INPUT` | Use safe ASCII or have the user manually configure ADB Keyboard for UTF-8. |
| Token expired, replayed, missing, invalid, mismatched, or execution failed | Do not retry. Prepare a new token file, show the new summary, end the turn, and wait for new approval. For `ACTION_OUTCOME_UNKNOWN`, first inspect the device manually because the action may have occurred and quota remains reserved. |
| `ACTION_LIMIT_EXCEEDED` | Stop. Do not reset state or change directories to bypass the configured limit. |
| `STATE_BUSY` or lock warning | Check for an active controller process; never delete locks speculatively. |
