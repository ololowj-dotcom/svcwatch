from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

INFO = "info"
WARNING = "warning"
CRITICAL = "critical"
OK = "ok"

SEVERITY_RANK = {INFO: 0, WARNING: 1, CRITICAL: 2}

ICONS = {CRITICAL: "\U0001f534", WARNING: "\U0001f7e1", OK: "\U0001f7e2", INFO: "ℹ️"}


@dataclass
class Event:
    kind: str
    target: str
    severity: str
    title: str
    body: str = ""
    ts: float = field(default_factory=time.time)
    route: Optional[List[str]] = None

    @property
    def icon(self) -> str:
        return ICONS.get(self.severity, "")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Event:
        return cls(
            kind=str(data.get("kind", "info")),
            target=str(data.get("target", "")),
            severity=str(data.get("severity", INFO)),
            title=str(data.get("title", "")),
            body=str(data.get("body", "")),
            ts=float(data.get("ts", time.time())),
            route=list(data["route"]) if data.get("route") else None,
        )


@dataclass
class LogLine:
    text: str
    priority: Optional[int] = None
    ts: Optional[str] = None
