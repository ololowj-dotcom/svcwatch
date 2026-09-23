# svcwatch

[![CI](https://github.com/ololowj-dotcom/svcwatch/actions/workflows/ci.yml/badge.svg)](https://github.com/ololowj-dotcom/svcwatch/actions/workflows/ci.yml)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)
![License MIT](https://img.shields.io/badge/license-MIT-green)

**A small watchdog for Linux servers.** It watches your systemd services, Docker containers, processes,
HTTP endpoints and TCP ports, and tells you in **Telegram** (or email / any webhook) the moment something
breaks - with the traceback attached - and again when it recovers.

*[Читать по-русски](README.ru.md)*

```
$ curl -fsSL https://raw.githubusercontent.com/ololowj-dotcom/svcwatch/main/install.sh | sudo bash
```

- **Two-minute setup.** A guided wizard connects your Telegram bot, finds your chat id by itself, sends a test
  message, picks up the services you deployed and installs a background service.
- **Tells you what matters.** One error = one alert (numbers, ids and timestamps are ignored when comparing),
  a whole traceback arrives as a single message, network noise goes into a daily summary instead of your phone.
- **Catches what "is it running?" misses.** A service that keeps crashing and restarting looks healthy to
  `systemctl` - svcwatch counts the restarts and warns you.
- **Aimed at exactly what you choose.** Every service, container, URL, port or process can have its own name,
  patterns, thresholds and destination chat.
- **Never loses an alert.** If Telegram is unreachable the alert waits in a queue and is retried with back-off.
- **Tiny.** Standard library only (no runtime dependencies on Python 3.11+), one config file, one process.

## Contents

[Install](#install) · [Quick start](#quick-start) · [Telegram](#telegram) · [Choosing what to watch](#choosing-what-to-watch) ·
[How alerts work](#how-alerts-work) · [Configuration](#configuration) · [Commands](#commands) · [Security](#security) ·
[Development](#development)

## Install

**One command** (Debian/Ubuntu and other systemd distros, needs Python 3.9+, `git`, `python3-venv`):

```bash
curl -fsSL https://raw.githubusercontent.com/ololowj-dotcom/svcwatch/main/install.sh | sudo bash
```

It creates a private virtualenv in `/opt/svcwatch`, links `svcwatch` into `/usr/local/bin` and starts the
wizard. To only install: `SVCWATCH_NO_SETUP=1` in front of `bash`.

**With pip**, anywhere:

```bash
pip install git+https://github.com/ololowj-dotcom/svcwatch
```

## Quick start

```bash
sudo svcwatch setup
```

The wizard asks for one thing - a bot token from [@BotFather](https://t.me/BotFather) - and then:

1. checks the token and shows the bot's name;
2. asks you to press **Start** in the bot (or add it to a group) and detects the chat by itself;
3. sends a test message;
4. lists the services it found and shows how many will be watched;
5. writes `/etc/svcwatch/svcwatch.toml` and a private `.env` with the token;
6. offers to install and start the background service.

Then, at any time:

```bash
svcwatch check          # what is watched, and its state right now
svcwatch test-notify    # send a test alert to every channel
```

Prefer to do it by hand? `svcwatch init` writes an annotated template and `svcwatch install-service` prints
the unit file.

### Change things later - without opening the config

```bash
svcwatch add telegram --name team              # another bot or chat (finds the chat_id for you)
svcwatch watch nginx --label "Web server"      # a systemd service
svcwatch watch --docker web db                 # Docker containers
svcwatch watch --http https://example.com/health --contains ok
svcwatch watch --tcp 5432 --name postgres      # a port (HOST:PORT, or just PORT for this machine)
svcwatch watch --process "python -m app.worker" --name worker --min-count 2
svcwatch watch payments --notify team          # send this one's alerts to the "team" chat only
```

These commands append to the file (your comments stay), validate the result before saving, keep the previous
version as `svcwatch.toml.bak`, refuse names that do not exist (unless `--force`), try a new URL, port or
process once so you see immediately whether it works, and restart the background service when it is running.

## Telegram

Create a bot with [@BotFather](https://t.me/BotFather) (`/newbot`). Use a bot of its own for svcwatch:
Telegram allows only one program to read a bot's messages, and the chat commands below need that.

Alerts look like this:

```
🔴 Service down: payments
web-01 · 2026-03-01 04:12:09

failed/failed (result: exit-code)
```

```
🔴 Error in Payments API
web-01 · 2026-03-01 04:12:41

Traceback (most recent call last):
  File "app.py", line 88, in charge
ValueError: card token expired
```

And when it is fixed: `🟢 Payments API recovered - Was down for 4m 12s`. Recoveries and summaries arrive
as silent notifications.

**Chat commands** (answered only in the configured chat, and in `allowed_chats` if you list some):

| Command | What it does |
|---|---|
| `/status` | what is up and what is not, right now |
| `/mute 30m` | pause alerts (`s`, `m`, `h`, `d`; default 1 h) - handy during a deploy |
| `/unmute` | resume |
| `/ping` | is the watchdog itself alive |

Several bots or chats, forum topics and severity filters are supported - see the
[full example](examples/svcwatch.example.toml). If `api.telegram.org` is unreliable from your server, point
`api_base` at your own relay (`svcwatch setup --api-base https://...`).

Not a Telegram person? Email (SMTP) and generic webhooks work the same way; the webhook payload contains
`text` and `content` so Slack, Mattermost and Discord accept it as is.

## Choosing what to watch

By default svcwatch watches **the services you deployed** (unit files in `/etc/systemd/system`) - not the
operating system's own. Everything else is opt-in and precise:

```toml
[systemd]
discover = "custom"            # custom = my units | all = everything | off = only what I list
exclude  = ["svcwatch", "backup-*"]
units    = ["nginx", "postgresql"]          # always watch these

[[systemd.watch]]              # rules for ONE service (or a glob such as "worker-*")
match = "payments"
label = "Payments API"                       # the name shown in alerts
alert_on = ["failed", "inactive"]            # also alert when it was stopped
immediate_extra = ["ERROR", "re:timeout after \\d+s"]   # extra patterns; "re:" = regular expression
ignore_extra = ["health check passed"]       # never alert on these lines
fail_threshold = 1
notify = ["oncall"]                          # only this Telegram chat

[[systemd.watch]]
match = "nightly-report"
logs = false                                 # watch its state, not its (chatty) log
```

Beyond systemd:

```toml
[docker]
enabled = true                 # containers: state, exit code, OOM kills, health checks, restarts, logs

[[http]]
name = "site"
url = "https://example.com/health"
contains = "ok"                # the page must contain this text
fail_threshold = 3             # ...three checks in a row must fail before you are woken up
every = 60

[[tcp]]
name = "postgres"
port = 5432

[[process]]
name = "queue-worker"
pattern = "python -m app.worker"   # matched against the full command line (pgrep -f)
min_count = 2
```

Every rule and check accepts `label`, `notify`, `fail_threshold` and `remind_after`. A typo in the config is
reported with a hint (`unknown setting 'intervall' (did you mean 'interval'?)`) instead of being ignored.

## How alerts work

| Situation | What you get |
|---|---|
| unit `failed` (or `inactive`, if you asked) | one **down** alert, then **recovered** with the downtime |
| unit restarted itself (`Restart=always` loop) | a **warning** with the restart count |
| error text in the log (`Traceback`, `CRITICAL`, your patterns) | one alert with the traceback; the same error stays quiet for `dedup_window` (30 min) |
| "noise" (`Bad Gateway`, `Connection reset`...) | counted, reported once a day in the summary |
| URL / port / process check fails | **down** after `fail_threshold` failures in a row, **recovered** later |
| still broken after hours | optional reminder (`remind_after`) |
| Telegram / SMTP unreachable | the alert is queued in the state file and retried (back-off, up to 24 h) |
| an alert storm | at most `rate_limit_per_hour` (60) alerts, plus one notice; recoveries are never dropped |
| first start | history is **not** replayed: only errors from now on are reported |

Once a day (`summary_interval`) you get a short summary - which also tells you the watchdog is alive.

## Configuration

`svcwatch.toml` is searched in `./`, `/etc/svcwatch/` and `~/.config/svcwatch/` (or use `-c`). Secrets are
referenced as `${NAME}` (with an optional default: `${NAME:-fallback}`) and read from a `.env` file next to
the config or from the environment. Sections: `[monitor]`, `[logs]`, `[systemd]`, `[docker]`, `[[http]]`,
`[[tcp]]`, `[[process]]`, `[notify.*]`. The annotated template (`svcwatch init`) and
[examples/svcwatch.example.toml](examples/svcwatch.example.toml) document every option.

## Commands

| Command | Purpose |
|---|---|
| `svcwatch setup` | guided setup (Telegram, services, background service) |
| `svcwatch add telegram` | connect another Telegram bot or chat (guided) |
| `svcwatch watch ...` | start watching a service, container, URL, port or process |
| `svcwatch check` | validate the config, show every target and its state (exit code 2 if something is down) |
| `svcwatch run` | run the watchdog (`--once` for one cycle, `--dry-run` to print instead of send) |
| `svcwatch test-notify` | send a test alert to every configured channel |
| `svcwatch telegram-setup` | find the `chat_id` for a bot token |
| `svcwatch mute 1h` / `unmute` | pause / resume alerts from the shell |
| `svcwatch init` | write an annotated config template |
| `svcwatch install-service` | print (or `--write --start`) the systemd unit |

## Security

- The bot token is stored only in the `.env` file (mode `0600`), never in the config, and is never printed or
  logged - error messages are scrubbed of it.
- Reading other services' journals requires root (or membership of the `systemd-journal` group). The supplied
  unit runs as root with `NoNewPrivileges`, `ProtectSystem=full`, `ProtectHome` and `PrivateTmp`. To run it as
  an unprivileged user, add that user to `systemd-journal` (and `docker` if you watch containers).
- Chat commands are accepted only from the configured chat (plus `allowed_chats`); everything else is ignored
  and logged.
- svcwatch makes outbound connections only to what you configure. It has no listening port.

## Development

```bash
git clone https://github.com/ololowj-dotcom/svcwatch && cd svcwatch
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest && ruff check src tests
```

The tests never touch a real system: `systemctl`, `journalctl`, `docker` and `pgrep` are replaced by
recorded outputs, and Telegram, webhooks and web pages are served by tiny local fakes. They also run in CI on
Python 3.9-3.13.

## License

MIT - see [LICENSE](LICENSE).
