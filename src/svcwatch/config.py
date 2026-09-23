from __future__ import annotations

import difflib
import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from .logscan import compile_pattern
from .models import SEVERITY_RANK


class ConfigError(Exception):
    pass


DEFAULT_IMMEDIATE = ["CRITICAL", "Traceback", "Exception", "FATAL", "panic:"]
DEFAULT_EXTERNAL = [
    "Bad Gateway",
    "Gateway Timeout",
    "Connection reset by peer",
    "Temporary failure in name resolution",
]
DEFAULT_SYSTEMD_EXCLUDE = ["svcwatch", "getty@*", "serial-getty@*", "systemd-*", "user@*", "user-runtime-dir@*"]
ALERT_ON_CHOICES = ("failed", "inactive")
DISCOVER_CHOICES = ("custom", "all", "off")

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_GLOB_CHARS = set("*?[")


@dataclass
class MonitorCfg:
    interval: int = 20
    dedup_window: int = 1800
    summary_interval: int = 86400
    summary_when_empty: bool = True
    remind_after: int = 0
    rate_limit_per_hour: int = 60
    initial_lookback: int = 0
    max_backlog_lines: int = 2000
    state_file: Path = Path("svcwatch-state.json")
    log_file: Optional[Path] = None
    hostname: str = ""
    title_prefix: str = "[svcwatch]"


@dataclass
class LogsCfg:
    immediate: List[str] = field(default_factory=lambda: list(DEFAULT_IMMEDIATE))
    external: List[str] = field(default_factory=lambda: list(DEFAULT_EXTERNAL))
    ignore: List[str] = field(default_factory=list)
    context_lines: int = 3
    max_lines_per_alert: int = 20
    journal_priority: int = 0


@dataclass
class Rule:
    match: str
    label: Optional[str] = None
    logs: Optional[bool] = None
    alert_on: Optional[List[str]] = None
    restart_alert: Optional[bool] = None
    immediate: Optional[List[str]] = None
    immediate_extra: List[str] = field(default_factory=list)
    external: Optional[List[str]] = None
    external_extra: List[str] = field(default_factory=list)
    ignore: Optional[List[str]] = None
    ignore_extra: List[str] = field(default_factory=list)
    notify: Optional[List[str]] = None
    fail_threshold: Optional[int] = None
    remind_after: Optional[int] = None
    journal_priority: Optional[int] = None


@dataclass
class Effective:
    label: str
    logs: bool
    alert_on: List[str]
    restart_alert: bool
    immediate: List[str]
    external: List[str]
    ignore: List[str]
    notify: Optional[List[str]]
    fail_threshold: int
    remind_after: int
    journal_priority: int


@dataclass
class SystemdCfg:
    enabled: bool = True
    discover: str = "custom"
    unit_dirs: List[str] = field(default_factory=lambda: ["/etc/systemd/system"])
    include: List[str] = field(default_factory=lambda: ["*"])
    exclude: List[str] = field(default_factory=lambda: list(DEFAULT_SYSTEMD_EXCLUDE))
    units: List[str] = field(default_factory=list)
    alert_on: List[str] = field(default_factory=lambda: ["failed"])
    restart_alert: bool = True
    watch: List[Rule] = field(default_factory=list)


@dataclass
class DockerCfg:
    enabled: bool = False
    discover: str = "all"
    include: List[str] = field(default_factory=lambda: ["*"])
    exclude: List[str] = field(default_factory=list)
    containers: List[str] = field(default_factory=list)
    alert_on: List[str] = field(default_factory=lambda: ["failed"])
    restart_alert: bool = True
    watch: List[Rule] = field(default_factory=list)


@dataclass
class HttpCheck:
    name: str
    url: str
    expect_status: List[int] = field(default_factory=lambda: [200])
    contains: str = ""
    timeout: int = 10
    fail_threshold: int = 3
    verify_tls: bool = True
    every: int = 0
    label: Optional[str] = None
    notify: Optional[List[str]] = None
    remind_after: Optional[int] = None


@dataclass
class TcpCheck:
    name: str
    host: str
    port: int
    timeout: int = 5
    fail_threshold: int = 2
    every: int = 0
    label: Optional[str] = None
    notify: Optional[List[str]] = None
    remind_after: Optional[int] = None


@dataclass
class ProcessCheck:
    name: str
    pattern: str
    min_count: int = 1
    max_count: int = 0
    fail_threshold: int = 2
    every: int = 0
    label: Optional[str] = None
    notify: Optional[List[str]] = None
    remind_after: Optional[int] = None


@dataclass
class TelegramCfg:
    name: str
    bot_token: str
    chat_id: str
    api_base: str = "https://api.telegram.org"
    thread_id: Optional[int] = None
    timeout: int = 15
    silent: bool = False
    min_severity: str = "info"
    commands: bool = False
    allowed_chats: List[str] = field(default_factory=list)


@dataclass
class EmailCfg:
    name: str
    host: str
    port: int
    sender: str
    to: List[str]
    user: str = ""
    password: str = ""
    use_ssl: bool = False
    starttls: bool = True
    timeout: int = 30
    min_severity: str = "info"


@dataclass
class WebhookCfg:
    name: str
    url: str
    headers: Dict[str, str] = field(default_factory=dict)
    timeout: int = 15
    min_severity: str = "info"


@dataclass
class NotifyCfg:
    console: bool = True
    telegram: List[TelegramCfg] = field(default_factory=list)
    email: List[EmailCfg] = field(default_factory=list)
    webhook: List[WebhookCfg] = field(default_factory=list)

    def names(self) -> List[str]:
        names = ["console"] if self.console else []
        names += [n.name for n in self.telegram]
        names += [n.name for n in self.email]
        names += [n.name for n in self.webhook]
        return names


@dataclass
class Config:
    monitor: MonitorCfg = field(default_factory=MonitorCfg)
    logs: LogsCfg = field(default_factory=LogsCfg)
    systemd: SystemdCfg = field(default_factory=SystemdCfg)
    docker: DockerCfg = field(default_factory=DockerCfg)
    http: List[HttpCheck] = field(default_factory=list)
    tcp: List[TcpCheck] = field(default_factory=list)
    process: List[ProcessCheck] = field(default_factory=list)
    notify: NotifyCfg = field(default_factory=NotifyCfg)
    path: Optional[Path] = None

    @property
    def mute_file(self) -> Path:
        return self.monitor.state_file.with_name(self.monitor.state_file.name + ".mute")


def _has_glob(pattern: str) -> bool:
    return any(ch in _GLOB_CHARS for ch in pattern)


def name_matches(pattern: str, name: str) -> bool:
    return fnmatch.fnmatchcase(name.lower(), pattern.lower())


def resolve(rules: List[Rule], name: str, base: Effective) -> Effective:
    eff = Effective(**{**base.__dict__})
    eff.label = base.label or name
    for rule in rules:
        if not name_matches(rule.match, name):
            continue
        if rule.label:
            eff.label = rule.label
        if rule.logs is not None:
            eff.logs = rule.logs
        if rule.alert_on is not None:
            eff.alert_on = list(rule.alert_on)
        if rule.restart_alert is not None:
            eff.restart_alert = rule.restart_alert
        if rule.immediate is not None:
            eff.immediate = list(rule.immediate)
        eff.immediate = eff.immediate + rule.immediate_extra
        if rule.external is not None:
            eff.external = list(rule.external)
        eff.external = eff.external + rule.external_extra
        if rule.ignore is not None:
            eff.ignore = list(rule.ignore)
        eff.ignore = eff.ignore + rule.ignore_extra
        if rule.notify is not None:
            eff.notify = list(rule.notify)
        if rule.fail_threshold is not None:
            eff.fail_threshold = rule.fail_threshold
        if rule.remind_after is not None:
            eff.remind_after = rule.remind_after
        if rule.journal_priority is not None:
            eff.journal_priority = rule.journal_priority
    return eff


def explicit_names(rules: List[Rule]) -> List[str]:
    return [r.match for r in rules if not _has_glob(r.match)]


class _Ctx:
    def __init__(self) -> None:
        self.errors: List[str] = []

    def err(self, path: str, msg: str) -> None:
        self.errors.append(f"{path}: {msg}")


def _check_keys(ctx: _Ctx, table: Dict[str, Any], path: str, allowed: List[str]) -> None:
    for key in table:
        if key not in allowed:
            hint = difflib.get_close_matches(key, allowed, n=1)
            more = f" (did you mean '{hint[0]}'?)" if hint else f" (allowed: {', '.join(allowed)})"
            ctx.err(f"{path}.{key}" if path else key, f"unknown setting{more}")


def _as_table(ctx: _Ctx, parent: Dict[str, Any], key: str, path: str) -> Dict[str, Any]:
    value = parent.get(key, {})
    if not isinstance(value, dict):
        ctx.err(path, "must be a table, e.g. [%s]" % path)
        return {}
    return value


def _as_list_of_tables(ctx: _Ctx, parent: Dict[str, Any], key: str, path: str) -> List[Dict[str, Any]]:
    value = parent.get(key, [])
    if isinstance(value, dict):
        return [value]
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        ctx.err(path, "must be an array of tables, e.g. [[%s]]" % path)
        return []
    return value


def _val(
    ctx: _Ctx,
    table: Dict[str, Any],
    path: str,
    key: str,
    default: Any,
    kind: str,
    *,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
    choices: Optional[tuple] = None,
) -> Any:
    if key not in table:
        return default
    value = table[key]
    where = f"{path}.{key}" if path else key
    ok = True
    if kind == "str":
        ok = isinstance(value, str)
    elif kind == "int":
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif kind == "bool":
        ok = isinstance(value, bool)
    elif kind == "strs":
        ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
    elif kind == "ints":
        ok = isinstance(value, list) and all(isinstance(v, int) and not isinstance(v, bool) for v in value)
    elif kind == "strmap":
        ok = isinstance(value, dict) and all(isinstance(v, str) for v in value.values())
    if not ok:
        names = {
            "str": "a string", "int": "an integer", "bool": "true/false",
            "strs": "a list of strings", "ints": "a list of integers", "strmap": "a table of strings",
        }
        ctx.err(where, f"must be {names[kind]}")
        return default
    if kind == "int":
        if minimum is not None and value < minimum:
            ctx.err(where, f"must be >= {minimum}")
            return default
        if maximum is not None and value > maximum:
            ctx.err(where, f"must be <= {maximum}")
            return default
    if choices is not None:
        items = value if isinstance(value, list) else [value]
        bad = [v for v in items if v not in choices]
        if bad:
            ctx.err(where, f"invalid value {bad[0]!r}; allowed: {', '.join(choices)}")
            return default
    return value


def _check_patterns(ctx: _Ctx, patterns: List[str], where: str) -> None:
    for pat in patterns:
        try:
            compile_pattern(pat)
        except re.error as exc:
            ctx.err(where, f"broken regular expression {pat!r}: {exc}")


def parse_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _interpolate(value: Any, env: Dict[str, str], path: str, ctx: _Ctx) -> Any:
    if isinstance(value, str):

        def repl(m: re.Match[str]) -> str:
            name, default = m.group(1), m.group(2)
            if env.get(name):
                return env[name]
            if default is not None:
                return default
            ctx.err(path, f"environment variable {name} is not set (put it in the .env file or the service environment)")
            return ""

        return _ENV_RE.sub(repl, value)
    if isinstance(value, list):
        return [_interpolate(v, env, f"{path}[{i}]", ctx) for i, v in enumerate(value)]
    if isinstance(value, dict):
        return {k: _interpolate(v, env, f"{path}.{k}" if path else k, ctx) for k, v in value.items()}
    return value


def _drop_disabled_notifiers(data: Dict[str, Any]) -> None:
    notify = data.get("notify")
    if not isinstance(notify, dict):
        return
    for kind in ("telegram", "email", "webhook"):
        section = notify.get(kind)
        if isinstance(section, dict) and section.get("enabled") is False:
            del notify[kind]
        elif isinstance(section, list):
            notify[kind] = [s for s in section if not (isinstance(s, dict) and s.get("enabled") is False)]


def _parse_rules(ctx: _Ctx, raw: List[Dict[str, Any]], path: str) -> List[Rule]:
    rules: List[Rule] = []
    allowed = [
        "match", "label", "logs", "alert_on", "restart_alert", "immediate", "immediate_extra",
        "external", "external_extra", "ignore", "ignore_extra", "notify", "fail_threshold",
        "remind_after", "journal_priority",
    ]
    for i, tbl in enumerate(raw):
        where = f"{path}[{i}]"
        _check_keys(ctx, tbl, where, allowed)
        match = _val(ctx, tbl, where, "match", "", "str")
        if not match:
            ctx.err(where, "'match' is required (unit/container name or glob such as 'api-*')")
            continue
        rule = Rule(match=match)
        rule.label = _val(ctx, tbl, where, "label", None, "str")
        rule.logs = _val(ctx, tbl, where, "logs", None, "bool")
        rule.alert_on = _val(ctx, tbl, where, "alert_on", None, "strs", choices=ALERT_ON_CHOICES)
        rule.restart_alert = _val(ctx, tbl, where, "restart_alert", None, "bool")
        rule.immediate = _val(ctx, tbl, where, "immediate", None, "strs")
        rule.immediate_extra = _val(ctx, tbl, where, "immediate_extra", [], "strs")
        rule.external = _val(ctx, tbl, where, "external", None, "strs")
        rule.external_extra = _val(ctx, tbl, where, "external_extra", [], "strs")
        rule.ignore = _val(ctx, tbl, where, "ignore", None, "strs")
        rule.ignore_extra = _val(ctx, tbl, where, "ignore_extra", [], "strs")
        rule.notify = _val(ctx, tbl, where, "notify", None, "strs")
        rule.fail_threshold = _val(ctx, tbl, where, "fail_threshold", None, "int", minimum=1)
        rule.remind_after = _val(ctx, tbl, where, "remind_after", None, "int", minimum=0)
        rule.journal_priority = _val(ctx, tbl, where, "journal_priority", None, "int", minimum=0, maximum=7)
        for attr in ("immediate", "immediate_extra", "external", "external_extra", "ignore", "ignore_extra"):
            _check_patterns(ctx, getattr(rule, attr) or [], f"{where}.{attr}")
        rules.append(rule)
    return rules


def _parse_systemd(ctx: _Ctx, data: Dict[str, Any]) -> SystemdCfg:
    tbl = _as_table(ctx, data, "systemd", "systemd")
    _check_keys(ctx, tbl, "systemd", [
        "enabled", "discover", "unit_dirs", "include", "exclude", "units", "alert_on", "restart_alert", "watch",
    ])
    cfg = SystemdCfg()
    cfg.enabled = _val(ctx, tbl, "systemd", "enabled", cfg.enabled, "bool")
    cfg.discover = _val(ctx, tbl, "systemd", "discover", cfg.discover, "str", choices=DISCOVER_CHOICES)
    cfg.unit_dirs = _val(ctx, tbl, "systemd", "unit_dirs", cfg.unit_dirs, "strs")
    cfg.include = _val(ctx, tbl, "systemd", "include", cfg.include, "strs")
    cfg.exclude = _val(ctx, tbl, "systemd", "exclude", cfg.exclude, "strs")
    cfg.units = [u.removesuffix(".service") for u in _val(ctx, tbl, "systemd", "units", [], "strs")]
    cfg.alert_on = _val(ctx, tbl, "systemd", "alert_on", cfg.alert_on, "strs", choices=ALERT_ON_CHOICES)
    cfg.restart_alert = _val(ctx, tbl, "systemd", "restart_alert", cfg.restart_alert, "bool")
    cfg.watch = _parse_rules(ctx, _as_list_of_tables(ctx, tbl, "watch", "systemd.watch"), "systemd.watch")
    for rule in cfg.watch:
        rule.match = rule.match.removesuffix(".service")
    return cfg


def _parse_docker(ctx: _Ctx, data: Dict[str, Any]) -> DockerCfg:
    tbl = _as_table(ctx, data, "docker", "docker")
    _check_keys(ctx, tbl, "docker", [
        "enabled", "discover", "include", "exclude", "containers", "alert_on", "restart_alert", "watch",
    ])
    cfg = DockerCfg()
    cfg.enabled = _val(ctx, tbl, "docker", "enabled", cfg.enabled, "bool")
    cfg.discover = _val(ctx, tbl, "docker", "discover", cfg.discover, "str", choices=("all", "off"))
    cfg.include = _val(ctx, tbl, "docker", "include", cfg.include, "strs")
    cfg.exclude = _val(ctx, tbl, "docker", "exclude", cfg.exclude, "strs")
    cfg.containers = _val(ctx, tbl, "docker", "containers", [], "strs")
    cfg.alert_on = _val(ctx, tbl, "docker", "alert_on", cfg.alert_on, "strs", choices=ALERT_ON_CHOICES)
    cfg.restart_alert = _val(ctx, tbl, "docker", "restart_alert", cfg.restart_alert, "bool")
    cfg.watch = _parse_rules(ctx, _as_list_of_tables(ctx, tbl, "watch", "docker.watch"), "docker.watch")
    return cfg


def _parse_checks(ctx: _Ctx, data: Dict[str, Any]) -> tuple:
    http: List[HttpCheck] = []
    for i, tbl in enumerate(_as_list_of_tables(ctx, data, "http", "http")):
        w = f"http[{i}]"
        _check_keys(ctx, tbl, w, [
            "name", "url", "expect_status", "contains", "timeout", "fail_threshold", "verify_tls",
            "every", "label", "notify", "remind_after",
        ])
        name = _val(ctx, tbl, w, "name", "", "str")
        url = _val(ctx, tbl, w, "url", "", "str")
        if not name or not url:
            ctx.err(w, "'name' and 'url' are required")
            continue
        if not re.match(r"^https?://", url):
            ctx.err(f"{w}.url", "must start with http:// or https://")
            continue
        status = tbl.get("expect_status", 200)
        statuses = status if isinstance(status, list) else [status]
        if not statuses or not all(isinstance(s, int) and not isinstance(s, bool) for s in statuses):
            ctx.err(f"{w}.expect_status", "must be an integer or a list of integers")
            statuses = [200]
        http.append(HttpCheck(
            name=name, url=url, expect_status=list(statuses),
            contains=_val(ctx, tbl, w, "contains", "", "str"),
            timeout=_val(ctx, tbl, w, "timeout", 10, "int", minimum=1),
            fail_threshold=_val(ctx, tbl, w, "fail_threshold", 3, "int", minimum=1),
            verify_tls=_val(ctx, tbl, w, "verify_tls", True, "bool"),
            every=_val(ctx, tbl, w, "every", 0, "int", minimum=0),
            label=_val(ctx, tbl, w, "label", None, "str"),
            notify=_val(ctx, tbl, w, "notify", None, "strs"),
            remind_after=_val(ctx, tbl, w, "remind_after", None, "int", minimum=0),
        ))
    tcp: List[TcpCheck] = []
    for i, tbl in enumerate(_as_list_of_tables(ctx, data, "tcp", "tcp")):
        w = f"tcp[{i}]"
        _check_keys(ctx, tbl, w, [
            "name", "host", "port", "timeout", "fail_threshold", "every", "label", "notify", "remind_after",
        ])
        name = _val(ctx, tbl, w, "name", "", "str")
        host = _val(ctx, tbl, w, "host", "127.0.0.1", "str")
        port = _val(ctx, tbl, w, "port", 0, "int", minimum=1, maximum=65535)
        if not name or not port:
            ctx.err(w, "'name' and 'port' are required")
            continue
        tcp.append(TcpCheck(
            name=name, host=host, port=port,
            timeout=_val(ctx, tbl, w, "timeout", 5, "int", minimum=1),
            fail_threshold=_val(ctx, tbl, w, "fail_threshold", 2, "int", minimum=1),
            every=_val(ctx, tbl, w, "every", 0, "int", minimum=0),
            label=_val(ctx, tbl, w, "label", None, "str"),
            notify=_val(ctx, tbl, w, "notify", None, "strs"),
            remind_after=_val(ctx, tbl, w, "remind_after", None, "int", minimum=0),
        ))
    procs: List[ProcessCheck] = []
    for i, tbl in enumerate(_as_list_of_tables(ctx, data, "process", "process")):
        w = f"process[{i}]"
        _check_keys(ctx, tbl, w, [
            "name", "pattern", "min_count", "max_count", "fail_threshold", "every", "label", "notify", "remind_after",
        ])
        name = _val(ctx, tbl, w, "name", "", "str")
        pattern = _val(ctx, tbl, w, "pattern", "", "str")
        if not name or not pattern:
            ctx.err(w, "'name' and 'pattern' are required (pattern is matched against the full command line)")
            continue
        procs.append(ProcessCheck(
            name=name, pattern=pattern,
            min_count=_val(ctx, tbl, w, "min_count", 1, "int", minimum=0),
            max_count=_val(ctx, tbl, w, "max_count", 0, "int", minimum=0),
            fail_threshold=_val(ctx, tbl, w, "fail_threshold", 2, "int", minimum=1),
            every=_val(ctx, tbl, w, "every", 0, "int", minimum=0),
            label=_val(ctx, tbl, w, "label", None, "str"),
            notify=_val(ctx, tbl, w, "notify", None, "strs"),
            remind_after=_val(ctx, tbl, w, "remind_after", None, "int", minimum=0),
        ))
    return http, tcp, procs


def _parse_notify(ctx: _Ctx, data: Dict[str, Any]) -> NotifyCfg:
    tbl = _as_table(ctx, data, "notify", "notify")
    _check_keys(ctx, tbl, "notify", ["console", "telegram", "email", "webhook"])
    cfg = NotifyCfg()
    cfg.console = _val(ctx, tbl, "notify", "console", True, "bool")
    min_sev = tuple(SEVERITY_RANK)

    for i, t in enumerate(_as_list_of_tables(ctx, tbl, "telegram", "notify.telegram")):
        w = f"notify.telegram[{i}]" if isinstance(tbl.get("telegram"), list) else "notify.telegram"
        _check_keys(ctx, t, w, [
            "enabled", "name", "bot_token", "chat_id", "api_base", "thread_id", "timeout", "silent",
            "min_severity", "commands", "allowed_chats",
        ])
        token = _val(ctx, t, w, "bot_token", "", "str")
        chat = t.get("chat_id", "")
        if isinstance(chat, int) and not isinstance(chat, bool):
            chat = str(chat)
        if not token or not chat or not isinstance(chat, str):
            ctx.err(w, "'bot_token' and 'chat_id' are required (run `svcwatch telegram-setup` to find your chat_id)")
            continue
        allowed = t.get("allowed_chats", [])
        allowed = [str(a) for a in allowed] if isinstance(allowed, list) else []
        name = _val(ctx, t, w, "name", "telegram" if i == 0 else f"telegram-{i + 1}", "str")
        cfg.telegram.append(TelegramCfg(
            name=name, bot_token=token, chat_id=chat,
            api_base=_val(ctx, t, w, "api_base", "https://api.telegram.org", "str").rstrip("/"),
            thread_id=_val(ctx, t, w, "thread_id", None, "int"),
            timeout=_val(ctx, t, w, "timeout", 15, "int", minimum=1),
            silent=_val(ctx, t, w, "silent", False, "bool"),
            min_severity=_val(ctx, t, w, "min_severity", "info", "str", choices=min_sev),
            commands=_val(ctx, t, w, "commands", False, "bool"),
            allowed_chats=allowed,
        ))

    for i, t in enumerate(_as_list_of_tables(ctx, tbl, "email", "notify.email")):
        w = f"notify.email[{i}]" if isinstance(tbl.get("email"), list) else "notify.email"
        _check_keys(ctx, t, w, [
            "enabled", "name", "host", "port", "user", "password", "sender", "to", "use_ssl", "starttls",
            "timeout", "min_severity",
        ])
        to = _val(ctx, t, w, "to", [], "strs")
        host = _val(ctx, t, w, "host", "", "str")
        sender = _val(ctx, t, w, "sender", "", "str")
        if not host or not sender or not to:
            ctx.err(w, "'host', 'sender' and 'to' are required")
            continue
        cfg.email.append(EmailCfg(
            name=_val(ctx, t, w, "name", "email" if i == 0 else f"email-{i + 1}", "str"),
            host=host, port=_val(ctx, t, w, "port", 587, "int", minimum=1, maximum=65535),
            sender=sender, to=to,
            user=_val(ctx, t, w, "user", "", "str"), password=_val(ctx, t, w, "password", "", "str"),
            use_ssl=_val(ctx, t, w, "use_ssl", False, "bool"),
            starttls=_val(ctx, t, w, "starttls", True, "bool"),
            timeout=_val(ctx, t, w, "timeout", 30, "int", minimum=1),
            min_severity=_val(ctx, t, w, "min_severity", "info", "str", choices=min_sev),
        ))

    for i, t in enumerate(_as_list_of_tables(ctx, tbl, "webhook", "notify.webhook")):
        w = f"notify.webhook[{i}]" if isinstance(tbl.get("webhook"), list) else "notify.webhook"
        _check_keys(ctx, t, w, ["enabled", "name", "url", "headers", "timeout", "min_severity"])
        url = _val(ctx, t, w, "url", "", "str")
        if not re.match(r"^https?://", url or ""):
            ctx.err(f"{w}.url", "is required and must start with http:// or https://")
            continue
        cfg.webhook.append(WebhookCfg(
            name=_val(ctx, t, w, "name", "webhook" if i == 0 else f"webhook-{i + 1}", "str"),
            url=url, headers=_val(ctx, t, w, "headers", {}, "strmap"),
            timeout=_val(ctx, t, w, "timeout", 15, "int", minimum=1),
            min_severity=_val(ctx, t, w, "min_severity", "info", "str", choices=min_sev),
        ))

    seen = set()
    for name in cfg.names():
        if name in seen:
            ctx.err("notify", f"notifier name {name!r} is used twice; give each channel a unique 'name'")
        seen.add(name)
    return cfg


def _validate_routes(ctx: _Ctx, cfg: Config) -> None:
    known = set(cfg.notify.names())
    routed: List[tuple] = []
    for rule in cfg.systemd.watch:
        routed.append((f"systemd.watch[{rule.match}]", rule.notify))
    for rule in cfg.docker.watch:
        routed.append((f"docker.watch[{rule.match}]", rule.notify))
    for chk in cfg.http:
        routed.append((f"http[{chk.name}]", chk.notify))
    for tcp in cfg.tcp:
        routed.append((f"tcp[{tcp.name}]", tcp.notify))
    for proc in cfg.process:
        routed.append((f"process[{proc.name}]", proc.notify))
    for where, notify in routed:
        for name in notify or []:
            if name not in known:
                hint = difflib.get_close_matches(name, sorted(known), n=1)
                more = f" (did you mean '{hint[0]}'?)" if hint else f" (configured: {', '.join(sorted(known)) or 'none'})"
                ctx.err(f"{where}.notify", f"unknown notifier {name!r}{more}")
    names = [c.name for c in cfg.http] + [c.name for c in cfg.tcp] + [c.name for c in cfg.process]
    for dup in {n for n in names if names.count(n) > 1}:
        ctx.err("checks", f"check name {dup!r} is used more than once")


def parse_config(data: Dict[str, Any], env: Optional[Dict[str, str]] = None, base_dir: Optional[Path] = None) -> Config:
    ctx = _Ctx()
    env = dict(os.environ) if env is None else env
    data = dict(data)
    _drop_disabled_notifiers(data)
    data = _interpolate(data, env, "", ctx)
    _check_keys(ctx, data, "", ["monitor", "logs", "systemd", "docker", "http", "tcp", "process", "notify"])

    cfg = Config()
    mon = _as_table(ctx, data, "monitor", "monitor")
    _check_keys(ctx, mon, "monitor", [
        "interval", "dedup_window", "summary_interval", "summary_when_empty", "remind_after",
        "rate_limit_per_hour", "initial_lookback", "max_backlog_lines", "state_file", "log_file",
        "hostname", "title_prefix",
    ])
    m = cfg.monitor
    m.interval = _val(ctx, mon, "monitor", "interval", m.interval, "int", minimum=1)
    m.dedup_window = _val(ctx, mon, "monitor", "dedup_window", m.dedup_window, "int", minimum=0)
    m.summary_interval = _val(ctx, mon, "monitor", "summary_interval", m.summary_interval, "int", minimum=0)
    m.summary_when_empty = _val(ctx, mon, "monitor", "summary_when_empty", m.summary_when_empty, "bool")
    m.remind_after = _val(ctx, mon, "monitor", "remind_after", m.remind_after, "int", minimum=0)
    m.rate_limit_per_hour = _val(ctx, mon, "monitor", "rate_limit_per_hour", m.rate_limit_per_hour, "int", minimum=0)
    m.initial_lookback = _val(ctx, mon, "monitor", "initial_lookback", m.initial_lookback, "int", minimum=0)
    m.max_backlog_lines = _val(ctx, mon, "monitor", "max_backlog_lines", m.max_backlog_lines, "int", minimum=10)
    m.hostname = _val(ctx, mon, "monitor", "hostname", m.hostname, "str")
    m.title_prefix = _val(ctx, mon, "monitor", "title_prefix", m.title_prefix, "str")
    base = base_dir or Path.cwd()
    state_file = Path(_val(ctx, mon, "monitor", "state_file", str(m.state_file), "str"))
    m.state_file = state_file if state_file.is_absolute() else base / state_file
    log_file = _val(ctx, mon, "monitor", "log_file", "", "str")
    if log_file:
        m.log_file = Path(log_file) if Path(log_file).is_absolute() else base / log_file

    logs = _as_table(ctx, data, "logs", "logs")
    _check_keys(ctx, logs, "logs", [
        "immediate", "external", "ignore", "context_lines", "max_lines_per_alert", "journal_priority",
    ])
    lg = cfg.logs
    lg.immediate = _val(ctx, logs, "logs", "immediate", lg.immediate, "strs")
    lg.external = _val(ctx, logs, "logs", "external", lg.external, "strs")
    lg.ignore = _val(ctx, logs, "logs", "ignore", lg.ignore, "strs")
    lg.context_lines = _val(ctx, logs, "logs", "context_lines", lg.context_lines, "int", minimum=0, maximum=50)
    lg.max_lines_per_alert = _val(ctx, logs, "logs", "max_lines_per_alert", lg.max_lines_per_alert, "int", minimum=1)
    lg.journal_priority = _val(ctx, logs, "logs", "journal_priority", lg.journal_priority, "int", minimum=0, maximum=7)
    for attr in ("immediate", "external", "ignore"):
        _check_patterns(ctx, getattr(lg, attr), f"logs.{attr}")

    cfg.systemd = _parse_systemd(ctx, data)
    cfg.docker = _parse_docker(ctx, data)
    cfg.http, cfg.tcp, cfg.process = _parse_checks(ctx, data)
    cfg.notify = _parse_notify(ctx, data)
    _validate_routes(ctx, cfg)

    if ctx.errors:
        raise ConfigError("\n".join(f"  - {e}" for e in ctx.errors))
    return cfg


def load_config(path: Path, env_file: Optional[Path] = None, env: Optional[Dict[str, str]] = None) -> Config:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}  (create one with `svcwatch init`)")
    merged: Dict[str, str] = {}
    candidates = [env_file] if env_file else [path.parent / ".env"]
    for cand in candidates:
        if cand and Path(cand).is_file():
            merged.update(parse_env_file(Path(cand)))
        elif cand and env_file:
            raise ConfigError(f"env file not found: {cand}")
    merged.update(os.environ if env is None else env)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path.name} is not valid TOML: {exc}") from exc
    cfg = parse_config(data, merged, path.parent.resolve())
    cfg.path = path.resolve()
    return cfg
