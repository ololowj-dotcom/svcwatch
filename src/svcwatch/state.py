from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

SEEN_TTL = 7 * 24 * 3600
STATE_VERSION = 1


def _empty() -> Dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "cursors": {},
        "seen": {},
        "summary": {},
        "summary_ts": None,
        "health": {},
        "restarts": {},
        "outbox": [],
        "rate": {"hour": 0, "count": 0, "notified": False},
        "tg_offset": {},
        "notified_errors": {},
    }


class State:
    def __init__(self, path: Optional[Path], data: Optional[Dict[str, Any]] = None):
        self.path = path
        self.data: Dict[str, Any] = data if data is not None else _empty()
        for key, value in _empty().items():
            self.data.setdefault(key, value)


    @classmethod
    def load(cls, path: Optional[Path]) -> State:
        if path is None or not Path(path).exists():
            return cls(path)
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("state root is not an object")
            return cls(path, raw)
        except (OSError, ValueError) as exc:
            broken = Path(str(path) + ".corrupt")
            try:
                os.replace(path, broken)
            except OSError:
                pass
            state = cls(path)
            state.load_error = f"state file was unreadable ({exc}); moved to {broken.name} and started fresh"
            return state

    load_error: Optional[str] = None

    def save(self, now: Optional[float] = None) -> None:
        if self.path is None:
            return
        now = time.time() if now is None else now
        self.data["seen"] = {k: v for k, v in self.data["seen"].items() if now - v <= SEEN_TTL}
        path = Path(self.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)


    @property
    def cursors(self) -> Dict[str, str]:
        return self.data["cursors"]

    @property
    def seen(self) -> Dict[str, float]:
        return self.data["seen"]

    @property
    def summary(self) -> Dict[str, int]:
        return self.data["summary"]

    @property
    def health(self) -> Dict[str, Dict[str, Any]]:
        return self.data["health"]

    @property
    def restarts(self) -> Dict[str, int]:
        return self.data["restarts"]

    @property
    def outbox(self) -> list:
        return self.data["outbox"]

    def forget_missing(self, live_keys: set) -> None:
        for section in ("cursors", "summary", "health", "restarts"):
            for key in [k for k in self.data[section] if k not in live_keys]:
                del self.data[section][key]
