from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from .config import DockerCfg, explicit_names, name_matches
from .models import LogLine
from .runner import CommandError, Runner

_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?Z?\s?(.*)$", re.S)


@dataclass
class ContainerState:
    status: str = ""
    exit_code: int = 0
    restarts: int = 0
    health: str = "none"
    oom: bool = False

    @property
    def missing(self) -> bool:
        return self.status == ""

    def failed(self) -> bool:
        if self.health == "unhealthy" or self.status == "dead":
            return True
        return self.status == "exited" and (self.exit_code != 0 or self.oom)

    def stopped(self) -> bool:
        return self.status in ("exited", "created", "paused") and not self.failed()

    def describe(self) -> str:
        text = self.status
        if self.status == "exited":
            text += f" (code {self.exit_code}{', OOM-killed' if self.oom else ''})"
        if self.health not in ("none", ""):
            text += f", health: {self.health}"
        return text


def norm_ts(stamp: str) -> str:
    m = _TS.match(stamp)
    if not m:
        return stamp
    return f"{m.group(1)}.{(m.group(2) or '').ljust(9, '0')[:9]}"


def split_line(line: str) -> Tuple[Optional[str], str]:
    m = _TS.match(line)
    if not m:
        return None, line
    return norm_ts(f"{m.group(1)}.{m.group(2) or ''}"), m.group(3)


class Docker:
    def __init__(self, runner: Runner, cfg: DockerCfg):
        self.runner = runner
        self.cfg = cfg

    def discover(self) -> List[str]:
        found: List[str] = []
        if self.cfg.discover == "all":
            res = self.runner.run(["docker", "ps", "-a", "--format", "{{.Names}}"])
            if res.returncode != 0:
                raise CommandError(f"docker ps failed: {res.stderr.strip()}")
            found = [n.strip() for n in res.stdout.splitlines() if n.strip()]
        selected = [
            n for n in found
            if any(name_matches(p, n) for p in self.cfg.include)
            and not any(name_matches(p, n) for p in self.cfg.exclude)
        ]
        return sorted(set(selected) | set(self.cfg.containers) | set(explicit_names(self.cfg.watch)))

    def state(self, name: str) -> ContainerState:
        fmt = (
            "{{.State.Status}}|{{.State.ExitCode}}|{{.RestartCount}}|"
            "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|{{.State.OOMKilled}}"
        )
        res = self.runner.run(["docker", "inspect", "-f", fmt, name])
        if res.returncode != 0:
            if "no such" in (res.stderr + res.stdout).lower():
                return ContainerState(status="")
            raise CommandError(f"docker inspect {name} failed: {res.stderr.strip()}")
        parts = res.stdout.strip().split("|")
        if len(parts) < 5:
            raise CommandError(f"unexpected docker inspect output for {name}: {res.stdout.strip()!r}")
        try:
            return ContainerState(
                status=parts[0], exit_code=int(parts[1]), restarts=int(parts[2]),
                health=parts[3], oom=parts[4].lower() == "true",
            )
        except ValueError as exc:
            raise CommandError(f"unexpected docker inspect output for {name}: {exc}") from exc

    def read_logs(self, name: str, cursor: Optional[str], lookback: int) -> Tuple[Optional[str], List[LogLine]]:
        cmd = ["docker", "logs", "--timestamps"]
        if cursor:
            cmd += ["--since", cursor.split(".")[0] + "Z"]
        else:
            cmd += ["--tail", str(max(1, lookback))]
        cmd.append(name)
        res = self.runner.run(cmd, timeout=60)
        if res.returncode != 0:
            raise CommandError(f"docker logs {name} failed: {res.stderr.strip()}")
        entries: List[Tuple[str, str]] = []
        for raw in (res.stdout + "\n" + res.stderr).splitlines():
            if not raw.strip():
                continue
            stamp, text = split_line(raw)
            if stamp is None:
                stamp = ""
            entries.append((stamp, text))
        entries.sort(key=lambda e: e[0])
        new_cursor = cursor
        lines: List[LogLine] = []
        for stamp, text in entries:
            if cursor and stamp and stamp <= cursor:
                continue
            if stamp and (new_cursor is None or stamp > new_cursor):
                new_cursor = stamp
            if text.strip():
                lines.append(LogLine(text=text, ts=stamp or None))
        if not cursor and lookback <= 0:
            lines = []
            if new_cursor is None:
                new_cursor = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "000"
        return new_cursor, lines
