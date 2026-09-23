# Changelog

## 1.0.0

First public release.

- Watches systemd units (state, crashes, restart loops, errors in the journal), Docker containers,
  plain processes, HTTP endpoints and TCP ports.
- Per-target rules: labels, extra/replacement/ignored log patterns, what counts as "down",
  failure thresholds, reminders, and routing to selected notifiers.
- Alerts to Telegram (several bots/chats, forum topics, `/status` `/mute` `/unmute` commands),
  email and generic webhooks (Slack/Discord/Mattermost-compatible payload).
- Guaranteed delivery: undelivered alerts are queued in the state file and retried with back-off.
- One error is reported once (timestamps, ids and counters are ignored when de-duplicating).
- Guided `svcwatch setup` and one-line installer.
- `svcwatch add telegram` and `svcwatch watch ...` change the configuration without opening it
  (append-only, validated before saving, backup kept, service restarted).
- No runtime dependencies on Python 3.11+ (`tomli` on 3.9/3.10).
