from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import SystemdCfg, explicit_names, name_matches
from .models import LogLine
from .runner import CommandError, Runner

SINCE = "since:"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class UnitState:
    load: str = ""
    active: str = ""
    sub: str = ""
    result: str = ""
    restarts: Optional[int] = None

    @property
    def missing(self) -> bool:
        return self.load == "not-found"

    def failed(self) -> bool:
        return self.active == "failed"

    def stopped(self) -> bool:
        return self.active in ("inactive", "deactivating") and not self.missing

    def describe(self) -> str:
        text = f"{self.active}/{self.sub}" if self.sub else self.active
        if self.result and self.result not in ("success", ""):
            text += f" (result: {self.result})"
        return text


def parse_show(text: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    return values


def _decode_message(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        try:
            return bytes(value).decode("utf-8", "replace")
        except (TypeError, ValueError):
            return ""
    return str(value)


def parse_journal_json(output: str) -> Tuple[Optional[str], List[LogLine]]:
    cursor: Optional[str] = None
    lines: List[LogLine] = []
    for raw in output.splitlines():
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if entry.get("__CURSOR"):
            cursor = entry["__CURSOR"]
        try:
            priority = int(entry.get("PRIORITY"))
        except (TypeError, ValueError):
            priority = None
        text = _decode_message(entry.get("MESSAGE"))
        if text.strip():
            lines.append(LogLine(text=text, priority=priority))
    return cursor, lines


class Systemd:
    def __init__(self, runner: Runner, cfg: SystemdCfg):
        self.runner = runner
        self.cfg = cfg


    def _list_custom(self) -> List[str]:
        names: List[str] = []
        for directory in self.cfg.unit_dirs:
            base = Path(directory)
            if not base.is_dir():
                continue
            for path in sorted(base.glob("*.service")):
                if path.stem.endswith("@"):
                    continue
                try:
                    if path.is_symlink() and str(path.resolve()) in ("/dev/null", "\\dev\\null"):
                        continue
                except OSError:
                    continue
                names.append(path.stem)
        return names

    def _list_all(self) -> List[str]:
        res = self.runner.run(["systemctl", "list-unit-files", "--type=service", "--no-legend", "--no-pager"])
        if res.returncode != 0:
            raise CommandError(f"systemctl list-unit-files failed: {res.stderr.strip()}")
        names: List[str] = []
        for line in res.stdout.splitlines():
            parts = line.split()
            if parts and parts[0].endswith(".service"):
                name = parts[0][: -len(".service")]
                if not name.endswith("@"):
                    names.append(name)
        return names

    def discover(self) -> List[str]:
        found: List[str] = []
        if self.cfg.discover == "custom":
            found = self._list_custom()
        elif self.cfg.discover == "all":
            found = self._list_all()
        selected = [
            n for n in found
            if any(name_matches(p, n) for p in self.cfg.include)
            and not any(name_matches(p, n) for p in self.cfg.exclude)
        ]
        explicit = list(self.cfg.units) + explicit_names(self.cfg.watch)
        return sorted(set(selected) | set(explicit))


    def state(self, unit: str) -> UnitState:
        res = self.runner.run([
            "systemctl", "show", f"{unit}.service",
            "-p", "LoadState", "-p", "ActiveState", "-p", "SubState", "-p", "Result", "-p", "NRestarts",
        ])
        if res.returncode != 0:
            raise CommandError(f"systemctl show {unit} failed: {res.stderr.strip() or res.stdout.strip()}")
        vals = parse_show(res.stdout)
        restarts: Optional[int]
        try:
            restarts = int(vals["NRestarts"])
        except (KeyError, ValueError):
            restarts = None
        return UnitState(
            load=vals.get("LoadState", ""),
            active=vals.get("ActiveState", ""),
            sub=vals.get("SubState", ""),
            result=vals.get("Result", ""),
            restarts=restarts,
        )


    def read_logs(self, unit: str, cursor: Optional[str], lookback: int) -> Tuple[Optional[str], List[LogLine]]:
        cmd = ["journalctl", "-u", f"{unit}.service", "--no-pager", "-o", "json"]
        if cursor and cursor.startswith(SINCE):
            cmd.append(f"--since={cursor[len(SINCE):]} UTC")
        elif cursor:
            cmd.append(f"--after-cursor={cursor}")
        else:
            cmd += ["-n", str(max(1, lookback))]
        started = utc_now()
        res = self.runner.run(cmd, timeout=60)
        if res.returncode != 0:
            if cursor and "cursor" in (res.stderr + res.stdout).lower():
                return self.read_logs(unit, None, 0)[0], []
            raise CommandError(f"journalctl for {unit} failed: {res.stderr.strip() or res.stdout.strip()}")
        new_cursor, lines = parse_journal_json(res.stdout)
        if not cursor and lookback <= 0:
            lines = []
            if not new_cursor:
                new_cursor = SINCE + started
        return new_cursor or cursor, lines
