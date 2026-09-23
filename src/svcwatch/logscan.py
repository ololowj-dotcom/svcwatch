from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence

from .models import LogLine

REGEX_PREFIX = "re:"


def compile_pattern(pattern: str) -> Callable[[str], bool]:
    if pattern.startswith(REGEX_PREFIX):
        rx = re.compile(pattern[len(REGEX_PREFIX):])
        return lambda text: rx.search(text) is not None
    needle = pattern.lower()
    return lambda text: needle in text.lower()


class Matcher:
    def __init__(self, patterns: Sequence[str]):
        self._tests = [compile_pattern(p) for p in patterns if p]

    def __bool__(self) -> bool:
        return bool(self._tests)

    def matches(self, text: str) -> bool:
        return any(test(text) for test in self._tests)


_TS_PREFIX = re.compile(r"^\s*\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\s*")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_HEX = re.compile(r"\b0x[0-9a-fA-F]+\b")
_NUM = re.compile(r"\d+")
_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    text = _TS_PREFIX.sub("", text)
    text = _UUID.sub("<uuid>", text)
    text = _HEX.sub("<hex>", text)
    text = _NUM.sub("#", text)
    return _WS.sub(" ", text).strip()


def fingerprint(target: str, lines: Sequence[str]) -> str:
    material = target + "\n" + "\n".join(normalize(line) for line in lines)
    return hashlib.sha1(material.encode("utf-8", "replace")).hexdigest()


@dataclass
class ScanResult:
    groups: List[List[str]] = field(default_factory=list)
    external: int = 0


def scan(
    lines: Sequence[LogLine],
    immediate: Matcher,
    external: Matcher,
    ignore: Matcher,
    context: int = 3,
    priority_max: Optional[int] = None,
) -> ScanResult:
    result = ScanResult()
    texts = [line.text for line in lines]

    def is_ignored(i: int) -> bool:
        return bool(ignore) and ignore.matches(texts[i])

    def is_external(i: int) -> bool:
        return bool(external) and external.matches(texts[i])

    def is_hit(i: int) -> bool:
        if is_ignored(i) or is_external(i):
            return False
        line = lines[i]
        if immediate.matches(line.text):
            return True
        return priority_max is not None and line.priority is not None and line.priority <= priority_max

    for i in range(len(texts)):
        if not is_ignored(i) and is_external(i):
            result.external += 1

    i, n = 0, len(texts)
    while i < n:
        if not is_hit(i):
            i += 1
            continue
        end = min(n, i + 1 + context)
        j = i + 1
        while j < end:
            if is_hit(j):
                end = min(n, j + 1 + context)
            j += 1
        result.groups.append([t for t in texts[i:end] if t.strip()])
        i = end
    return result
