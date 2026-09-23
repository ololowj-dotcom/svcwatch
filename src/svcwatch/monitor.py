from __future__ import annotations

import html
import logging
import re
import signal
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import Config, Effective, resolve
from .dockerx import Docker
from .logscan import Matcher, fingerprint, normalize, scan
from .models import CRITICAL, INFO, OK, WARNING, Event, LogLine
from .notifiers import Notifier, TelegramNotifier, build_notifiers
from .probes import check_http, check_process, check_tcp
from .runner import CommandError, Runner
from .state import State
from .systemd import Systemd
from .telegram import TelegramError

log = logging.getLogger("svcwatch")

OUTBOX_MAX = 200
OUTBOX_MAX_AGE = 24 * 3600
MAX_LINE_LEN = 400

_DURATION = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$", re.I)


def fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s" if sec else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def parse_duration(text: str, default_unit: str = "m") -> Optional[int]:
    m = _DURATION.match(text or "")
    if not m:
        return None
    value, unit = int(m.group(1)), (m.group(2) or default_unit).lower()
    seconds = value * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return seconds if seconds > 0 else None


def mute_until(path: Path) -> float:
    try:
        return float(Path(path).read_text().strip())
    except (OSError, ValueError):
        return 0.0


def set_mute(path: Path, until: float) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(str(until))


def clear_mute(path: Path) -> None:
    try:
        Path(path).unlink()
    except OSError:
        pass


class Monitor:
    def __init__(
        self,
        cfg: Config,
        *,
        runner: Optional[Runner] = None,
        notifiers: Optional[List[Notifier]] = None,
        clock: Callable[[], float] = time.time,
        dry_run: bool = False,
    ):
        self.cfg = cfg
        self.runner = runner or Runner()
        self.clock = clock
        self.dry_run = dry_run
        self.host = cfg.monitor.hostname or socket.gethostname()
        self.notifiers: List[Notifier] = build_notifiers(cfg.notify) if notifiers is None else notifiers
        self.systemd = Systemd(self.runner, cfg.systemd) if cfg.systemd.enabled else None
        self.docker = Docker(self.runner, cfg.docker) if cfg.docker.enabled else None
        self.state = State.load(None if dry_run else cfg.monitor.state_file)
        if self.state.load_error:
            log.warning(self.state.load_error)
        self.status: Dict[str, Dict[str, Any]] = {}
        self._last_run: Dict[str, float] = {}
        self._matchers: Dict[Tuple[str, ...], Matcher] = {}
        self._stop = threading.Event()


    def tick(self) -> List[Event]:
        now = self.clock()
        events: List[Event] = []
        live: set = set()
        if self.state.data["summary_ts"] is None:
            self.state.data["summary_ts"] = now

        self._watch_systemd(now, events, live)
        self._watch_docker(now, events, live)
        self._watch_checks(now, events, live)

        self.state.forget_missing(live)
        for key in [k for k in self.status if k not in live]:
            del self.status[key]
        self._maybe_summary(now, events)
        self._poll_commands(now)
        self._dispatch(events, now)
        if not self.dry_run:
            self.state.save(now)
        return events

    def run(self) -> None:
        self._install_signal_handlers()
        log.info(
            "svcwatch started on %s: interval %ss, %d notifier(s): %s",
            self.host, self.cfg.monitor.interval, len(self.notifiers),
            ", ".join(n.name for n in self.notifiers) or "none",
        )
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("monitoring cycle failed")
            self._stop.wait(self.cfg.monitor.interval)
        log.info("svcwatch stopped")

    def stop(self) -> None:
        self._stop.set()


    def _base(self, section: Any) -> Effective:
        lg = self.cfg.logs
        return Effective(
            label="", logs=True, alert_on=list(section.alert_on), restart_alert=section.restart_alert,
            immediate=list(lg.immediate), external=list(lg.external), ignore=list(lg.ignore),
            notify=None, fail_threshold=1, remind_after=self.cfg.monitor.remind_after,
            journal_priority=lg.journal_priority,
        )

    def _watch_systemd(self, now: float, events: List[Event], live: set) -> None:
        if not self.systemd:
            return
        try:
            units = self.systemd.discover()
        except CommandError as exc:
            self._internal("systemd", str(exc), now, events)
            return
        base = self._base(self.cfg.systemd)
        for unit in units:
            key = f"systemd:{unit}"
            live.add(key)
            eff = resolve(self.cfg.systemd.watch, unit, base)
            try:
                st = self.systemd.state(unit)
                if st.missing:
                    bad, detail = True, "unit not found (is the name right? see `systemctl list-units`)"
                else:
                    bad = st.failed() or ("inactive" in eff.alert_on and st.stopped())
                    detail = st.describe()
                self._health(key, eff, "Service", not bad, detail, now, events)
                if not st.missing:
                    self._restarts(key, eff, st.restarts, now, events)
                    if eff.logs:
                        self._logs(key, eff, now, events, lambda cur, u=unit: self.systemd.read_logs(
                            u, cur, self.cfg.monitor.initial_lookback))
            except CommandError as exc:
                self._internal(key, str(exc), now, events)

    def _watch_docker(self, now: float, events: List[Event], live: set) -> None:
        if not self.docker:
            return
        try:
            names = self.docker.discover()
        except CommandError as exc:
            self._internal("docker", str(exc), now, events)
            return
        base = self._base(self.cfg.docker)
        for name in names:
            key = f"docker:{name}"
            live.add(key)
            eff = resolve(self.cfg.docker.watch, name, base)
            try:
                st = self.docker.state(name)
                if st.missing:
                    bad, detail = True, "container not found"
                else:
                    bad = st.failed() or ("inactive" in eff.alert_on and st.stopped())
                    detail = st.describe()
                self._health(key, eff, "Container", not bad, detail, now, events)
                if not st.missing:
                    self._restarts(key, eff, st.restarts, now, events)
                    if eff.logs:
                        self._logs(key, eff, now, events, lambda cur, n=name: self.docker.read_logs(
                            n, cur, self.cfg.monitor.initial_lookback))
            except CommandError as exc:
                self._internal(key, str(exc), now, events)

    def _watch_checks(self, now: float, events: List[Event], live: set) -> None:
        def due(key: str, every: int) -> bool:
            if every and now - self._last_run.get(key, 0) < every:
                return False
            self._last_run[key] = now
            return True

        def eff_for(chk: Any) -> Effective:
            return Effective(
                label=chk.label or chk.name, logs=False, alert_on=[], restart_alert=False, immediate=[],
                external=[], ignore=[], notify=chk.notify, fail_threshold=chk.fail_threshold,
                remind_after=self.cfg.monitor.remind_after if chk.remind_after is None else chk.remind_after,
                journal_priority=0,
            )

        for http in self.cfg.http:
            key = f"http:{http.name}"
            live.add(key)
            if due(key, http.every):
                ok, detail = check_http(http)
                self._health(key, eff_for(http), "Endpoint", ok, detail, now, events)
        for tcp in self.cfg.tcp:
            key = f"tcp:{tcp.name}"
            live.add(key)
            if due(key, tcp.every):
                ok, detail = check_tcp(tcp)
                self._health(key, eff_for(tcp), "Port", ok, detail, now, events)
        for proc in self.cfg.process:
            key = f"process:{proc.name}"
            live.add(key)
            if due(key, proc.every):
                ok, detail = check_process(proc, self.runner)
                self._health(key, eff_for(proc), "Process", ok, detail, now, events)


    def _health(self, key: str, eff: Effective, what: str, ok: bool, detail: str, now: float,
                events: List[Event]) -> None:
        self.status[key] = {"label": eff.label, "ok": ok, "detail": detail}
        h = self.state.health.setdefault(
            key, {"fails": 0, "down": False, "since": None, "last_alert": None, "detail": ""})
        h["detail"] = detail
        if ok:
            if h["down"]:
                down_for = fmt_duration(now - (h["since"] or now))
                events.append(Event("recovered", key, OK, f"{eff.label} recovered",
                                    f"{detail}\nWas down for {down_for}", now, eff.notify))
            h.update(fails=0, down=False, since=None, last_alert=None)
            return
        if h["fails"] == 0:
            h["since"] = now
        h["fails"] += 1
        if not h["down"]:
            if h["fails"] >= eff.fail_threshold:
                h.update(down=True, last_alert=now)
                events.append(Event("down", key, CRITICAL, f"{what} down: {eff.label}", detail, now, eff.notify))
        elif eff.remind_after and now - (h["last_alert"] or now) >= eff.remind_after:
            h["last_alert"] = now
            events.append(Event("down", key, CRITICAL, f"Still down: {eff.label}",
                                f"{detail}\nDown for {fmt_duration(now - (h['since'] or now))}", now, eff.notify))

    def _restarts(self, key: str, eff: Effective, count: Optional[int], now: float, events: List[Event]) -> None:
        if count is None:
            return
        prev = self.state.restarts.get(key)
        self.state.restarts[key] = count
        if eff.restart_alert and prev is not None and count > prev:
            n = count - prev
            events.append(Event(
                "restart", key, WARNING, f"{eff.label} restarted" + (f" {n} times" if n > 1 else ""),
                f"Automatic restart counter: {prev} -> {count}.\nThe process is up again, but it crashed or exited.",
                now, eff.notify))

    def _matcher(self, patterns: List[str]) -> Matcher:
        key = tuple(patterns)
        if key not in self._matchers:
            self._matchers[key] = Matcher(patterns)
        return self._matchers[key]

    def _logs(self, key: str, eff: Effective, now: float, events: List[Event],
              read: Callable[[Optional[str]], Tuple[Optional[str], List[LogLine]]]) -> None:
        cursor = self.state.cursors.get(key)
        new_cursor, lines = read(cursor)
        if new_cursor:
            self.state.cursors[key] = new_cursor
        limit = self.cfg.monitor.max_backlog_lines
        if len(lines) > limit:
            log.warning("%s: %d new log lines, only the last %d are scanned", key, len(lines), limit)
            lines = lines[-limit:]
        if not lines:
            return
        result = scan(
            lines, self._matcher(eff.immediate), self._matcher(eff.external), self._matcher(eff.ignore),
            self.cfg.logs.context_lines, eff.journal_priority or None,
        )
        if result.external:
            self.state.summary[key] = self.state.summary.get(key, 0) + result.external
        fresh: List[List[str]] = []
        for group in result.groups:
            fp = fingerprint(key, group)
            last = self.state.seen.get(fp)
            if last is not None and now - last < self.cfg.monitor.dedup_window:
                continue
            self.state.seen[fp] = now
            fresh.append(group)
        if not fresh:
            return
        budget = self.cfg.logs.max_lines_per_alert
        chunks: List[str] = []
        used = 0
        for group in reversed(fresh):
            take = group[: max(1, budget - used)]
            if used >= budget:
                break
            chunks.append("\n".join(t[:MAX_LINE_LEN] for t in take))
            used += len(take)
        chunks.reverse()
        omitted = sum(len(g) for g in fresh) - used
        body = "\n——\n".join(chunks)
        if omitted > 0:
            body += f"\n(+{omitted} more lines)"
        title = f"Error in {eff.label}" if len(fresh) == 1 else f"{len(fresh)} errors in {eff.label}"
        events.append(Event("log_error", key, CRITICAL, title, body, now, eff.notify))

    def _internal(self, key: str, message: str, now: float, events: List[Event]) -> None:
        log.warning("%s: %s", key, message)
        fp = "internal|" + key + "|" + normalize(message)
        window = max(self.cfg.monitor.dedup_window, 3600)
        last = self.state.seen.get(fp)
        if last is not None and now - last < window:
            return
        self.state.seen[fp] = now
        events.append(Event("internal", key, WARNING, f"svcwatch cannot check {key}", message, now))

    def _maybe_summary(self, now: float, events: List[Event]) -> None:
        interval = self.cfg.monitor.summary_interval
        if not interval or now - self.state.data["summary_ts"] < interval:
            return
        counts = dict(self.state.summary)
        total = sum(counts.values())
        if total or self.cfg.monitor.summary_when_empty:
            kinds: Dict[str, int] = {}
            for key in self.status:
                kinds[key.split(":", 1)[0]] = kinds.get(key.split(":", 1)[0], 0) + 1
            watching = ", ".join(f"{k}: {v}" for k, v in sorted(kinds.items())) or "nothing"
            lines = [f"Watching {len(self.status)} targets ({watching})"]
            down = [s["label"] for s in self.status.values() if not s["ok"]]
            lines.append("Currently down: " + (", ".join(down) if down else "none"))
            if total:
                lines.append(f"\nExternal/network errors in the last {fmt_duration(interval)}: {total}")
                for key, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:15]:
                    lines.append(f"  {key}: {n}")
            else:
                lines.append("No external/network errors were seen.")
            events.append(Event("summary", "summary", INFO, "Daily summary", "\n".join(lines), now))
        self.state.summary.clear()
        self.state.data["summary_ts"] = now


    def _is_muted(self, now: float) -> bool:
        return mute_until(self.cfg.mute_file) > now

    def _select(self, event: Event) -> List[Notifier]:
        chosen = [n for n in self.notifiers if (event.route is None or n.name in event.route) and n.wants(event)]
        return chosen

    def _attempt(self, event: Event, notifiers: List[Notifier]) -> Tuple[List[str], float]:
        failed: List[str] = []
        delay = 0.0
        for n in notifiers:
            try:
                n.send(event, self.host, self.cfg.monitor.title_prefix)
            except Exception as exc:
                log.warning("notifier '%s' failed: %s", n.name, exc)
                failed.append(n.name)
                if isinstance(exc, TelegramError) and exc.retry_after:
                    delay = max(delay, float(exc.retry_after))
        return failed, delay

    def _rate_ok(self, event: Event, now: float) -> bool:
        limit = self.cfg.monitor.rate_limit_per_hour
        if not limit or event.severity == OK or event.kind == "summary":
            return True
        rate = self.state.data["rate"]
        hour = int(now // 3600)
        if rate["hour"] != hour:
            rate.update(hour=hour, count=0, notified=False)
        if rate["count"] < limit:
            rate["count"] += 1
            return True
        if not rate["notified"]:
            rate["notified"] = True
            notice = Event("info", "svcwatch", WARNING, "Alert rate limit reached",
                           f"More than {limit} alerts this hour - further alerts are suppressed until the next hour. "
                           "Something is very wrong (or the patterns are too broad).", now)
            self._deliver(notice, now)
        log.warning("rate limit: dropped alert '%s'", event.title)
        return False

    def _deliver(self, event: Event, now: float) -> None:
        chosen = self._select(event)
        failed, delay = self._attempt(event, chosen)
        if failed and not self.dry_run:
            self.state.outbox.append({
                "event": event.to_dict(), "pending": failed, "attempts": 1, "queued": now,
                "next_try": now + max(delay, 30.0),
            })
            del self.state.outbox[:-OUTBOX_MAX]

    def _retry_outbox(self, now: float) -> None:
        keep: List[Dict[str, Any]] = []
        by_name = {n.name: n for n in self.notifiers}
        for entry in self.state.outbox:
            if now - entry.get("queued", now) > OUTBOX_MAX_AGE:
                log.warning("dropping undeliverable alert after 24h: %s", entry["event"].get("title"))
                continue
            if now < entry.get("next_try", 0):
                keep.append(entry)
                continue
            event = Event.from_dict(entry["event"])
            targets = [by_name[n] for n in entry["pending"] if n in by_name]
            failed, delay = self._attempt(event, targets)
            if failed:
                entry["pending"] = failed
                entry["attempts"] = entry.get("attempts", 1) + 1
                entry["next_try"] = now + max(delay, min(900.0, 30.0 * 2 ** min(entry["attempts"], 5)))
                keep.append(entry)
            else:
                log.info("delivered queued alert: %s", event.title)
        self.state.data["outbox"] = keep

    def _dispatch(self, events: List[Event], now: float) -> None:
        if not self.dry_run:
            self._retry_outbox(now)
        muted = self._is_muted(now)
        for event in events:
            log.info("event [%s] %s", event.severity, event.title)
            if muted:
                log.info("muted: not sending '%s'", event.title)
                continue
            if not self._rate_ok(event, now):
                continue
            self._deliver(event, now)


    def _poll_commands(self, now: float) -> None:
        if self.dry_run:
            return
        for n in self.notifiers:
            if isinstance(n, TelegramNotifier) and n.cfg.commands:
                self._poll_telegram(n, now)

    def _poll_telegram(self, n: TelegramNotifier, now: float) -> None:
        offsets = self.state.data["tg_offset"]
        try:
            updates = n.client.get_updates(offsets.get(n.name), timeout=0)
        except TelegramError as exc:
            key = f"tgpoll:{n.name}"
            if now - self.state.data["notified_errors"].get(key, 0) > 3600:
                self.state.data["notified_errors"][key] = now
                log.warning("telegram commands for '%s' unavailable: %s", n.name, exc)
            return
        allowed = {str(n.cfg.chat_id), *map(str, n.cfg.allowed_chats)}
        for upd in updates:
            offsets[n.name] = upd["update_id"] + 1
            msg = upd.get("message") or {}
            text = (msg.get("text") or "").strip()
            chat_id = str((msg.get("chat") or {}).get("id", ""))
            if not text.startswith("/"):
                continue
            if chat_id not in allowed:
                log.warning("ignored command %r from unauthorized chat %s", text.split()[0], chat_id)
                continue
            reply = self.handle_command(text, now)
            try:
                thread = msg.get("message_thread_id") if msg.get("is_topic_message") else n.cfg.thread_id
                n.client.send_message(chat_id, reply, thread_id=thread, silent=True)
            except TelegramError as exc:
                log.warning("cannot answer telegram command: %s", exc)

    def handle_command(self, text: str, now: float) -> str:
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        arg = " ".join(parts[1:])
        esc = html.escape
        if cmd in ("/start", "/help"):
            return ("<b>svcwatch</b>\n/status - what is running and what is not\n"
                    "/mute [30m|2h|1d] - pause alerts (default 1h), e.g. during a deploy\n"
                    "/unmute - resume alerts\n/ping - check that the watchdog is alive")
        if cmd == "/ping":
            return f"pong from <code>{esc(self.host)}</code>"
        if cmd == "/mute":
            seconds = parse_duration(arg, "m") if arg else 3600
            if not seconds:
                return "Usage: /mute 30m  (units: s, m, h, d)"
            set_mute(self.cfg.mute_file, now + seconds)
            until = time.strftime("%H:%M", time.localtime(now + seconds))
            return f"\U0001f507 Alerts muted for {fmt_duration(seconds)} (until {until}). /unmute to resume."
        if cmd == "/unmute":
            clear_mute(self.cfg.mute_file)
            return "\U0001f514 Alerts resumed."
        if cmd == "/status":
            return self.status_report(now)
        return "Unknown command. Try /help"

    def status_report(self, now: float) -> str:
        esc = html.escape
        total = len(self.status)
        down = [(k, s) for k, s in sorted(self.status.items()) if not s["ok"]]
        head = (f"\U0001f534 <b>{len(down)} of {total}</b> need attention" if down
                else f"\U0001f7e2 <b>All good</b> - {total} targets")
        lines = [f"{head} on <code>{esc(self.host)}</code>"]
        for _, s in down:
            lines.append(f"\U0001f534 {esc(s['label'])}: {esc(s['detail'])}")
        ok_names = [esc(s["label"]) for _, s in sorted(self.status.items()) if s["ok"]]
        if ok_names:
            shown = ", ".join(ok_names[:40]) + (f" (+{len(ok_names) - 40})" if len(ok_names) > 40 else "")
            lines.append(f"\U0001f7e2 {shown}")
        remaining = mute_until(self.cfg.mute_file) - now
        if remaining > 0:
            lines.append(f"\U0001f507 Muted for another {fmt_duration(remaining)}")
        return "\n".join(lines)


    def _install_signal_handlers(self) -> None:
        def handler(signum, _frame):
            log.info("signal %s received, stopping", signum)
            self.stop()

        for name in ("SIGTERM", "SIGINT"):
            sig = getattr(signal, name, None)
            if sig is not None:
                try:
                    signal.signal(sig, handler)
                except ValueError:
                    pass
