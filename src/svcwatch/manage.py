from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from .config import Config, ConfigError, load_config, parse_config, parse_env_file
from .probes import check_http, check_process, check_tcp
from .runner import CommandError, Runner
from .systemd import Systemd
from .telegram import TelegramClient, TelegramError
from .template import q, render_check_block, render_rule_block, render_telegram_block
from .wizard import IO, SetupArgs, _find_chat, _get_bot, _upsert_env, is_root

SEVERITIES = ("info", "warning", "critical")


def _promote_single_telegram(text: str) -> str:
    if "[[notify.telegram]]" in text:
        return text
    return re.sub(r"(?m)^\[notify\.telegram\](?=[ \t]*(?:#.*)?$)", "[[notify.telegram]]", text, count=1)


def _enable_docker(text: str) -> str:
    if re.search(r"(?m)^\[docker\][ \t]*(?:#.*)?$", text):
        pattern = re.compile(r"(?ms)(^\[docker\][^\n]*\n(?:(?!^\[)[^\n]*\n)*?)([ \t]*enabled[ \t]*=[ \t]*)false")
        new, n = pattern.subn(r"\1\2true", text, count=1)
        if n or re.search(r"(?ms)^\[docker\][^\n]*\n(?:(?!^\[)[^\n]*\n)*?[ \t]*enabled[ \t]*=[ \t]*true", text):
            return new
        return re.sub(r"(?m)^(\[docker\][^\n]*\n)", r"\1enabled = true\n", text, count=1)
    return text.rstrip("\n") + "\n\n[docker]\nenabled = true\n"


def _env_for(cfg_path: Path, env_file: Optional[Path], extra: Dict[str, str]) -> Dict[str, str]:
    merged: Dict[str, str] = {}
    env_path = env_file or (cfg_path.parent / ".env")
    if Path(env_path).is_file():
        merged.update(parse_env_file(Path(env_path)))
    merged.update(os.environ)
    merged.update(extra)
    return merged


def apply_edit(
    cfg_path: Path,
    blocks: List[str],
    *,
    env_updates: Optional[Dict[str, str]] = None,
    env_file: Optional[Path] = None,
    enable_docker: bool = False,
) -> Config:
    cfg_path = Path(cfg_path)
    env_updates = env_updates or {}
    original = cfg_path.read_text(encoding="utf-8")
    text = original
    if any(b.lstrip().startswith("[[notify.telegram]]") for b in blocks):
        text = _promote_single_telegram(text)
    if enable_docker:
        text = _enable_docker(text)
    text = text.rstrip("\n") + "\n\n" + "\n".join(b.strip("\n") + "\n" for b in blocks)
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"the change would make {cfg_path.name} invalid TOML ({exc}); nothing was changed") from exc
    try:
        new_cfg = parse_config(data, _env_for(cfg_path, env_file, env_updates), cfg_path.parent)
    except ConfigError as exc:
        raise ConfigError(f"the change would make {cfg_path.name} invalid; nothing was changed:\n{exc}") from exc

    backup = cfg_path.with_name(cfg_path.name + ".bak")
    backup.write_text(original, encoding="utf-8")
    tmp = cfg_path.with_name(cfg_path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, cfg_path)
    for key, value in env_updates.items():
        _upsert_env(Path(env_file) if env_file else cfg_path.parent / ".env", key, value)
    new_cfg.path = cfg_path.resolve()
    return new_cfg


def restart_service(io: IO, runner: Runner, assume_yes: bool = True) -> None:
    try:
        active = runner.run(["systemctl", "is-active", "--quiet", "svcwatch"], timeout=15).returncode == 0
    except CommandError:
        return
    if not active:
        io.say("  (the svcwatch service is not running; start it with `sudo systemctl enable --now svcwatch`)")
        return
    if not is_root():
        io.say("  Apply it now:  sudo systemctl restart svcwatch")
        return
    if assume_yes or io.confirm("Restart the svcwatch service to apply the change?", default=True):
        res = runner.run(["systemctl", "restart", "svcwatch"], timeout=60)
        io.say("  ok svcwatch restarted" if res.returncode == 0 else f"  x restart failed: {res.stderr.strip()}")


@dataclass
class AddTelegramArgs:
    config: Optional[Path] = None
    env_file: Optional[Path] = None
    name: Optional[str] = None
    token: Optional[str] = None
    chat_id: Optional[str] = None
    thread_id: Optional[int] = None
    min_severity: str = "info"
    commands: bool = True
    api_base: str = "https://api.telegram.org"
    wait: int = 120
    yes: bool = False


def _free_name(existing: List[str], base: str) -> str:
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def _token_env_name(name: str) -> str:
    return "TELEGRAM_BOT_TOKEN_" + re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()


def add_telegram(cfg_path: Path, args: AddTelegramArgs, io: Optional[IO] = None,
                 runner: Optional[Runner] = None) -> int:
    io = io or IO()
    runner = runner or Runner()
    cfg = load_config(cfg_path, env_file=args.env_file)
    existing = cfg.notify.names()

    suggestion = _free_name(existing, "telegram")
    name = args.name or (suggestion if args.yes else io.ask("Name for this channel (used in `notify = [...]`)",
                                                            default=suggestion))
    if not re.match(r"^[A-Za-z0-9_.-]+$", name):
        io.say("  x The name may only contain letters, digits, '_', '-' and '.'")
        return 1
    if name in existing:
        io.say(f"  x A notifier called '{name}' already exists. Pick another name (--name).")
        return 1

    sargs = SetupArgs(token=args.token, chat_id=args.chat_id, yes=args.yes, wait=args.wait, api_base=args.api_base)
    known_tokens = _known_tokens(cfg_path, args.env_file)
    if not args.token and known_tokens:
        first_var = next(iter(known_tokens))
        if args.yes or io.confirm(f"Use the same bot as the existing channel ({first_var})?", default=True):
            sargs.token = known_tokens[first_var]
    token, me = _get_bot(io, sargs, args.api_base)
    if not token:
        return 1
    client = TelegramClient(token, args.api_base)
    chat_id = _find_chat(io, client, me, sargs)
    if not chat_id:
        return 1
    try:
        client.send_message(chat_id, f"✅ <b>svcwatch</b>: channel <code>{name}</code> connected.",
                            thread_id=args.thread_id)
    except TelegramError as exc:
        io.say(f"  x Could not send a message: {exc}")
        return 1
    io.say("  ok Test message sent - check Telegram.")

    var = next((v for v, t in known_tokens.items() if t == token), None) or _token_env_name(name)
    block = render_telegram_block(
        name=name, token_env=var, chat_id=chat_id, thread_id=args.thread_id,
        min_severity=args.min_severity, commands=args.commands, api_base=args.api_base)
    updates = {} if var in known_tokens else {var: token}
    apply_edit(cfg_path, [block], env_updates=updates, env_file=args.env_file)
    io.say(f"  ok Added channel '{name}' to {cfg_path}")
    io.say(f"     Route alerts to it per service with:  notify = [\"{name}\"]   (or `svcwatch watch ... --notify {name}`)")
    restart_service(io, runner)
    return 0


def _known_tokens(cfg_path: Path, env_file: Optional[Path]) -> Dict[str, str]:
    env_path = Path(env_file) if env_file else cfg_path.parent / ".env"
    if not env_path.is_file():
        return {}
    return {k: v for k, v in parse_env_file(env_path).items() if k.startswith("TELEGRAM_BOT_TOKEN") and v}


@dataclass
class WatchArgs:
    config: Optional[Path] = None
    env_file: Optional[Path] = None
    names: List[str] = field(default_factory=list)
    docker: bool = False
    http: Optional[str] = None
    tcp: Optional[str] = None
    process: Optional[str] = None
    name: Optional[str] = None
    label: Optional[str] = None
    notify: List[str] = field(default_factory=list)
    inactive: bool = False
    no_logs: bool = False
    threshold: Optional[int] = None
    contains: Optional[str] = None
    every: Optional[int] = None
    min_count: Optional[int] = None
    force: bool = False
    yes: bool = False


def _slug(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return slug[:40] or "check"


def add_watch(cfg_path: Path, args: WatchArgs, io: Optional[IO] = None, runner: Optional[Runner] = None) -> int:
    io = io or IO()
    runner = runner or Runner()
    cfg = load_config(cfg_path, env_file=args.env_file)

    kinds = [bool(args.names), bool(args.http), bool(args.tcp), bool(args.process)]
    if sum(kinds) != 1:
        io.say("  x Say what to watch: a service/container name, or one of --http URL, --tcp HOST:PORT, --process PATTERN")
        return 1
    if args.docker and not args.names:
        io.say("  x --docker needs at least one container name")
        return 1
    known = set(cfg.notify.names())
    for n in args.notify:
        if n not in known:
            io.say(f"  x Unknown notifier '{n}'. Configured: {', '.join(sorted(known)) or 'none'}")
            return 1

    blocks: List[str] = []
    enable_docker = False
    notify = args.notify or None

    if args.names:
        section = "docker" if args.docker else "systemd"
        already = {r.match for r in (cfg.docker if args.docker else cfg.systemd).watch}
        for target in args.names:
            target = target.removesuffix(".service") if not args.docker else target
            if target in already:
                io.say(f"  = '{target}' already has a rule in the config - not adding a second one")
                continue
            problem = _verify_target(runner, cfg, target, args.docker)
            if problem and not args.force:
                io.say(f"  x {problem}  (use --force to add it anyway)")
                return 1
            if problem:
                io.say(f"  ! {problem} - adding anyway")
            blocks.append(render_rule_block(
                section, target, label=args.label if len(args.names) == 1 else None, notify=notify,
                inactive=args.inactive, logs=not args.no_logs, fail_threshold=args.threshold))
            io.say(f"  + {section}: {target}")
        enable_docker = args.docker and not cfg.docker.enabled
    elif args.http:
        name = args.name or _slug(args.http.split("://", 1)[-1])
        _reject_dupe_check(cfg, name, io)
        fields: Dict[str, Any] = {"url": args.http}
        if args.contains:
            fields["contains"] = args.contains
        if args.threshold:
            fields["fail_threshold"] = args.threshold
        if args.every:
            fields["every"] = args.every
        blocks.append(render_check_block("http", name, fields, label=args.label, notify=notify))
        _probe_now(io, "http", cfg, name, args)
    elif args.tcp:
        host, _, port = args.tcp.rpartition(":")
        if not port.isdigit() or not 0 < int(port) < 65536:
            io.say("  x --tcp expects HOST:PORT (or just PORT for this machine), e.g. 127.0.0.1:5432")
            return 1
        name = args.name or _slug(f"tcp-{host or 'localhost'}-{port}")
        _reject_dupe_check(cfg, name, io)
        fields = {"host": host or "127.0.0.1", "port": int(port)}
        if args.threshold:
            fields["fail_threshold"] = args.threshold
        if args.every:
            fields["every"] = args.every
        blocks.append(render_check_block("tcp", name, fields, label=args.label, notify=notify))
        _probe_now(io, "tcp", cfg, name, args)
    else:
        name = args.name or _slug(args.process or "process")
        _reject_dupe_check(cfg, name, io)
        fields = {"pattern": args.process}
        if args.min_count is not None:
            fields["min_count"] = args.min_count
        if args.threshold:
            fields["fail_threshold"] = args.threshold
        if args.every:
            fields["every"] = args.every
        blocks.append(render_check_block("process", name, fields, label=args.label, notify=notify))
        _probe_now(io, "process", cfg, name, args, runner)

    if not blocks:
        io.say("Nothing to add.")
        return 0
    apply_edit(cfg_path, blocks, env_file=args.env_file, enable_docker=enable_docker)
    io.say(f"  ok Updated {cfg_path}  (previous version: {cfg_path.name}.bak)")
    restart_service(io, runner)
    return 0


def _reject_dupe_check(cfg: Config, name: str, io: IO) -> None:
    taken = {c.name for c in cfg.http} | {c.name for c in cfg.tcp} | {c.name for c in cfg.process}
    if name in taken:
        raise ConfigError(f"a check called '{name}' already exists; choose another with --name")


def _verify_target(runner: Runner, cfg: Config, target: str, docker: bool) -> Optional[str]:
    try:
        if docker:
            from .dockerx import Docker
            if Docker(runner, cfg.docker).state(target).missing:
                return f"container '{target}' does not exist"
            return None
        if Systemd(runner, cfg.systemd).state(target).missing:
            return f"service '{target}.service' does not exist (check the name: systemctl list-units)"
    except CommandError:
        return None
    return None


def _probe_now(io: IO, kind: str, cfg: Config, name: str, args: WatchArgs, runner: Optional[Runner] = None) -> None:
    from .config import HttpCheck, ProcessCheck, TcpCheck
    if kind == "http":
        chk = HttpCheck(name=name, url=args.http or "", contains=args.contains or "", timeout=10)
        ok, detail = check_http(chk)
    elif kind == "tcp":
        host, _, port = (args.tcp or "").rpartition(":")
        ok, detail = check_tcp(TcpCheck(name=name, host=host or "127.0.0.1", port=int(port)))
    else:
        ok, detail = check_process(ProcessCheck(name=name, pattern=args.process or "",
                                                min_count=args.min_count if args.min_count is not None else 1),
                                   runner or Runner())
    io.say(f"  + {kind}: {name}")
    io.say(f"    right now: {'OK' if ok else 'DOWN'} - {detail}")
    if not ok:
        io.say("    (it is added anyway; you will be alerted if it stays down)")


__all__ = ["AddTelegramArgs", "WatchArgs", "add_telegram", "add_watch", "apply_edit", "restart_service", "q"]
