from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import List


class CommandError(RuntimeError):
    pass


class CommandNotFound(CommandError):
    pass


@dataclass
class CmdResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class Runner:
    def run(self, cmd: List[str], timeout: float = 30.0) -> CmdResult:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise CommandNotFound(f"'{cmd[0]}' was not found on this machine") from exc
        except subprocess.TimeoutExpired as exc:
            raise CommandError(f"'{' '.join(cmd[:3])}...' timed out after {timeout:.0f}s") from exc
        except OSError as exc:
            raise CommandError(f"cannot run '{cmd[0]}': {exc}") from exc
        return CmdResult(proc.returncode, proc.stdout, proc.stderr)
