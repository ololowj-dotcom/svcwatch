from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import socket
import sys
import time
from pathlib import Path
from typing import List, Optional

from . import __version__
from .config import Config, ConfigError, load_config
from .manage import SEVERITIES, AddTelegramArgs, WatchArgs, add_telegram, add_watch
from .models import INFO, Event
from .monitor import Monitor, clear_mute, mute_until, parse_duration, set_mute
from .notifiers import build_notifiers
from .telegram import TelegramClient, TelegramError, chats_from_updates
from .template import render_config, render_unit
from .wizard import IO, SetupArgs, default_dir, install_service, run_setup

log = logging.getLogger("svcwatch")

SEARCH_PATHS = [
    Path("svcwatch.toml"),
    Path("/etc/svcwatch/svcwatch.toml"),
    Path.home() / ".config" / "svcwatch" / "svcwatch.toml",
]


def find_config(explicit: Optional[str]) -> Path:
    candidates = [Path(explicit)] if explicit else []
    if not explicit and os.environ.get("SVCWATCH_CONFIG"):
        candidates.append(Path(os.environ["SVCWATCH_CONFIG"]))
    if not candidates:
        candidates = SEARCH_PATHS
    for cand in candidates:
        if cand.is_file():
            return cand
    if explicit:
        raise ConfigError(f"config file not found: {explicit}")
    raise ConfigError("no config found. Run `svcwatch setup` (guided) or `svcwatch init` (template).")


def setup_logging(cfg: Optional[Config], verbose: bool = False) -> None:
    root = logging.getLogger("svcwatch")
    root.handlers.clear()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    root.addHandler(stream)
    if cfg and cfg.monitor.log_file:
        cfg.monitor.log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            cfg.monitor.log_file, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    root.propagate = False


def _load(args: argparse.Namespace) -> Config:
    cfg = load_config(find_config(args.config), env_file=Path(args.env_file) if args.env_file else None)
    setup_logging(cfg, getattr(args, "verbose", False))
    return cfg


def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.path)
    if target.exists() and not args.force:
        print(f"{target} already exists (use --force to overwrite)")
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    state = "/var/lib/svcwatch/state.json" if target.resolve().parent == Path("/etc/svcwatch") else "svcwatch-state.json"
    target.write_text(render_config(state_file=state), encoding="utf-8")
    print(f"Wrote {target}. Edit it, then run:  svcwatch check --config {target}")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    setup_logging(None)
    return run_setup(SetupArgs(
        directory=Path(args.dir) if args.dir else None, token=args.token, chat_id=args.chat_id,
        yes=args.yes, force=args.force, no_service=args.no_service, wait=args.wait, api_base=args.api_base,
    ))


def cmd_check(args: argparse.Namespace) -> int:
    cfg = _load(args)
    mon = Monitor(cfg, notifiers=[], dry_run=True)
    print(f"svcwatch {__version__}  config: {cfg.path}")
    print("Notifiers: " + (", ".join(n.name for n in build_notifiers(cfg.notify)) or "none"))
    mon.tick()
    if not mon.status:
        print("\nNothing is being watched. Add units in svcwatch.toml or enable discovery.")
        return 1
    print(f"\n{len(mon.status)} targets:")
    width = max(len(k) for k in mon.status)
    down = 0
    for key, st in sorted(mon.status.items()):
        mark = "OK  " if st["ok"] else "DOWN"
        down += 0 if st["ok"] else 1
        print(f"  {mark} {key.ljust(width)}  {st['detail']}")
    remaining = mute_until(cfg.mute_file) - time.time()
    if remaining > 0:
        print(f"\nAlerts are muted for another {int(remaining // 60)} min.")
    return 2 if down else 0


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _load(args)
    mon = Monitor(cfg, dry_run=args.dry_run, notifiers=None if not args.dry_run else [])
    if args.once:
        events = mon.tick()
        for ev in events:
            print(f"[{ev.severity}] {ev.title}\n{ev.body}\n")
        print(f"{len(events)} event(s).")
        return 0
    mon.run()
    return 0


def cmd_test_notify(args: argparse.Namespace) -> int:
    cfg = _load(args)
    notifiers = build_notifiers(cfg.notify)
    host = cfg.monitor.hostname or socket.gethostname()
    event = Event("info", "test", INFO, "Test alert from svcwatch",
                  "If you can read this, this channel works.", time.time())
    failed = 0
    for n in notifiers:
        try:
            n.send(event, host, cfg.monitor.title_prefix)
            print(f"  OK    {n.name}")
        except Exception as exc:
            failed += 1
            print(f"  FAIL  {n.name}: {exc}")
    return 1 if failed else 0


def cmd_telegram_setup(args: argparse.Namespace) -> int:
    token = args.token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("Give the bot token: svcwatch telegram-setup --token 123:ABC   (or set TELEGRAM_BOT_TOKEN)")
        return 1
    client = TelegramClient(token, args.api_base)
    try:
        me = client.get_me()
        print(f"Bot: @{me.get('username')}")
        print(f"Now open https://t.me/{me.get('username')}, press START (or write in your group) ...")
        deadline = time.time() + args.wait
        chats: List[dict] = []
        offset = None
        while time.time() < deadline and not chats:
            began = time.time()
            updates = client.get_updates(offset, timeout=5)
            if updates:
                offset = updates[-1]["update_id"] + 1
            chats = chats_from_updates(updates)
            if not chats and time.time() - began < 1:
                time.sleep(1)
    except TelegramError as exc:
        print(f"x {exc}")
        return 1
    if not chats:
        print("Nothing received.")
        return 1
    for chat in chats:
        print(f"  chat_id = {chat['id']}   ({chat['title']}, {chat['type']})")
    print('\nPut it in svcwatch.toml:\n  [notify.telegram]\n  bot_token = "${TELEGRAM_BOT_TOKEN}"\n'
          f'  chat_id = "{chats[0]["id"]}"')
    return 0


def cmd_mute(args: argparse.Namespace) -> int:
    cfg = _load(args)
    seconds = parse_duration(args.duration, "m")
    if not seconds:
        print("Duration examples: 30m, 2h, 1d")
        return 1
    set_mute(cfg.mute_file, time.time() + seconds)
    print(f"Alerts muted for {args.duration}. `svcwatch unmute` to resume.")
    return 0


def cmd_unmute(args: argparse.Namespace) -> int:
    cfg = _load(args)
    clear_mute(cfg.mute_file)
    print("Alerts resumed.")
    return 0


def _config_path(args: argparse.Namespace) -> Path:
    path = find_config(args.config)
    setup_logging(None)
    return path


def cmd_add_telegram(args: argparse.Namespace) -> int:
    return add_telegram(_config_path(args), AddTelegramArgs(
        config=Path(args.config) if args.config else None,
        env_file=Path(args.env_file) if args.env_file else None,
        name=args.name, token=args.token, chat_id=args.chat_id, thread_id=args.thread_id,
        min_severity=args.min_severity, commands=not args.no_commands, api_base=args.api_base,
        wait=args.wait, yes=args.yes,
    ))


def cmd_watch(args: argparse.Namespace) -> int:
    return add_watch(_config_path(args), WatchArgs(
        env_file=Path(args.env_file) if args.env_file else None,
        names=args.targets, docker=args.docker, http=args.http, tcp=args.tcp, process=args.process,
        name=args.name, label=args.label, notify=args.notify or [], inactive=args.inactive,
        no_logs=args.no_logs, threshold=args.threshold, contains=args.contains, every=args.every,
        min_count=args.min_count, force=args.force, yes=args.yes,
    ))


def cmd_install_service(args: argparse.Namespace) -> int:
    path = str(Path(args.config or default_dir() / "svcwatch.toml").resolve())
    if not args.write:
        sys.stdout.write(render_unit(path))
        print("\n# Save as /etc/systemd/system/svcwatch.service, or run:  sudo svcwatch install-service --write --start")
        return 0
    return install_service(path, start=args.start, io=IO())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="svcwatch",
        description="Watchdog for Linux services: systemd, Docker, processes, HTTP and TCP. Alerts to Telegram, email or webhooks.",
    )
    parser.add_argument("--version", action="version", version=f"svcwatch {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("-c", "--config", help="path to svcwatch.toml (default: ./svcwatch.toml, /etc/svcwatch/, ~/.config/svcwatch/)")
        p.add_argument("--env-file", help="file with secrets (default: .env next to the config)")
        p.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    p = sub.add_parser("setup", help="guided setup: connect a Telegram bot, pick services, install the service")
    p.add_argument("--dir", help="where to write the config (default: /etc/svcwatch as root, else ~/.config/svcwatch)")
    p.add_argument("--token", help="bot token (or env TELEGRAM_BOT_TOKEN)")
    p.add_argument("--chat-id", help="skip chat discovery and use this chat id")
    p.add_argument("-y", "--yes", action="store_true", help="no questions, accept defaults")
    p.add_argument("--force", action="store_true", help="overwrite an existing config (backup is kept)")
    p.add_argument("--no-service", action="store_true", help="do not offer to install the systemd service")
    p.add_argument("--wait", type=int, default=120, help="seconds to wait for you to press START (default 120)")
    p.add_argument("--api-base", default="https://api.telegram.org",
                   help="Telegram API address, e.g. your own relay if api.telegram.org is unreliable")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("init", help="write an annotated config template")
    p.add_argument("path", nargs="?", default="svcwatch.toml")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("check", help="validate the config and show what is watched and its state now")
    common(p)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("run", help="run the watchdog")
    common(p)
    p.add_argument("--once", action="store_true", help="one cycle, print the events, exit")
    p.add_argument("--dry-run", action="store_true", help="never send or save anything (use with --once)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("test-notify", help="send a test alert to every configured channel")
    common(p)
    p.set_defaults(func=cmd_test_notify)

    p = sub.add_parser("telegram-setup", help="find your Telegram chat_id for a bot token")
    p.add_argument("--token")
    p.add_argument("--wait", type=int, default=120)
    p.add_argument("--api-base", default="https://api.telegram.org")
    p.set_defaults(func=cmd_telegram_setup)

    p = sub.add_parser("mute", help="pause alerts, e.g. during a deploy")
    common(p)
    p.add_argument("duration", nargs="?", default="1h")
    p.set_defaults(func=cmd_mute)

    p = sub.add_parser("unmute", help="resume alerts")
    common(p)
    p.set_defaults(func=cmd_unmute)

    p = sub.add_parser("add", help="add something to the configuration (currently: a Telegram bot/chat)")
    add_sub = p.add_subparsers(dest="what", metavar="<what>")
    pt = add_sub.add_parser("telegram", help="connect another Telegram bot or chat (guided)")
    common(pt)
    pt.add_argument("--name", help="name of the channel, used in `notify = [...]` (default: telegram-2, ...)")
    pt.add_argument("--token", help="bot token (default: ask, or reuse the existing bot)")
    pt.add_argument("--chat-id", help="skip chat discovery and use this chat id")
    pt.add_argument("--thread-id", type=int, help="post into this forum topic")
    pt.add_argument("--min-severity", choices=SEVERITIES, default="info",
                    help="send only alerts of at least this severity (recoveries always pass)")
    pt.add_argument("--no-commands", action="store_true", help="do not answer /status, /mute in this chat")
    pt.add_argument("--api-base", default="https://api.telegram.org")
    pt.add_argument("--wait", type=int, default=120)
    pt.add_argument("-y", "--yes", action="store_true", help="no questions, accept defaults")
    pt.set_defaults(func=cmd_add_telegram)

    p = sub.add_parser(
        "watch", help="start watching a service, container, URL, port or process without editing the config")
    common(p)
    p.add_argument("targets", nargs="*", metavar="NAME", help="systemd service name(s) (container names with --docker)")
    p.add_argument("--docker", action="store_true", help="the names are Docker containers")
    p.add_argument("--http", metavar="URL", help="watch a URL")
    p.add_argument("--tcp", metavar="[HOST:]PORT", help="watch a TCP port (HOST defaults to this machine)")
    p.add_argument("--process", metavar="PATTERN", help="watch a process by its command line (pgrep -f)")
    p.add_argument("--name", help="name of the check (for --http/--tcp/--process)")
    p.add_argument("--label", help="how it is called in alerts")
    p.add_argument("--notify", action="append", metavar="CHANNEL", help="send its alerts only to this channel (repeatable)")
    p.add_argument("--inactive", action="store_true", help="also alert when the service/container is merely stopped")
    p.add_argument("--no-logs", action="store_true", help="watch the state only, not the log text")
    p.add_argument("--threshold", type=int, help="failed checks in a row before alerting")
    p.add_argument("--contains", help="(--http) text the page must contain")
    p.add_argument("--every", type=int, help="(checks) seconds between checks")
    p.add_argument("--min-count", type=int, help="(--process) minimum number of matching processes")
    p.add_argument("--force", action="store_true", help="add it even if it does not seem to exist")
    p.add_argument("-y", "--yes", action="store_true")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("install-service", help="print (or --write) the systemd unit")
    p.add_argument("-c", "--config")
    p.add_argument("--write", action="store_true", help="write /etc/systemd/system/svcwatch.service")
    p.add_argument("--start", action="store_true", help="also enable and start it")
    p.set_defaults(func=cmd_install_service)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"Configuration problem:\n{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130

