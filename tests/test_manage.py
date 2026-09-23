import pytest
from conftest import FakeRunner, FakeTelegram, journal, show, tomllib
from test_wizard import ScriptIO

from svcwatch import manage
from svcwatch.cli import main
from svcwatch.config import ConfigError, load_config
from svcwatch.manage import AddTelegramArgs, WatchArgs, add_telegram, add_watch, apply_edit, restart_service
from svcwatch.monitor import Monitor
from svcwatch.runner import CommandNotFound
from svcwatch.template import render_config

TOKEN = FakeTelegram.TOKEN


@pytest.fixture
def cfgdir(tmp_path, telegram):
    d = tmp_path / "cfg"
    d.mkdir()
    text = render_config(state_file=str(d / "state.json"), chat_id="42", api_base=telegram.base)
    text = text.replace('discover = "custom"', 'discover = "off"')
    (d / "svcwatch.toml").write_text(text, encoding="utf-8")
    (d / ".env").write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\nSMTP_PASSWORD=keep\n", encoding="utf-8")
    return d


@pytest.fixture
def cfg_path(cfgdir):
    return cfgdir / "svcwatch.toml"


@pytest.fixture(autouse=True)
def quiet_env(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setattr(manage, "is_root", lambda: False)


def base_runner():
    r = FakeRunner()
    r.on("systemctl is-active --quiet svcwatch", returncode=3)
    return r


def tg_args(telegram, **kw):
    base = dict(yes=True, api_base=telegram.base, wait=2)
    base.update(kw)
    return AddTelegramArgs(**base)


def test_add_a_second_chat_for_the_same_bot(cfg_path, cfgdir, telegram):
    original = cfg_path.read_text(encoding="utf-8")
    io = ScriptIO()
    assert add_telegram(cfg_path, tg_args(telegram, name="team", chat_id="-100500"), io, base_runner()) == 0
    cfg = load_config(cfg_path, env={})
    assert [n.name for n in cfg.notify.telegram] == ["telegram", "team"]
    team = cfg.notify.telegram[1]
    assert team.chat_id == "-100500" and team.bot_token == TOKEN and team.api_base == telegram.base
    text = cfg_path.read_text(encoding="utf-8")
    assert text.startswith(original.rstrip("\n"))
    assert "# Where alerts go" in text
    assert (cfgdir / "svcwatch.toml.bak").read_text(encoding="utf-8") == original
    env = (cfgdir / ".env").read_text()
    assert "TELEGRAM_BOT_TOKEN_TEAM" not in env and "SMTP_PASSWORD=keep" in env
    assert telegram.sent[-1]["chat_id"] == "-100500" and "team" in telegram.sent[-1]["text"]


def test_default_name_is_the_next_free_one(cfg_path, telegram):
    assert add_telegram(cfg_path, tg_args(telegram, chat_id="1"), ScriptIO(), base_runner()) == 0
    assert add_telegram(cfg_path, tg_args(telegram, chat_id="2"), ScriptIO(), base_runner()) == 0
    names = [n.name for n in load_config(cfg_path, env={}).notify.telegram]
    assert names == ["telegram", "telegram-2", "telegram-3"]


def test_chat_is_discovered_when_not_given(cfg_path, telegram):
    telegram.message(9001, "/start")
    assert add_telegram(cfg_path, tg_args(telegram, name="ops"), ScriptIO(), base_runner()) == 0
    assert load_config(cfg_path, env={}).notify.telegram[1].chat_id == "9001"


def test_topic_severity_and_commands_options(cfg_path, telegram):
    args = tg_args(telegram, name="quiet", chat_id="5", thread_id=12, min_severity="critical", commands=False)
    assert add_telegram(cfg_path, args, ScriptIO(), base_runner()) == 0
    n = load_config(cfg_path, env={}).notify.telegram[1]
    assert (n.thread_id, n.min_severity, n.commands) == (12, "critical", False)
    assert telegram.sent[-1]["message_thread_id"] == 12


def test_name_collision_and_bad_names_change_nothing(cfg_path, telegram):
    before = cfg_path.read_text(encoding="utf-8")
    io = ScriptIO()
    assert add_telegram(cfg_path, tg_args(telegram, name="telegram", chat_id="1"), io, base_runner()) == 1
    assert "already exists" in io.text
    assert add_telegram(cfg_path, tg_args(telegram, name="bad name!", chat_id="1"), ScriptIO(), base_runner()) == 1
    assert cfg_path.read_text(encoding="utf-8") == before and not cfg_path.with_name("svcwatch.toml.bak").exists()


def test_single_table_config_is_promoted_so_more_bots_can_follow(cfg_path, telegram):
    cfg_path.write_text(cfg_path.read_text(encoding="utf-8").replace("[[notify.telegram]]", "[notify.telegram]"),
                        encoding="utf-8")
    assert add_telegram(cfg_path, tg_args(telegram, name="team", chat_id="7"), ScriptIO(), base_runner()) == 0
    assert "[[notify.telegram]]" in cfg_path.read_text(encoding="utf-8")
    assert len(load_config(cfg_path, env={}).notify.telegram) == 2


def test_a_different_bot_gets_its_own_secret(cfg_path, cfgdir, telegram):
    other = FakeTelegram()
    other.TOKEN = "987654321:AAOtherBotTokenOtherBotTokenOtherBot12"
    try:
        args = AddTelegramArgs(yes=True, name="team-bot", token=other.TOKEN, chat_id="3",
                               api_base=other.base, wait=2)
        assert add_telegram(cfg_path, args, ScriptIO(), base_runner()) == 0
        assert f"TELEGRAM_BOT_TOKEN_TEAM_BOT={other.TOKEN}" in (cfgdir / ".env").read_text()
        text = cfg_path.read_text(encoding="utf-8")
        assert other.TOKEN not in text and "${TELEGRAM_BOT_TOKEN_TEAM_BOT}" in text
        team = load_config(cfg_path, env={}).notify.telegram[1]
        assert team.bot_token == other.TOKEN and team.api_base == other.base
        assert TOKEN in (cfgdir / ".env").read_text()
    finally:
        other.server.close()


def test_wrong_token_and_send_failure_change_nothing(cfg_path, telegram):
    before = cfg_path.read_text(encoding="utf-8")
    bad = AddTelegramArgs(yes=True, name="x", token="111111111:AAWrongTokenWrongTokenWrongTokenWrong", chat_id="1",
                          api_base=telegram.base)
    assert add_telegram(cfg_path, bad, ScriptIO(), base_runner()) == 1
    telegram.fail_send = {"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked"}
    io = ScriptIO()
    assert add_telegram(cfg_path, tg_args(telegram, name="y", chat_id="1"), io, base_runner()) == 1
    assert "Could not send" in io.text and cfg_path.read_text(encoding="utf-8") == before


def test_interactive_flow_asks_name_and_offers_the_existing_bot(cfg_path, telegram):
    io = ScriptIO("ops", "y")
    args = AddTelegramArgs(chat_id="8", api_base=telegram.base, wait=2)
    assert add_telegram(cfg_path, args, io, base_runner()) == 0
    assert "Name for this channel" in io.text and "same bot" in io.text
    assert load_config(cfg_path, env={}).notify.telegram[1].name == "ops"


def test_service_restart_after_a_change(cfg_path, telegram, monkeypatch):
    monkeypatch.setattr(manage, "is_root", lambda: True)
    runner = FakeRunner()
    runner.on("systemctl is-active --quiet svcwatch", returncode=0)
    runner.on("systemctl restart svcwatch")
    io = ScriptIO()
    assert add_telegram(cfg_path, tg_args(telegram, name="t", chat_id="1"), io, runner) == 0
    assert runner.called("systemctl restart svcwatch") and "svcwatch restarted" in io.text


def test_service_restart_hint_when_not_root_or_not_running(telegram):
    r1 = FakeRunner()
    r1.on("systemctl is-active", returncode=0)
    io = ScriptIO()
    restart_service(io, r1)
    assert "sudo systemctl restart svcwatch" in io.text and not r1.called("systemctl restart")
    r2 = FakeRunner()
    r2.on("systemctl is-active", returncode=3)
    io2 = ScriptIO()
    restart_service(io2, r2)
    assert "not running" in io2.text
    r3 = FakeRunner()
    r3.missing("systemctl")
    restart_service(ScriptIO(), r3)


def test_invalid_result_is_rejected_and_the_file_is_untouched(cfg_path, cfgdir):
    before = cfg_path.read_text(encoding="utf-8")
    dup = '[[notify.telegram]]\nname = "telegram"\nbot_token = "1:a"\nchat_id = "1"\n'
    with pytest.raises(ConfigError, match="nothing was changed"):
        apply_edit(cfg_path, [dup])
    with pytest.raises(ConfigError, match="invalid TOML"):
        apply_edit(cfg_path, ["[[http]\nbroken"])
    assert cfg_path.read_text(encoding="utf-8") == before
    assert not (cfgdir / "svcwatch.toml.bak").exists() and not list(cfgdir.glob("*.tmp"))


@pytest.mark.parametrize("before,expected_true", [
    ("[docker]\nenabled = false\n", True),
    ("[docker]   # containers\n# a comment\nenabled = false   # off\n[logs]\n", True),
    ("[docker]\nenabled = true\n", True),
    ("[docker]\n# nothing set\n[logs]\n", True),
    ("[systemd]\nenabled = true\n", True),
])
def test_enable_docker_variants(before, expected_true):
    out = manage._enable_docker(before)
    assert tomllib.loads(out)["docker"]["enabled"] is True
    assert out.count("[docker]") == 1
    if "# off" in before:
        assert "# off" in out


def test_enable_docker_does_not_touch_other_sections():
    text = "[systemd]\nenabled = false\n[docker]\nenabled = false\n[logs]\nimmediate = []\n"
    out = tomllib.loads(manage._enable_docker(text))
    assert out["systemd"]["enabled"] is False and out["docker"]["enabled"] is True


def watch(cfg_path, runner=None, **kw):
    io = ScriptIO()
    rc = add_watch(cfg_path, WatchArgs(yes=True, **kw), io, runner or base_runner())
    return rc, io


def runner_with_unit(name="nginx", **st):
    r = base_runner()
    r.on(f"systemctl show {name}.service", stdout=show(**st))
    return r


def test_watch_a_service_adds_a_rule_and_it_is_then_monitored(cfg_path, cfgdir):
    rc, io = watch(cfg_path, runner_with_unit("nginx"), names=["nginx"], label="Web", inactive=True)
    assert rc == 0 and "systemd: nginx" in io.text
    cfg = load_config(cfg_path, env={})
    rule = cfg.systemd.watch[0]
    assert (rule.match, rule.label, rule.alert_on) == ("nginx", "Web", ["failed", "inactive"])
    assert (cfgdir / "svcwatch.toml.bak").exists()
    runner = runner_with_unit("nginx", active="failed", sub="failed")
    runner.on("journalctl", stdout=journal("x"))
    mon = Monitor(cfg, runner=runner, notifiers=[], dry_run=True)
    assert [e.kind for e in mon.tick()] == ["down"]


def test_unknown_service_is_refused_unless_forced(cfg_path):
    before = cfg_path.read_text(encoding="utf-8")
    runner = base_runner()
    runner.on("systemctl show typo.service", stdout=show(load="not-found", active="inactive", sub="dead"))
    rc, io = watch(cfg_path, runner, names=["typo"])
    assert rc == 1 and "does not exist" in io.text and "--force" in io.text
    assert cfg_path.read_text(encoding="utf-8") == before
    rc, io = watch(cfg_path, runner, names=["typo"], force=True)
    assert rc == 0 and "adding anyway" in io.text


def test_machine_without_systemd_cannot_verify_but_still_adds(cfg_path):
    runner = FakeRunner()
    runner.missing("systemctl")
    rc, io = watch(cfg_path, runner, names=["api"])
    assert rc == 0 and load_config(cfg_path, env={}).systemd.watch[0].match == "api"


def test_service_suffix_is_stripped_and_duplicates_skipped(cfg_path):
    runner = runner_with_unit("api")
    assert watch(cfg_path, runner, names=["api.service"])[0] == 0
    rc, io = watch(cfg_path, runner, names=["api"])
    assert rc == 0 and "already has a rule" in io.text and "Nothing to add" in io.text
    assert len(load_config(cfg_path, env={}).systemd.watch) == 1


def test_several_services_at_once_label_only_applies_to_a_single_one(cfg_path):
    runner = runner_with_unit("a")
    runner.on("systemctl show b.service", stdout=show())
    rc, _ = watch(cfg_path, runner, names=["a", "b"], label="Ignored", no_logs=True, threshold=2)
    rules = load_config(cfg_path, env={}).systemd.watch
    assert [r.match for r in rules] == ["a", "b"] and all(r.label is None and r.logs is False for r in rules)
    assert all(r.fail_threshold == 2 for r in rules)


def test_notify_routing_is_validated(cfg_path, telegram):
    rc, io = watch(cfg_path, runner_with_unit("a"), names=["a"], notify=["nope"])
    assert rc == 1 and "Unknown notifier 'nope'" in io.text and "telegram" in io.text
    rc, _ = watch(cfg_path, runner_with_unit("a"), names=["a"], notify=["telegram"])
    assert rc == 0 and load_config(cfg_path, env={}).systemd.watch[0].notify == ["telegram"]


def test_docker_container_watch_enables_docker(cfg_path):
    assert load_config(cfg_path, env={}).docker.enabled is False
    runner = base_runner()
    runner.on("docker inspect -f", stdout="running|0|0|none|false\n")
    rc, io = watch(cfg_path, runner, names=["web"], docker=True, label="Site")
    assert rc == 0 and "docker: web" in io.text
    cfg = load_config(cfg_path, env={})
    assert cfg.docker.enabled is True and cfg.docker.watch[0].match == "web" and cfg.docker.watch[0].label == "Site"


def test_missing_container_is_refused(cfg_path):
    runner = base_runner()
    runner.on("docker inspect -f", stderr="Error: No such object: web", returncode=1)
    rc, io = watch(cfg_path, runner, names=["web"], docker=True)
    assert rc == 1 and "container 'web' does not exist" in io.text


def test_docker_section_is_created_when_missing(cfg_path):
    text = cfg_path.read_text(encoding="utf-8").replace("[docker]\nenabled = false\n", "")
    cfg_path.write_text(text, encoding="utf-8")
    runner = base_runner()
    runner.on("docker inspect -f", stdout="running|0|0|none|false\n")
    assert watch(cfg_path, runner, names=["db"], docker=True)[0] == 0
    assert load_config(cfg_path, env={}).docker.enabled is True


def test_watch_an_url_probes_it_immediately(cfg_path, pages):
    rc, io = watch(cfg_path, http=pages.base + "/ok", contains="systems", every=60, threshold=2, label="Site")
    assert rc == 0 and "right now: OK" in io.text
    chk = load_config(cfg_path, env={}).http[0]
    assert (chk.contains, chk.every, chk.fail_threshold, chk.label) == ("systems", 60, 2, "Site")
    assert chk.name.startswith("127-0-0-1")


def test_a_failing_url_is_still_added_with_a_clear_warning(cfg_path, pages):
    rc, io = watch(cfg_path, http=pages.base + "/error", name="maint")
    assert rc == 0 and "DOWN" in io.text and "503" in io.text and "added anyway" in io.text
    assert load_config(cfg_path, env={}).http[0].name == "maint"


def test_watch_a_tcp_port(cfg_path):
    import socket
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    try:
        rc, io = watch(cfg_path, tcp=f"127.0.0.1:{srv.getsockname()[1]}", name="db")
    finally:
        srv.close()
    assert rc == 0 and "right now: OK" in io.text
    chk = load_config(cfg_path, env={}).tcp[0]
    assert (chk.host, chk.name) == ("127.0.0.1", "db")


@pytest.mark.parametrize("bad", ["host:notaport", "h:0", "h:70000", ""])
def test_bad_tcp_target_is_rejected(cfg_path, bad):
    before = cfg_path.read_text(encoding="utf-8")
    rc, io = watch(cfg_path, tcp=bad)
    assert rc == 1 and "HOST:PORT" in io.text and cfg_path.read_text(encoding="utf-8") == before


def test_a_bare_port_means_this_machine(cfg_path):
    rc, io = watch(cfg_path, tcp="5432", name="pg")
    assert rc == 0
    assert load_config(cfg_path, env={}).tcp[0].host == "127.0.0.1"


def test_watch_a_process(cfg_path):
    runner = base_runner()
    runner.on("pgrep -f", stdout="10\n11\n")
    rc, io = watch(cfg_path, runner, process="python -m app.worker", name="worker", min_count=2)
    assert rc == 0 and "2 process" in io.text
    chk = load_config(cfg_path, env={}).process[0]
    assert (chk.pattern, chk.min_count) == ("python -m app.worker", 2)


def test_process_pattern_with_quotes_survives_toml_escaping(cfg_path):
    runner = base_runner()
    runner.on("pgrep -f", stdout="1\n")
    pattern = 'node "C:\\srv\\app.js" --flag'
    assert watch(cfg_path, runner, process=pattern, name="node")[0] == 0
    assert load_config(cfg_path, env={}).process[0].pattern == pattern


def test_duplicate_check_names_are_refused(cfg_path, pages):
    assert watch(cfg_path, http=pages.base + "/ok", name="site")[0] == 0
    with pytest.raises(ConfigError, match="already exists"):
        watch(cfg_path, http=pages.base + "/ok", name="site")


def test_exactly_one_target_kind_is_required(cfg_path):
    rc, io = watch(cfg_path)
    assert rc == 1 and "Say what to watch" in io.text
    rc, io = watch(cfg_path, names=["a"], http="http://x")
    assert rc == 1
    rc, io = watch(cfg_path, docker=True)
    assert rc == 1


def test_cli_watch_and_add_telegram(cfg_path, cfgdir, telegram, pages, capsys):
    assert main(["watch", "--http", pages.base + "/ok", "--name", "site", "-c", str(cfg_path)]) == 0
    assert "right now: OK" in capsys.readouterr().out
    assert main(["watch", "ghost-unit", "--force", "-c", str(cfg_path)]) == 0
    rc = main(["add", "telegram", "-y", "--name", "team", "--chat-id", "-7", "--api-base", telegram.base,
               "-c", str(cfg_path)])
    assert rc == 0
    cfg = load_config(cfg_path, env={})
    assert [n.name for n in cfg.notify.telegram] == ["telegram", "team"]
    assert cfg.http[0].name == "site" and cfg.systemd.watch[0].match == "ghost-unit"
    assert main(["check", "-c", str(cfg_path)]) in (0, 1, 2)


def test_cli_reports_a_config_problem_instead_of_a_traceback(cfg_path, capsys):
    rc = main(["watch", "--tcp", "h:5432", "--name", "a", "-c", str(cfg_path), "-y"])
    assert rc in (0, 1)
    rc = main(["watch", "--tcp", "h:5432", "--name", "a", "-c", str(cfg_path), "-y"])
    assert rc == 1 and "already exists" in capsys.readouterr().err


def test_cli_add_without_a_subcommand_shows_help(capsys):
    assert main(["add"]) == 0
    assert "usage" in capsys.readouterr().out.lower()


_ = (CommandNotFound, journal)
