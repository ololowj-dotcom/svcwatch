from __future__ import annotations

import json
import shlex
import sys
from typing import Any, Dict, List, Optional

CONFIG_TEMPLATE = """\
# svcwatch configuration.  Docs: README.md
# Check it any time with:  svcwatch check
# Secrets live in the .env file next to this file and are referenced as ${{VAR}}.

[monitor]
interval = 20                 # seconds between checks
dedup_window = 1800           # the same error is reported at most once per 30 min
summary_interval = 86400      # daily summary (also works as a "still alive" heartbeat)
# remind_after = 21600        # repeat "still down" every 6 h (0 = never)
state_file = "{state_file}"

# --------------------------------------------------------------------------
# What to watch
# --------------------------------------------------------------------------
[systemd]
enabled = true
# custom = the units you deployed yourself (files in /etc/systemd/system);
# all    = every service on the machine;  off = only what is listed below.
discover = "{discover}"
exclude = ["svcwatch", "getty@*", "systemd-*"]
# units = ["nginx", "postgresql"]     # always watch these, whatever discover says

# Per-service rules. `match` is a name or a glob such as "api-*"; a plain name
# also adds that service to the watch list. Every setting is optional.
#
# [[systemd.watch]]
# match = "payments"
# label = "Payments API"                   # name shown in alerts
# alert_on = ["failed", "inactive"]        # "inactive" = also alert if it was stopped
# immediate_extra = ["ERROR", "re:timeout after \\\\d+s"]   # more patterns ("re:" = regex)
# ignore_extra = ["healthcheck ok"]        # never alert on these lines
# notify = ["ops"]                         # only this notifier (default: all)
#
# [[systemd.watch]]
# match = "backup"
# logs = false                             # watch the state only, not the log text

[docker]
enabled = {docker}
# discover = "all"
# exclude = ["watchtower"]
# [[docker.watch]]
# match = "web"
# alert_on = ["failed", "inactive"]

# Patterns are case-insensitive substrings; prefix with "re:" for a regex.
[logs]
immediate = ["CRITICAL", "Traceback", "Exception", "FATAL", "panic:"]   # alert now
external = ["Bad Gateway", "Connection reset by peer"]                   # noise: daily summary only
# ignore = []                                                             # drop completely
# journal_priority = 3     # also alert on every journald entry of level "err" or worse

# Active checks ------------------------------------------------------------
# [[http]]
# name = "website"
# url = "https://example.com/health"
# expect_status = 200
# contains = "ok"             # optional: text that must be in the body
# fail_threshold = 3          # alert after 3 failed checks in a row
# every = 60                  # seconds between checks (0 = every cycle)
#
# [[tcp]]
# name = "postgres"
# host = "127.0.0.1"
# port = 5432
#
# [[process]]
# name = "worker"
# pattern = "python -m app.worker"   # matched against the full command line
# min_count = 1

# --------------------------------------------------------------------------
# Where alerts go
# --------------------------------------------------------------------------
[notify]
console = true                # also print alerts to the service log
{telegram}
# [notify.email]
# host = "smtp.example.com"
# port = 587
# user = "alerts@example.com"
# password = "${{SMTP_PASSWORD}}"
# sender = "alerts@example.com"
# to = ["me@example.com"]
#
# [notify.webhook]             # Slack / Discord / Mattermost / anything that takes JSON
# url = "${{WEBHOOK_URL}}"
"""

DEFAULT_API_BASE = "https://api.telegram.org"

TELEGRAM_BLOCK = """\
[[notify.telegram]]
bot_token = "${{TELEGRAM_BOT_TOKEN}}"
chat_id = "{chat_id}"
{api_line}commands = {commands}          # answer /status, /mute 1h, /unmute in that chat
# thread_id = 12              # post into a forum topic
# min_severity = "critical"   # info | warning | critical
"""

TELEGRAM_PLACEHOLDER = """\
# [[notify.telegram]]         # run `svcwatch setup` to fill this in automatically
# bot_token = "${TELEGRAM_BOT_TOKEN}"
# chat_id = "123456789"
# commands = true
"""


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def render_config(
    *,
    state_file: str,
    chat_id: Optional[str] = None,
    discover: str = "custom",
    docker: bool = False,
    commands: bool = True,
    api_base: str = DEFAULT_API_BASE,
) -> str:
    api_line = "" if api_base == DEFAULT_API_BASE else 'api_base = "%s"\n' % _toml_escape(api_base)
    telegram = (
        TELEGRAM_BLOCK.format(chat_id=chat_id, commands="true" if commands else "false", api_line=api_line)
        if chat_id else TELEGRAM_PLACEHOLDER
    )
    return CONFIG_TEMPLATE.format(
        state_file=_toml_escape(state_file), discover=discover, docker="true" if docker else "false",
        telegram=telegram,
    )


UNIT_TEMPLATE = """\
[Unit]
Description=svcwatch - service watchdog
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=true
PrivateTmp=true
StateDirectory=svcwatch

[Install]
WantedBy=multi-user.target
"""


def render_unit(config_path: str, python: Optional[str] = None) -> str:
    py = python or sys.executable
    parts: List[str] = [py, "-m", "svcwatch", "run", "--config", config_path]
    return UNIT_TEMPLATE.format(exec_start=" ".join(shlex.quote(p) for p in parts))


def q(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_telegram_block(
    *,
    name: str,
    token_env: str,
    chat_id: str,
    thread_id: Optional[int] = None,
    min_severity: str = "info",
    commands: bool = True,
    api_base: str = DEFAULT_API_BASE,
) -> str:
    lines = [
        "[[notify.telegram]]",
        f"name = {q(name)}",
        f'bot_token = "${{{token_env}}}"',
        f"chat_id = {q(chat_id)}",
    ]
    if api_base != DEFAULT_API_BASE:
        lines.append(f"api_base = {q(api_base)}")
    if thread_id is not None:
        lines.append(f"thread_id = {int(thread_id)}")
    if min_severity != "info":
        lines.append(f"min_severity = {q(min_severity)}")
    lines.append(f"commands = {'true' if commands else 'false'}")
    return "\n".join(lines) + "\n"


def render_rule_block(
    section: str,
    match: str,
    *,
    label: Optional[str] = None,
    notify: Optional[List[str]] = None,
    inactive: bool = False,
    logs: bool = True,
    fail_threshold: Optional[int] = None,
) -> str:
    lines = [f"[[{section}.watch]]", f"match = {q(match)}"]
    if label:
        lines.append(f"label = {q(label)}")
    if inactive:
        lines.append('alert_on = ["failed", "inactive"]')
    if not logs:
        lines.append("logs = false")
    if fail_threshold is not None:
        lines.append(f"fail_threshold = {int(fail_threshold)}")
    if notify:
        lines.append("notify = [" + ", ".join(q(n) for n in notify) + "]")
    return "\n".join(lines) + "\n"


def render_check_block(kind: str, name: str, fields: Dict[str, Any], *, label: Optional[str] = None,
                       notify: Optional[List[str]] = None) -> str:
    lines = [f"[[{kind}]]", f"name = {q(name)}"]
    if label:
        lines.append(f"label = {q(label)}")
    for key, value in fields.items():
        if isinstance(value, bool):
            lines.append(f"{key} = {'true' if value else 'false'}")
        elif isinstance(value, int):
            lines.append(f"{key} = {value}")
        else:
            lines.append(f"{key} = {q(str(value))}")
    if notify:
        lines.append("notify = [" + ", ".join(q(n) for n in notify) + "]")
    return "\n".join(lines) + "\n"
