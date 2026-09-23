from __future__ import annotations

import getpass
import os
import re
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .config import ConfigError, SystemdCfg, load_config
from .monitor import Monitor
from .runner import CommandError, Runner
from .systemd import Systemd
from .telegram import TelegramClient, TelegramError, chats_from_updates, mask_token
from .template import render_config, render_unit

TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{30,}$")
UNIT_PATH = Path("/etc/systemd/system/svcwatch.service")


class IO:
    def say(self, text: str = "") -> None:
        print(text, flush=True)

    def ask(self, prompt: str, default: Optional[str] = None, secret: bool = False) -> str:
        suffix = f" [{default}]" if default else ""
        try:
            value = getpass.getpass(f"{prompt}: ") if secret else input(f"{prompt}{suffix}: ")
        except EOFError:
            return default or ""
        return value.strip() or (default or "")

    def confirm(self, prompt: str, default: bool = True) -> bool:
        hint = "Y/n" if default else "y/N"
        answer = self.ask(f"{prompt} [{hint}]").lower()
        if not answer:
            return default
        return answer in ("y", "yes", "д", "да")


@dataclass
class SetupArgs:
    directory: Optional[Path] = None
    token: Optional[str] = None
    chat_id: Optional[str] = None
    yes: bool = False
    force: bool = False
    no_service: bool = False
    wait: int = 120
    api_base: str = "https://api.telegram.org"


def default_dir() -> Path:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return Path("/etc/svcwatch")
    return Path.home() / ".config" / "svcwatch"


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _validated_client(io: IO, token: str, api_base: str) -> Optional[dict]:
    client = TelegramClient(token, api_base)
    try:
        return client.get_me()
    except TelegramError as exc:
        io.say(f"  x {exc}")
        return None


def _get_bot(io: IO, args: SetupArgs, api_base: str):
    token = args.token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    for _ in range(3):
        if not token:
            io.say("\n1/4  Telegram bot")
            io.say("     Open @BotFather in Telegram, send /newbot, and paste the token it gives you.")
            token = io.ask("     Bot token", secret=True)
        if not TOKEN_RE.match(token or ""):
            io.say("  x That does not look like a bot token (expected 123456789:AA...).")
            token = ""
            if args.yes:
                return None, None
            continue
        me = _validated_client(io, token, api_base)
        if me:
            io.say(f"  ok Connected to @{me.get('username')} ({mask_token(token)})")
            return token, me
        token = ""
        if args.yes:
            return None, None
    return None, None


def _find_chat(io: IO, client: TelegramClient, me: dict, args: SetupArgs) -> Optional[str]:
    if args.chat_id:
        return str(args.chat_id)
    try:
        hook = client.webhook_url()
    except TelegramError:
        hook = ""
    if hook:
        io.say("  x This bot has a webhook set, so its messages cannot be read here.")
        io.say("    Use a fresh bot from @BotFather, or pass --chat-id yourself.")
        return None
    username = me.get("username", "your_bot")
    io.say("\n2/4  Where should alerts go?")
    io.say(f"     Open https://t.me/{username} and press START")
    io.say("     (for a group: add the bot to the group and write any message there).")
    io.say(f"     Waiting up to {args.wait}s ...")
    deadline = time.time() + args.wait
    offset: Optional[int] = None
    found: List[dict] = []
    while time.time() < deadline and not found:
        began = time.time()
        try:
            updates = client.get_updates(offset, timeout=5)
        except TelegramError as exc:
            io.say(f"  x {exc}")
            return None
        if updates:
            offset = updates[-1]["update_id"] + 1
        found = chats_from_updates(updates)
        if not found and time.time() - began < 1:
            time.sleep(1)
    if not found:
        io.say("  x Nothing received. Run `svcwatch setup` again after pressing START, or pass --chat-id.")
        return None
    chosen = found[0]
    if len(found) > 1:
        io.say("     Several chats wrote to the bot:")
        for i, chat in enumerate(found, 1):
            io.say(f"       {i}. {chat['title']} ({chat['type']}, id {chat['id']})")
        pick = io.ask("     Use which one?", default="1")
        chosen = found[int(pick) - 1] if pick.isdigit() and 1 <= int(pick) <= len(found) else found[0]
    io.say(f"  ok Alerts will go to: {chosen['title']} ({chosen['type']})")
    return str(chosen["id"])


def _write_secret_file(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _upsert_env(path: Path, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = [ln for ln in lines if not ln.startswith(f"{key}=")]
    lines.append(f"{key}={value}")
    _write_secret_file(path, "\n".join(lines) + "\n")


def run_setup(args: SetupArgs, io: Optional[IO] = None, runner: Optional[Runner] = None) -> int:
    io = io or IO()
    runner = runner or Runner()
    api_base = args.api_base
    directory = Path(args.directory) if args.directory else default_dir()
    cfg_path, env_path = directory / "svcwatch.toml", directory / ".env"

    io.say("svcwatch setup - about two minutes, nothing is changed until the end.")
    if cfg_path.exists() and not args.force:
        if args.yes or not io.confirm(f"\n{cfg_path} already exists. Replace it? (a backup is kept)", default=False):
            io.say("Nothing changed. Use --force to overwrite.")
            return 1

    token, me = _get_bot(io, args, api_base)
    if not token:
        return 1
    client = TelegramClient(token, api_base)
    chat_id = _find_chat(io, client, me, args)
    if not chat_id:
        return 1
    try:
        client.send_message(
            chat_id,
            "✅ <b>svcwatch connected</b>\nYou will get alerts here. Send /help to see the commands.",
        )
        io.say("  ok Test message sent - check Telegram.")
    except TelegramError as exc:
        io.say(f"  x Could not send a message: {exc}")
        return 1

    io.say("\n3/4  What to watch")
    units: List[str] = []
    try:
        units = Systemd(runner, SystemdCfg()).discover()
    except CommandError as exc:
        io.say(f"     (systemd not available here: {exc})")
    if units:
        io.say(f"     Found {len(units)} services you deployed: {', '.join(units[:15])}" + (" ..." if len(units) > 15 else ""))
        io.say("     They will be watched (state, crashes, restart loops, errors in the log).")
    else:
        io.say("     No custom services found; you can add them to svcwatch.toml later.")
    docker = False
    if shutil.which("docker"):
        try:
            res = runner.run(["docker", "ps", "-a", "--format", "{{.Names}}"], timeout=10)
            names = [n for n in res.stdout.split() if n]
            if res.returncode == 0 and names:
                docker = args.yes or io.confirm(f"     Also watch {len(names)} Docker containers?", default=True)
        except CommandError:
            pass

    io.say("\n4/4  Saving")
    directory.mkdir(parents=True, exist_ok=True)
    if cfg_path.exists():
        shutil.copy2(cfg_path, cfg_path.with_name(cfg_path.name + ".bak"))
    state_file = "/var/lib/svcwatch/state.json" if is_root() else str(directory / "state.json")
    cfg_path.write_text(
        render_config(state_file=state_file, chat_id=chat_id, docker=docker, commands=True,
                      api_base=api_base), encoding="utf-8")
    _upsert_env(env_path, "TELEGRAM_BOT_TOKEN", token)
    io.say(f"  ok {cfg_path}")
    io.say(f"  ok {env_path} (the token; readable by the owner only)")

    try:
        cfg = load_config(cfg_path)
        mon = Monitor(cfg, runner=runner, notifiers=[], dry_run=True)
        mon.tick()
        bad = [s for s in mon.status.values() if not s["ok"]]
        io.say(f"  ok Config is valid; {len(mon.status)} targets checked, {len(bad)} currently down.")
    except ConfigError as exc:
        io.say(f"  x The generated config has a problem:\n{exc}")
        return 1

    _offer_service(io, args, cfg_path, runner)
    io.say("\nDone. Handy commands:")
    io.say(f"  svcwatch check   --config {cfg_path}     what is watched and its state now")
    io.say(f"  svcwatch test-notify --config {cfg_path}  send a test alert")
    io.say("  in Telegram: /status  /mute 1h  /unmute")
    return 0


def _offer_service(io: IO, args: SetupArgs, cfg_path: Path, runner: Runner) -> None:
    if args.no_service:
        return
    have_systemd = shutil.which("systemctl") is not None
    if not have_systemd:
        io.say("\n  systemd not found: start it manually with  svcwatch run --config " + str(cfg_path))
        return
    if not is_root():
        io.say("\n  To run it in the background, re-run as root (sudo svcwatch setup --force)")
        io.say("  or start it manually:  svcwatch run --config " + str(cfg_path))
        return
    if not (args.yes or io.confirm("\nInstall and start svcwatch as a background service now?", default=True)):
        io.say("  Skipped. Start later with:  svcwatch install-service --write --start")
        return
    install_service(str(cfg_path), runner=runner, start=True, io=io)


def install_service(config_path: str, *, runner: Optional[Runner] = None, start: bool = True,
                    io: Optional[IO] = None, unit_path: Path = UNIT_PATH) -> int:
    io = io or IO()
    runner = runner or Runner()
    unit_path.write_text(render_unit(str(Path(config_path).resolve())), encoding="utf-8")
    io.say(f"  ok wrote {unit_path}")
    steps = [["systemctl", "daemon-reload"]]
    if start:
        steps.append(["systemctl", "enable", "--now", "svcwatch"])
    for cmd in steps:
        res = runner.run(cmd, timeout=60)
        if res.returncode != 0:
            io.say(f"  x {' '.join(cmd)} failed: {res.stderr.strip()}")
            return 1
    if start:
        time.sleep(1.5)
        res = runner.run(["systemctl", "is-active", "svcwatch"])
        io.say(f"  ok service is {res.stdout.strip() or 'unknown'}  (logs: journalctl -u svcwatch -f)")
    return 0


__all__ = ["IO", "SetupArgs", "run_setup", "install_service", "default_dir", "is_root", "TOKEN_RE"]
