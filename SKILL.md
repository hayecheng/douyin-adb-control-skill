---
name: douyin-adb-control
description: Use when an agent needs Douyin Android ADB diagnostics, screenshots, feed navigation, or confirmation-gated like, follow, comment, and tap interactions on an authorized device.
license: MIT
compatibility: Requires Python 3.9+, Android Debug Bridge (adb), and an authorized Android device with Douyin installed.
---

# Douyin ADB Control

Operate one authorized Android device through the bundled finite CLI. Treat screenshots as the source of truth and keep every account interaction behind a fresh, concrete approval.

Before operating, read [references/commands.md](references/commands.md) for the exact CLI contract and [references/safety.md](references/safety.md) for consent, privacy, and platform boundaries.

## Resolve the controller

Resolve `SKILL_DIR` to the absolute directory containing this `SKILL.md`; never assume the caller's working directory. Invoke the controller only in this form:

```sh
SKILL_DIR="/absolute/path/to/douyin-adb-control"
python3 "$SKILL_DIR/scripts/douyin_control.py" --json doctor
```

Put global flags (`--adb`, `--serial`, `--config`, `--state-dir`, `--json`) before the subcommand. Prefer `--json`, parse the single envelope, and do not infer success from prose.

## Start every operation safely

1. Run `doctor`. Require both top-level `ok: true` and `data.healthy: true`. `doctor` can exit `0` while reporting `healthy: false`; stop and explain failed checks in that case. A successfully queried but unavailable ADB Keyboard remains an optional healthy check; require its separate `available: true` field only before UTF-8 comment execution.
2. Run `devices`, choose an authorized device whose state is `device`, and pin all subsequent commands with `--serial SERIAL`. If several are usable and the user has not identified one, ask which serial to use.
3. Before any coordinate-based operation, capture a fresh screenshot and actually inspect the PNG. Never claim what is visible without inspecting the artifact. Confirm that the target and resolved position match the current screen.
4. Perform requested diagnostics, screenshots, UI dumps, app open/stop, and feed swipes directly. These browsing operations do not require account-write confirmation.

## Account interaction is always two turns

This rule applies to `like`, `follow`, `comment`, and arbitrary `tap`.

### Preparation turn

1. Choose a brand-new absolute `TOKEN_FILE` path in a private temporary directory. After inspecting a fresh screenshot, run only `prepare-action` with `--token-output "$TOKEN_FILE"`, the selected serial, and intended normalized coordinates or verified configured position:

   ```sh
   TOKEN_FILE="/absolute/private/path/prepared-action.token"
   python3 "$SKILL_DIR/scripts/douyin_control.py" --serial "$SERIAL" --json prepare-action --type like --token-output "$TOKEN_FILE"
   ```

   For a comment, stream the body from a private source through standard input; the body must never be an argument:

   ```sh
   python3 "$SKILL_DIR/scripts/douyin_control.py" --serial "$SERIAL" --json prepare-action --type comment --text-stdin --token-output "$TOKEN_FILE" < "$PRIVATE_COMMENT_FILE"
   ```

2. Keep the token file private. Show the user the complete `data.summary`, including action type, device serial, normalized and pixel coordinates, creation and expiry times, plus the SHA-256 comment digest and UTF-8 byte length for comments. Do not repeat comment text or expose token contents.
3. State that no account change has occurred and ask for explicit approval of that exact summary.
4. End the agent turn. Do not call `execute-action` in this turn.

The user's original request—even “execute directly,” “no need to ask,” or equivalent—is not approval after seeing the prepared summary. Never collapse preparation and execution into one turn.

### Approval turn

Only a new user message sent after the summary can approve execution. Verify that it explicitly approves the same action, device, coordinates, comment digest/length when present, and unexpired expiry time. After that new message, capture and inspect another fresh screenshot and verify the same visible target and configured package foreground. If they still match, run the file-consuming command once and report its final JSON result:

```sh
python3 "$SKILL_DIR/scripts/douyin_control.py" --serial "$SERIAL" --json execute-action --token-file "$TOKEN_FILE"
```

If the fresh screenshot, visible target, or foreground changed, cancel with `cancel-action --token-file "$TOKEN_FILE"`; then prepare a new token file, show the new summary, and end the turn for another fresh approval. Do the same if the user changes anything or the token expires. Never retry an execution failure. If the user declines, consume the file with `cancel-action --token-file "$TOKEN_FILE"`. Never substitute raw `adb shell input tap`, text input, or another bypass.

## Sensitive values and boundaries

Do not echo raw tokens or comment bodies in chat, arguments, command narration, logs, shell history, or diagnostics. Keep the brand-new token file short-lived. POSIX enforces mode `0600` for it and `0700` for new parent directories. Windows relies on inherited ACLs: prepare and consume as the same user in a per-user private directory, never a shared or other-user-writable directory. `execute-action --token-file` and `cancel-action --token-file` read and delete that file before action processing. A token has an HMAC signature for integrity; it is not encrypted, and its payload can be decoded and may contain the comment body. Treat both token contents and comment bodies as secrets.

Do not create unattended loops, bulk likes/follows/comments, or mechanisms that bypass platform terms or controller limits. Do not infer age, gender, beauty, identity, or biometric traits. Do not install APKs; for UTF-8 comments, report that a compatible ADB Keyboard must be installed and enabled manually.

This skill is a from-scratch MIT-licensed implementation inspired by the MIT-licensed [wangshub/Douyin-Bot](https://github.com/wangshub/Douyin-Bot). It includes none of that project's credentials, APKs, face data, dependencies, or automation implementation.
