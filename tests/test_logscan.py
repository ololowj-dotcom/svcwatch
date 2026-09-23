import re

import pytest

from svcwatch.logscan import Matcher, compile_pattern, fingerprint, normalize, scan
from svcwatch.models import LogLine


def L(*texts, priority=None):
    return [LogLine(t, priority) for t in texts]


def test_substring_is_case_insensitive():
    assert compile_pattern("traceback")("Traceback (most recent call last)")
    assert not compile_pattern("traceback")("all fine")


def test_regex_prefix_is_case_sensitive_regex():
    test = compile_pattern(r"re:timeout after \d+s")
    assert test("request timeout after 30s")
    assert not test("timeout after soon")
    assert not compile_pattern("re:ERROR")("an error occurred")


def test_broken_regex_raises():
    with pytest.raises(re.error):
        compile_pattern("re:(unclosed")


def test_empty_matcher_matches_nothing():
    m = Matcher([])
    assert not m
    assert not m.matches("anything")


def test_normalize_strips_timestamps_numbers_ids():
    a = "2026-01-05T10:11:12+0000 host app[123]: user 5521 failed id 0xdeadbeef"
    b = "2026-02-09 22:00:01,551 host app[999]: user 7 failed id 0xabc"
    assert normalize(a) == normalize(b)
    assert "2026" not in normalize(a)


def test_fingerprint_is_stable_across_occurrences_but_not_across_errors():
    a = ["2026-01-05T10:00:00 ValueError: bad value 12"]
    b = ["2026-03-01T23:59:59 ValueError: bad value 99"]
    c = ["2026-03-01T23:59:59 KeyError: 'x'"]
    assert fingerprint("t", a) == fingerprint("t", b)
    assert fingerprint("t", a) != fingerprint("t", c)
    assert fingerprint("t", a) != fingerprint("other", a)


def test_uuid_is_masked():
    x = normalize("job 3f2b8c1e-1111-4222-9333-444455556666 failed")
    y = normalize("job aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee failed")
    assert x == y


def test_traceback_becomes_one_group_with_context():
    lines = L(
        "starting", "Traceback (most recent call last):", '  File "a.py", line 3', "    boom()",
        "ValueError: nope", "next request ok", "still fine", "fine again", "and again", "more",
    )
    res = scan(lines, Matcher(["Traceback"]), Matcher([]), Matcher([]), context=4)
    assert len(res.groups) == 1
    assert res.groups[0][0].startswith("Traceback")
    assert "ValueError: nope" in res.groups[0]


def test_two_far_apart_errors_make_two_groups():
    lines = L("ERROR one", "ok", "ok", "ok", "ok", "ok", "ok", "ERROR two")
    res = scan(lines, Matcher(["ERROR"]), Matcher([]), Matcher([]), context=1)
    assert len(res.groups) == 2


def test_overlapping_hits_merge_into_one_group():
    lines = L("ERROR a", "ERROR b", "ERROR c", "ok")
    res = scan(lines, Matcher(["ERROR"]), Matcher([]), Matcher([]), context=2)
    assert len(res.groups) == 1
    assert len(res.groups[0]) == 4


def test_external_lines_are_counted_not_alerted():
    lines = L("Exception: Bad Gateway from upstream", "Exception: real bug")
    res = scan(lines, Matcher(["Exception"]), Matcher(["Bad Gateway"]), Matcher([]), context=0)
    assert res.external == 1
    assert len(res.groups) == 1 and "real bug" in res.groups[0][0]


def test_ignore_wins_over_everything():
    lines = L("Exception: healthcheck ok", "Bad Gateway healthcheck ok")
    res = scan(lines, Matcher(["Exception"]), Matcher(["Bad Gateway"]), Matcher(["healthcheck"]), context=0)
    assert res.groups == [] and res.external == 0


def test_priority_threshold_triggers_alert():
    lines = [LogLine("disk failure", 3), LogLine("just info", 6)]
    res = scan(lines, Matcher([]), Matcher([]), Matcher([]), context=0, priority_max=3)
    assert [g[0] for g in res.groups] == ["disk failure"]


def test_priority_off_by_default():
    res = scan([LogLine("disk failure", 3)], Matcher([]), Matcher([]), Matcher([]), context=0)
    assert res.groups == []


def test_no_lines_no_result():
    res = scan([], Matcher(["x"]), Matcher([]), Matcher([]))
    assert res.groups == [] and res.external == 0


def test_blank_context_lines_are_dropped():
    lines = L("ERROR x", "   ", "detail")
    res = scan(lines, Matcher(["ERROR"]), Matcher([]), Matcher([]), context=2)
    assert res.groups == [["ERROR x", "detail"]]
