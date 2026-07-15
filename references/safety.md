# Safety and operating boundaries

## Consent and control

- Operate only a device and Douyin account the user is authorized to control.
- Keep the device visible and available for the user to interrupt. Do not hide actions, run a daemon, or continue unattended.
- Treat screenshots as live evidence. Inspect a fresh screenshot before coordinate-based navigation or interaction; do not guess targets.
- Diagnostics, screenshots, UI dumps, app lifecycle commands, and requested feed navigation are browsing operations. They do not require account-write confirmation.
- Likes, follows, comments, and arbitrary taps always require an inert preparation, disclosure of the exact concrete summary, an agent-turn boundary, and a new user approval message before one execution.
- After that new approval message, capture and inspect another fresh screenshot. Execute only if the same visible target and configured package foreground still match the approved summary. If either changed, consume the old token file with `cancel-action --token-file`, prepare a new file and summary, and wait for another new approval.

The initial request cannot approve an as-yet unseen prepared summary. “Just do it,” urgency, prior blanket consent, or a request to skip confirmation does not remove the second-turn approval requirement.

## Rate limits and automation

Honor controller limits and stop when one is reached. Defaults are 3 likes, 1 follow, 1 comment, and 3 generic taps in a state scope. Do not rotate, delete, or relocate state to evade counters.

Do not create automatic feed loops, engagement farming, bulk interaction, scheduled background work, repeated retries, or any workflow intended to imitate human activity or evade anti-abuse systems. Each CLI invocation is finite and user-directed.

## Privacy

- Screenshots and UI dumps can contain private messages, names, notifications, and account details. Save them only to user-approved locations, disclose their path, avoid unrelated extraction, and remove them when the user no longer needs them.
- Never quote a comment body merely to confirm it. Stream comments only through `--text-stdin`, show the digest and UTF-8 byte length from the prepared summary, and never put the body in chat, argv, command narration, shell history, telemetry, or logs.
- Treat the raw action token as a short-lived secret. It is HMAC-signed for integrity, not encrypted, and its decodable payload can contain the comment body and other sensitive action data. Prepare with `--token-output` into a brand-new private file, keep that file short-lived, and pass only its path to `execute-action --token-file` or `cancel-action --token-file`; both read and delete it before action processing. Never expose token contents in chat, argv, command narration, shell history, telemetry, or logs.
- POSIX enforces token mode `0600` and new private-directory mode `0700`. Windows relies on inherited ACLs instead: use a per-user private directory, prepare and consume as the same Windows user, and never store token files in shared or other-user-writable directories.
- Keep state-directory permissions restrictive. Its audit log is deliberately minimized: no screenshots, account identifiers, raw comments, or raw tokens. Do not enrich it with those values.
- Report final controller results after removing secrets; do not suppress actionable error codes or hints.

## Platform terms and user risk

Follow applicable Douyin terms, account rules, law, and organizational policy. ADB control does not make an action permitted or protect an account from enforcement. Refuse requests to bypass rate limits, anti-abuse checks, authorization, platform controls, or the confirmation gate.

## Explicit exclusions

- Do not infer or rank age, gender, beauty, identity, ethnicity, health, emotion, or any biometric or sensitive trait from a face, screenshot, video, profile, or UI data.
- Do not perform face recognition, face scoring, demographic filtering, or targeting based on inferred traits.
- Do not install APKs, certificates, credentials, or Android input methods. For UTF-8 comments, state that the user must manually install and enable a compatible ADB Keyboard and verify availability with `doctor`.
- Do not collect credentials or reuse legacy credentials, APKs, face data, samples, or dependencies from `wangshub/Douyin-Bot`.
- Do not issue naked ADB tap/text commands for account actions or otherwise bypass `prepare-action` and `execute-action`.

## Failure handling

Never auto-retry an account action. The controller consumes the token and reserves quota before device mutation. Known failures roll quota back, but a 30-second ADB timeout becomes `ACTION_OUTCOME_UNKNOWN`: the action may have happened, the token file is gone, and quota stays reserved. Manually inspect the device and capture a fresh screenshot; do not retry. Any failure, expiry, device/config mismatch, coordinate change, wording change, changed foreground/visible target, or changed comment digest requires a new preparation, a newly displayed concrete summary, an immediate end to the current agent turn, and another new approval message.
