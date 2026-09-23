from __future__ import annotations

import html
import json
import logging
import smtplib
import time
import urllib.error
import urllib.request
from email.message import EmailMessage
from typing import List, Optional

from .config import EmailCfg, NotifyCfg, TelegramCfg, WebhookCfg
from .models import OK, SEVERITY_RANK, Event
from .telegram import TelegramClient

log = logging.getLogger("svcwatch")


class NotifyError(RuntimeError):
    pass


class Notifier:
    name = "notifier"
    min_severity = "info"

    def wants(self, event: Event) -> bool:
        if event.severity == OK:
            return True
        return SEVERITY_RANK.get(event.severity, 0) >= SEVERITY_RANK.get(self.min_severity, 0)

    def send(self, event: Event, host: str, prefix: str) -> None:
        raise NotImplementedError


def plain_text(event: Event, host: str, prefix: str) -> str:
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event.ts))
    head = f"{prefix} {event.icon} {event.title}".strip()
    body = f"\n\n{event.body}" if event.body else ""
    return f"{head}\nHost: {host}\nTime: {when}{body}"


class ConsoleNotifier(Notifier):
    name = "console"

    def send(self, event: Event, host: str, prefix: str) -> None:
        log.warning("ALERT [%s] %s | %s", event.severity, event.title, event.body.replace("\n", " | ")[:300])


class TelegramNotifier(Notifier):
    def __init__(self, cfg: TelegramCfg):
        self.cfg = cfg
        self.name = cfg.name
        self.min_severity = cfg.min_severity
        self.client = TelegramClient(cfg.bot_token, cfg.api_base, cfg.timeout)

    def render(self, event: Event, host: str, prefix: str) -> str:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event.ts))
        esc = html.escape
        head = f"{event.icon} <b>{esc(event.title)}</b>"
        meta = f"<code>{esc(host)}</code> · {esc(when)}"
        text = f"{head}\n{meta}"
        if event.body:
            room = 3800 - len(text)
            body = event.body if len(event.body) <= room else event.body[: max(0, room - 20)] + "\n... (truncated)"
            text += f"\n\n<pre>{esc(body)}</pre>"
        return text

    def send(self, event: Event, host: str, prefix: str) -> None:
        self.client.send_message(
            self.cfg.chat_id,
            self.render(event, host, prefix),
            thread_id=self.cfg.thread_id,
            silent=self.cfg.silent or event.severity in ("info", "ok"),
        )


class EmailNotifier(Notifier):
    def __init__(self, cfg: EmailCfg):
        self.cfg = cfg
        self.name = cfg.name
        self.min_severity = cfg.min_severity

    def send(self, event: Event, host: str, prefix: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = f"{prefix} {event.title}".strip()
        msg["From"] = self.cfg.sender
        msg["To"] = ", ".join(self.cfg.to)
        msg.set_content(plain_text(event, host, prefix))
        smtp_cls = smtplib.SMTP_SSL if self.cfg.use_ssl else smtplib.SMTP
        with smtp_cls(self.cfg.host, self.cfg.port, timeout=self.cfg.timeout) as smtp:
            if self.cfg.starttls and not self.cfg.use_ssl:
                smtp.starttls()
            if self.cfg.user:
                smtp.login(self.cfg.user, self.cfg.password)
            smtp.send_message(msg)


class WebhookNotifier(Notifier):
    def __init__(self, cfg: WebhookCfg):
        self.cfg = cfg
        self.name = cfg.name
        self.min_severity = cfg.min_severity

    def send(self, event: Event, host: str, prefix: str) -> None:
        text = plain_text(event, host, prefix)
        payload = {
            "host": host,
            "kind": event.kind,
            "target": event.target,
            "severity": event.severity,
            "title": event.title,
            "body": event.body,
            "time": event.ts,
            "text": text,
            "content": text[:1900],
        }
        headers = {"Content-Type": "application/json", "User-Agent": "svcwatch/1.0"}
        headers.update(self.cfg.headers)
        req = urllib.request.Request(
            self.cfg.url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout) as resp:
                if resp.status >= 400:
                    raise NotifyError(f"webhook answered HTTP {resp.status}")
        except urllib.error.HTTPError as exc:
            raise NotifyError(f"webhook answered HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise NotifyError(f"webhook unreachable: {getattr(exc, 'reason', exc)}") from exc


def build_notifiers(cfg: NotifyCfg) -> List[Notifier]:
    notifiers: List[Notifier] = []
    if cfg.console:
        notifiers.append(ConsoleNotifier())
    notifiers += [TelegramNotifier(c) for c in cfg.telegram]
    notifiers += [EmailNotifier(c) for c in cfg.email]
    notifiers += [WebhookNotifier(c) for c in cfg.webhook]
    return notifiers


def find_notifier(notifiers: List[Notifier], name: str) -> Optional[Notifier]:
    return next((n for n in notifiers if n.name == name), None)
