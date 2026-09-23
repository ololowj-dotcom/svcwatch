from pathlib import Path

import pytest
from conftest import make_cfg, tomllib

from svcwatch.config import (
    ConfigError,
    Effective,
    explicit_names,
    load_config,
    parse_config,
    parse_env_file,
    resolve,
)
from svcwatch.template import render_config


def errors_of(toml_text, env=None):
    with pytest.raises(ConfigError) as info:
        make_cfg(toml_text, env=env)
    return str(info.value)


def test_empty_config_gives_working_defaults(tmp_path):
    cfg = make_cfg("", tmp_path)
    assert cfg.monitor.interval == 20
    assert cfg.systemd.enabled and cfg.systemd.discover == "custom"
    assert not cfg.docker.enabled
    assert cfg.notify.console and cfg.notify.telegram == []
    assert "Traceback" in cfg.logs.immediate


def test_relative_state_file_is_resolved_next_to_the_config(tmp_path):
    cfg = make_cfg('[monitor]\nstate_file = "data/state.json"\nlog_file = "svc.log"', tmp_path)
    assert cfg.monitor.state_file == tmp_path / "data" / "state.json"
    assert cfg.monitor.log_file == tmp_path / "svc.log"
    assert cfg.mute_file.name == "state.json.mute"


def test_env_interpolation_and_default():
    cfg = make_cfg(
        '[notify.telegram]\nbot_token = "${TOK}"\nchat_id = "${CHAT:-777}"', env={"TOK": "111:abc"})
    tg = cfg.notify.telegram[0]
    assert tg.bot_token == "111:abc" and tg.chat_id == "777"


def test_missing_env_var_is_reported_by_name():
    msg = errors_of('[notify.telegram]\nbot_token = "${NOPE}"\nchat_id = "1"')
    assert "NOPE" in msg and "notify.telegram.bot_token" in msg


def test_disabled_notifier_does_not_need_its_secrets():
    cfg = make_cfg('[notify.telegram]\nenabled = false\nbot_token = "${NOPE}"\nchat_id = "1"')
    assert cfg.notify.telegram == []


def test_unknown_key_suggests_the_right_one():
    msg = errors_of("[monitor]\nintervall = 5")
    assert "monitor.intervall" in msg and "did you mean 'interval'" in msg


def test_unknown_top_level_section():
    assert "sistemd" in errors_of("[sistemd]\nenabled = true")


def test_wrong_types_are_explained():
    msg = errors_of('[monitor]\ninterval = "fast"\ndedup_window = true')
    assert "monitor.interval: must be an integer" in msg
    assert "monitor.dedup_window: must be an integer" in msg


def test_ranges_are_enforced():
    msg = errors_of("[monitor]\ninterval = 0\n[[tcp]]\nname='x'\nport = 70000")
    assert "monitor.interval: must be >= 1" in msg
    assert "tcp[0].port: must be <= 65535" in msg


def test_all_problems_are_reported_together():
    msg = errors_of('[monitor]\ninterval = 0\n[logs]\nimmediate = ["re:(bad"]\n[systemd]\ndiscover = "some"')
    assert msg.count("\n") >= 2
    assert "broken regular expression" in msg and "invalid value 'some'" in msg


def test_broken_regex_in_rule_is_reported():
    msg = errors_of('[[systemd.watch]]\nmatch = "api"\nignore_extra = ["re:*oops"]')
    assert "systemd.watch[0].ignore_extra" in msg


def test_rule_requires_match():
    assert "'match' is required" in errors_of('[[systemd.watch]]\nlabel = "x"')


def test_unknown_notifier_in_route_gets_a_hint():
    msg = errors_of(
        '[notify.telegram]\nname = "ops"\nbot_token = "1:a"\nchat_id = "1"\n'
        '[[systemd.watch]]\nmatch = "api"\nnotify = ["opss"]')
    assert "unknown notifier 'opss'" in msg and "did you mean 'ops'" in msg


def test_duplicate_notifier_names_rejected():
    msg = errors_of(
        '[[notify.telegram]]\nname = "a"\nbot_token = "1:a"\nchat_id = "1"\n'
        '[[notify.telegram]]\nname = "a"\nbot_token = "2:b"\nchat_id = "2"')
    assert "used twice" in msg


def test_two_telegram_bots_get_distinct_default_names():
    cfg = make_cfg(
        '[[notify.telegram]]\nbot_token = "1:a"\nchat_id = "1"\n[[notify.telegram]]\nbot_token = "2:b"\nchat_id = 2')
    assert [t.name for t in cfg.notify.telegram] == ["telegram", "telegram-2"]
    assert cfg.notify.telegram[1].chat_id == "2"


def test_telegram_requires_token_and_chat():
    assert "'bot_token' and 'chat_id' are required" in errors_of('[notify.telegram]\nbot_token = "1:a"')


def test_min_severity_validated():
    assert "invalid value 'loud'" in errors_of(
        '[notify.telegram]\nbot_token = "1:a"\nchat_id = "1"\nmin_severity = "loud"')


def test_http_check_validation():
    assert "must start with http" in errors_of('[[http]]\nname = "x"\nurl = "ftp://a"')
    assert "'name' and 'url' are required" in errors_of('[[http]]\nname = "x"')
    cfg = make_cfg('[[http]]\nname = "x"\nurl = "https://a.b"\nexpect_status = [200, 204]')
    assert cfg.http[0].expect_status == [200, 204] and cfg.http[0].fail_threshold == 3


def test_check_names_must_be_unique():
    assert "used more than once" in errors_of(
        '[[tcp]]\nname = "db"\nport = 1\n[[process]]\nname = "db"\npattern = "x"')


def test_email_and_webhook_parse():
    cfg = make_cfg(
        '[notify.email]\nhost = "smtp.x"\nsender = "a@x"\nto = ["b@x"]\n'
        '[notify.webhook]\nurl = "https://hooks.x/y"\nheaders = { Authorization = "Bearer t" }')
    assert cfg.notify.email[0].port == 587 and cfg.notify.email[0].starttls
    assert cfg.notify.webhook[0].headers == {"Authorization": "Bearer t"}


def test_systemd_units_strip_suffix_and_rules_are_explicit_targets():
    cfg = make_cfg('[systemd]\nunits = ["nginx.service"]\n[[systemd.watch]]\nmatch = "api.service"\n[[systemd.watch]]\nmatch = "w-*"')
    assert cfg.systemd.units == ["nginx"]
    assert explicit_names(cfg.systemd.watch) == ["api"]


def _base():
    return Effective(
        label="", logs=True, alert_on=["failed"], restart_alert=True, immediate=["ERROR"], external=["gateway"],
        ignore=[], notify=None, fail_threshold=1, remind_after=0, journal_priority=0)


def test_resolve_applies_matching_rules_in_order():
    cfg = make_cfg(
        '[[systemd.watch]]\nmatch = "api-*"\nimmediate_extra = ["FATAL"]\nnotify = ["console"]\n'
        '[[systemd.watch]]\nmatch = "api-payments"\nlabel = "Payments"\nignore_extra = ["noise"]\nfail_threshold = 3')
    rules = cfg.systemd.watch
    pay = resolve(rules, "api-payments", _base())
    assert pay.label == "Payments" and pay.immediate == ["ERROR", "FATAL"]
    assert pay.ignore == ["noise"] and pay.fail_threshold == 3 and pay.notify == ["console"]
    other = resolve(rules, "api-users", _base())
    assert other.label == "api-users" and other.ignore == [] and other.fail_threshold == 1
    none = resolve(rules, "nginx", _base())
    assert none.immediate == ["ERROR"] and none.notify is None


def test_rule_replace_vs_extra():
    cfg = make_cfg('[[systemd.watch]]\nmatch = "x"\nimmediate = ["ONLY"]\nlogs = false\nalert_on = ["failed", "inactive"]')
    eff = resolve(cfg.systemd.watch, "x", _base())
    assert eff.immediate == ["ONLY"] and eff.logs is False and eff.alert_on == ["failed", "inactive"]


def test_glob_matching_is_case_insensitive():
    cfg = make_cfg('[[systemd.watch]]\nmatch = "API-*"\nlabel = "L"')
    assert resolve(cfg.systemd.watch, "api-one", _base()).label == "L"


def test_resolve_does_not_mutate_the_base():
    base = _base()
    cfg = make_cfg('[[systemd.watch]]\nmatch = "x"\nimmediate_extra = ["MORE"]')
    resolve(cfg.systemd.watch, "x", base)
    assert base.immediate == ["ERROR"]


def test_parse_env_file(tmp_path):
    f = tmp_path / ".env"
    f.write_text('# c\nA=1\nexport B="two words"\nC=\'x\'\n\nbroken\nD=a=b\n', encoding="utf-8")
    assert parse_env_file(f) == {"A": "1", "B": "two words", "C": "x", "D": "a=b"}


def test_load_config_reads_env_next_to_file_and_real_env_wins(tmp_path):
    (tmp_path / ".env").write_text("TOK=from-file\nCHAT=5\n", encoding="utf-8")
    cfg_file = tmp_path / "svcwatch.toml"
    cfg_file.write_text('[notify.telegram]\nbot_token = "${TOK}"\nchat_id = "${CHAT}"\n', encoding="utf-8")
    cfg = load_config(cfg_file, env={"CHAT": "9"})
    assert cfg.notify.telegram[0].bot_token == "from-file"
    assert cfg.notify.telegram[0].chat_id == "9"
    assert cfg.path == cfg_file.resolve()


def test_load_config_errors(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")
    bad = tmp_path / "bad.toml"
    bad.write_text("[monitor\ninterval = ", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(bad)
    ok = tmp_path / "ok.toml"
    ok.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="env file not found"):
        load_config(ok, env_file=tmp_path / "missing.env")


@pytest.mark.parametrize("chat", [None, "12345"])
@pytest.mark.parametrize("docker", [False, True])
def test_generated_config_template_is_always_valid(tmp_path, chat, docker):
    text = render_config(state_file=str(tmp_path / "s.json"), chat_id=chat, docker=docker)
    cfg = parse_config(tomllib.loads(text), {"TELEGRAM_BOT_TOKEN": "1:abc"}, tmp_path)
    assert cfg.docker.enabled is docker
    assert (len(cfg.notify.telegram) == 1) is bool(chat)
    if chat:
        assert cfg.notify.telegram[0].commands is True
    assert isinstance(cfg.monitor.state_file, Path)
