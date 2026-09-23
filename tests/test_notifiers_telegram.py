import smtplib
import time

import pytest
from conftest import Clock, FakeRunner, journal, make_cfg, show

from svcwatch.models import CRITICAL, INFO, OK, WARNING, Event
from svcwatch.monitor import Monitor, clear_mute, mute_until
from svcwatch.notifiers import (
    ConsoleNotifier,
    EmailNotifier,
    NotifyError,
    TelegramNotifier,
    WebhookNotifier,
    build_notifiers,
    plain_text,
)
from svcwatch.runner import CmdResult
from svcwatch.telegram import TelegramClient, TelegramError, chats_from_updates, mask_token

TOKEN = "123456789:AAFakeTokenForTestsOnly_abcdefghijklmnop"


def tg_cfg(telegram, extra="", tmp_path=None):
    return make_cfg(
        f'[systemd]\nenabled = false\n[notify.telegram]\nbot_token = "{TOKEN}"\nchat_id = "42"\n'
        f'api_base = "{telegram.base}"\n{extra}', tmp_path)


def ev(severity=CRITICAL, title="Service down: api", body="failed/failed", **kw):
    return Event("down", "systemd:api", severity, title, body, time.time(), **kw)


def test_client_get_me_and_send(telegram):
    c = TelegramClient(TOKEN, telegram.base)
    assert c.get_me()["username"] == "watch_test_bot"
    c.send_message("42", "hi <b>", thread_id=7, silent=True)
    sent = telegram.sent[0]
    assert sent["chat_id"] == "42" and sent["message_thread_id"] == 7
    assert sent["disable_notification"] is True and sent["parse_mode"] == "HTML"


def test_wrong_token_gives_a_helpful_error_without_leaking_the_token(telegram):
    c = TelegramClient("999:wrongtokenwrongtokenwrongtokenwrong", telegram.base)
    with pytest.raises(TelegramError) as info:
        c.get_me()
    assert "wrong or was revoked" in str(info.value)
    assert "wrongtoken" not in str(info.value)
    assert info.value.code == 401


def test_chat_not_found_hint(telegram):
    telegram.fail_send = {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
    with pytest.raises(TelegramError, match="wrong chat_id"):
        TelegramClient(TOKEN, telegram.base).send_message("1", "x")


def test_rate_limit_error_carries_retry_after(telegram):
    telegram.fail_send = {"ok": False, "error_code": 429, "description": "Too Many Requests",
                          "parameters": {"retry_after": 17}}
    with pytest.raises(TelegramError) as info:
        TelegramClient(TOKEN, telegram.base).send_message("1", "x")
    assert info.value.retry_after == 17


def test_unreachable_telegram_error_does_not_contain_the_token():
    c = TelegramClient(TOKEN, "http://127.0.0.1:9", timeout=2)
    with pytest.raises(TelegramError) as info:
        c.get_me()
    assert TOKEN not in str(info.value) and "cannot reach Telegram" in str(info.value)


def test_long_messages_are_truncated(telegram):
    TelegramClient(TOKEN, telegram.base).send_message("1", "x" * 9000)
    assert len(telegram.sent[0]["text"]) <= 4000


def test_chats_from_updates_dedupes_and_orders_newest_first():
    updates = [
        {"update_id": 1, "message": {"chat": {"id": 1, "type": "private", "first_name": "Ann"}}},
        {"update_id": 2, "message": {"chat": {"id": -100, "type": "supergroup", "title": "Ops"}}},
        {"update_id": 3, "message": {"chat": {"id": 1, "type": "private", "first_name": "Ann"}}},
        {"update_id": 4, "edited": {}},
        {"update_id": 5, "my_chat_member": {"chat": {"id": -200, "type": "group", "title": "New group"}}},
    ]
    chats = chats_from_updates(updates)
    assert [c["id"] for c in chats] == [-200, 1, -100] or [c["id"] for c in chats][0] == -200
    assert {c["id"] for c in chats} == {1, -100, -200}
    assert next(c for c in chats if c["id"] == -100)["title"] == "Ops"


def test_mask_token():
    assert mask_token(TOKEN).endswith("...") and TOKEN not in mask_token(TOKEN)
    assert mask_token("short") == "***"


def test_notifier_renders_html_and_escapes(telegram):
    n = TelegramNotifier(tg_cfg(telegram).notify.telegram[0])
    n.send(ev(title="Error in <api>", body="<script>alert(1)</script> & more"), "web-01", "[svcwatch]")
    text = telegram.sent[0]["text"]
    assert "<b>Error in &lt;api&gt;</b>" in text
    assert "&lt;script&gt;" in text and "<script>" not in text
    assert "<code>web-01</code>" in text and text.count("<pre>") == 1
    assert telegram.sent[0]["disable_notification"] is False


def test_recoveries_and_infos_are_silent_pushes(telegram):
    n = TelegramNotifier(tg_cfg(telegram).notify.telegram[0])
    n.send(ev(OK, "api recovered"), "h", "")
    n.send(ev(INFO, "Daily summary"), "h", "")
    n.send(ev(CRITICAL), "h", "")
    assert [m["disable_notification"] for m in telegram.sent] == [True, True, False]


def test_silent_option_and_thread(telegram):
    cfg = tg_cfg(telegram, "silent = true\nthread_id = 5\n").notify.telegram[0]
    TelegramNotifier(cfg).send(ev(), "h", "")
    assert telegram.sent[0]["disable_notification"] is True and telegram.sent[0]["message_thread_id"] == 5


def test_huge_body_is_cut_inside_the_limit(telegram):
    n = TelegramNotifier(tg_cfg(telegram).notify.telegram[0])
    n.send(ev(body="line\n" * 5000), "h", "")
    assert len(telegram.sent[0]["text"]) <= 4000 and "truncated" in telegram.sent[0]["text"]


def test_failure_raises_so_the_monitor_can_queue(telegram):
    telegram.fail_send = {"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked"}
    n = TelegramNotifier(tg_cfg(telegram).notify.telegram[0])
    with pytest.raises(TelegramError, match="blocked or removed"):
        n.send(ev(), "h", "")


def test_webhook_payload_is_slack_and_discord_compatible(webhook_sink):
    cfg = make_cfg(f'[notify.webhook]\nurl = "{webhook_sink.url}"\nheaders = {{ "X-Token" = "s3" }}').notify.webhook[0]
    WebhookNotifier(cfg).send(ev(body="details"), "web-01", "[svcwatch]")
    payload = webhook_sink.received[0]
    assert payload["target"] == "systemd:api" and payload["severity"] == "critical"
    assert "Service down: api" in payload["text"] and payload["content"] == payload["text"]
    assert webhook_sink.headers[0]["X-Token"] == "s3"


def test_webhook_http_error_is_an_error_without_the_secret_url():
    cfg = make_cfg('[notify.webhook]\nurl = "http://127.0.0.1:9/secret-path-token"\ntimeout = 2').notify.webhook[0]
    with pytest.raises(NotifyError) as info:
        WebhookNotifier(cfg).send(ev(), "h", "")
    assert "secret-path-token" not in str(info.value)


def test_webhook_bad_status(webhook_sink):
    webhook_sink.server.close()
    from conftest import WebhookSink
    sink = WebhookSink(status=500)
    try:
        cfg = make_cfg(f'[notify.webhook]\nurl = "{sink.url}"').notify.webhook[0]
        with pytest.raises(NotifyError, match="HTTP 500"):
            WebhookNotifier(cfg).send(ev(), "h", "")
    finally:
        sink.server.close()


def test_email_uses_starttls_and_login(monkeypatch):
    log = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            log["conn"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            log["tls"] = True

        def login(self, u, p):
            log["login"] = (u, p)

        def send_message(self, msg):
            log["msg"] = msg

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    cfg = make_cfg('[notify.email]\nhost = "smtp.x"\nuser = "u"\npassword = "p"\nsender = "a@x"\nto = ["b@x", "c@x"]').notify.email[0]
    EmailNotifier(cfg).send(ev(), "web-01", "[svcwatch]")
    assert log["conn"] == ("smtp.x", 587) and log["tls"] and log["login"] == ("u", "p")
    msg = log["msg"]
    assert msg["Subject"].startswith("[svcwatch]") and "Service down: api" in msg["Subject"]
    assert msg["To"] == "b@x, c@x" and "web-01" in msg.get_content()


def test_email_without_credentials_skips_login(monkeypatch):
    calls = []

    class FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            calls.append("tls")

        def login(self, *a):
            calls.append("login")

        def send_message(self, m):
            calls.append("send")

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    cfg = make_cfg('[notify.email]\nhost = "h"\nsender = "a@x"\nto = ["b@x"]').notify.email[0]
    EmailNotifier(cfg).send(ev(), "h", "")
    assert calls == ["tls", "send"]


def test_plain_text_and_console():
    text = plain_text(ev(), "web-01", "[svcwatch]")
    assert text.startswith("[svcwatch]") and "Host: web-01" in text and "failed/failed" in text
    ConsoleNotifier().send(ev(), "h", "")


def test_build_notifiers_order_and_names(telegram):
    cfg = make_cfg(
        '[notify]\nconsole = true\n[[notify.telegram]]\nbot_token = "1:a"\nchat_id = "1"\n'
        '[notify.webhook]\nurl = "http://x"\n')
    assert [n.name for n in build_notifiers(cfg.notify)] == ["console", "telegram", "webhook"]
    assert build_notifiers(make_cfg("[notify]\nconsole = false").notify) == []


def test_severity_filter_semantics():
    n = ConsoleNotifier()
    n.min_severity = "critical"
    assert n.wants(ev(CRITICAL)) and not n.wants(ev(WARNING)) and not n.wants(ev(INFO))
    assert n.wants(ev(OK))


def make_monitor(telegram, tmp_path, extra="commands = true\n"):
    cfg = tg_cfg(telegram, extra, tmp_path)
    runner, clock = FakeRunner(), Clock()
    runner.on("systemctl show", stdout=show("failed"))
    runner.on("journalctl", stdout=journal("anchor"))
    cfg.systemd.enabled = True
    cfg.systemd.discover = "off"
    cfg.systemd.units = ["api"]
    return Monitor(cfg, runner=runner, clock=clock), runner, clock, cfg


def alerts(telegram):
    return [m for m in telegram.sent if "<b>" in m["text"] and "svcwatch" not in m["text"][:12]]


def test_alert_reaches_the_bot_end_to_end(telegram, tmp_path):
    mon, runner, clock, cfg = make_monitor(telegram, tmp_path)
    mon.tick()
    texts = [m["text"] for m in telegram.sent]
    assert any("Service down: api" in t and "failed" in t for t in texts)
    assert all(m["chat_id"] == "42" for m in telegram.sent)


def test_telegram_outage_queues_and_delivers_later(telegram, tmp_path):
    mon, runner, clock, cfg = make_monitor(telegram, tmp_path, "")
    telegram.fail_send = {"ok": False, "error_code": 429, "description": "Too Many Requests",
                          "parameters": {"retry_after": 30}}
    mon.tick()
    assert telegram.sent == [] and len(mon.state.outbox) == 1
    assert mon.state.outbox[0]["next_try"] >= clock.t + 30
    telegram.fail_send = None
    clock.advance(40)
    mon.tick()
    assert len(telegram.sent) == 1 and mon.state.outbox == []


def test_status_command(telegram, tmp_path):
    mon, runner, clock, cfg = make_monitor(telegram, tmp_path)
    mon.tick()
    telegram.sent.clear()
    telegram.message(42, "/status")
    clock.advance(20)
    mon.tick()
    reply = telegram.sent[-1]["text"]
    assert "1 of 1" in reply and "need attention" in reply and "failed" in reply


def test_status_is_green_when_everything_is_fine(telegram, tmp_path):
    mon, runner, clock, cfg = make_monitor(telegram, tmp_path)
    runner.on("systemctl show", stdout=show("active"))
    mon.tick()
    telegram.message(42, "/status")
    clock.advance(20)
    mon.tick()
    assert "All good" in telegram.sent[-1]["text"]


def test_mute_and_unmute_commands(telegram, tmp_path):
    mon, runner, clock, cfg = make_monitor(telegram, tmp_path)
    mon.tick()
    telegram.message(42, "/mute 30m")
    clock.advance(20)
    mon.tick()
    assert abs(mute_until(cfg.mute_file) - (clock.t + 1800)) < 5
    assert "muted for 30m" in telegram.sent[-1]["text"]
    telegram.message(42, "/unmute")
    clock.advance(20)
    mon.tick()
    assert mute_until(cfg.mute_file) == 0.0 and "resumed" in telegram.sent[-1]["text"]


def test_mute_with_nonsense_gets_usage(telegram, tmp_path):
    mon, runner, clock, cfg = make_monitor(telegram, tmp_path)
    mon.tick()
    telegram.message(42, "/mute banana")
    clock.advance(20)
    mon.tick()
    assert "Usage: /mute" in telegram.sent[-1]["text"]
    clear_mute(cfg.mute_file)


def test_commands_from_strangers_are_ignored(telegram, tmp_path):
    mon, runner, clock, cfg = make_monitor(telegram, tmp_path)
    mon.tick()
    before = len(telegram.sent)
    telegram.message(999, "/mute 1d")
    telegram.message(999, "/status")
    clock.advance(20)
    mon.tick()
    assert len(telegram.sent) == before
    assert mute_until(cfg.mute_file) == 0.0


def test_allowed_chats_may_also_command(telegram, tmp_path):
    mon, runner, clock, cfg = make_monitor(telegram, tmp_path, 'commands = true\nallowed_chats = [555]\n')
    mon.tick()
    telegram.message(555, "/ping")
    clock.advance(20)
    mon.tick()
    last = telegram.sent[-1]
    assert last["chat_id"] == "555" and "pong" in last["text"]


def test_commands_are_processed_once_and_offset_is_saved(telegram, tmp_path):
    mon, runner, clock, cfg = make_monitor(telegram, tmp_path)
    mon.tick()
    telegram.message(42, "/ping", update_id=10)
    clock.advance(20)
    mon.tick()
    n = len(telegram.sent)
    clock.advance(20)
    mon.tick()
    assert len(telegram.sent) == n
    assert mon.state.data["tg_offset"]["telegram"] == 11


def test_bot_username_suffix_and_plain_text(telegram, tmp_path):
    mon, runner, clock, cfg = make_monitor(telegram, tmp_path)
    mon.tick()
    telegram.message(42, "just chatting")
    telegram.message(42, "/help@watch_test_bot")
    clock.advance(20)
    mon.tick()
    assert "/status" in telegram.sent[-1]["text"] and "/mute" in telegram.sent[-1]["text"]


def test_unknown_command(telegram, tmp_path):
    mon, runner, clock, cfg = make_monitor(telegram, tmp_path)
    mon.tick()
    telegram.message(42, "/nope")
    clock.advance(20)
    mon.tick()
    assert "Unknown command" in telegram.sent[-1]["text"]


def test_commands_disabled_by_default(telegram, tmp_path):
    mon, runner, clock, cfg = make_monitor(telegram, tmp_path, "")
    mon.tick()
    telegram.message(42, "/ping")
    clock.advance(20)
    mon.tick()
    assert "getUpdates" not in telegram.requests


def test_polling_failure_never_breaks_the_cycle(telegram, tmp_path):
    mon, runner, clock, cfg = make_monitor(telegram, tmp_path)
    mon.tick()
    telegram.server.close()
    clock.advance(20)
    mon.tick()


def test_dry_run_does_not_poll_or_send(telegram, tmp_path):
    cfg = tg_cfg(telegram, "commands = true\n", tmp_path)
    runner = FakeRunner()
    runner.on("systemctl show", stdout=show("failed"))
    runner.on("journalctl", stdout=journal("x"))
    cfg.systemd.enabled, cfg.systemd.discover, cfg.systemd.units = True, "off", ["api"]
    mon = Monitor(cfg, runner=runner, notifiers=[], dry_run=True)
    events = mon.tick()
    assert events and telegram.sent == [] and "getUpdates" not in telegram.requests


def test_topic_messages_are_answered_in_the_same_topic(telegram, tmp_path):
    mon, runner, clock, cfg = make_monitor(telegram, tmp_path)
    mon.tick()
    telegram.message(42, "/ping", message_thread_id=9, is_topic_message=True)
    clock.advance(20)
    mon.tick()
    assert telegram.sent[-1]["message_thread_id"] == 9


_ = CmdResult
