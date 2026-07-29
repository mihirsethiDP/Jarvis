from __future__ import annotations

from jarvis.security.limits import ActionLimiter


def test_allows_up_to_the_hourly_cap_then_refuses(tmp_path):
    clock = [1_000_000.0]
    lim = ActionLimiter(caps={"send_email": (3, 10)}, path=tmp_path / "l.json",
                        now=lambda: clock[0])
    for _ in range(3):
        assert lim.check("send_email") is None
        lim.record("send_email")
    reason = lim.check("send_email")
    assert reason and "within an hour" in reason


def test_hourly_window_rolls_forward(tmp_path):
    clock = [1_000_000.0]
    lim = ActionLimiter(caps={"send_email": (2, 100)}, path=tmp_path / "l.json",
                        now=lambda: clock[0])
    lim.record("send_email"); lim.record("send_email")
    assert lim.check("send_email") is not None
    clock[0] += 3601                      # an hour later
    assert lim.check("send_email") is None


def test_daily_cap_still_applies_within_the_day(tmp_path):
    clock = [1_000_000.0]
    lim = ActionLimiter(caps={"send_email": (2, 3)}, path=tmp_path / "l.json",
                        now=lambda: clock[0])
    for _ in range(2):
        lim.record("send_email")
    clock[0] += 3601
    lim.record("send_email")              # 3rd today, hourly window is clear
    reason = lim.check("send_email")
    assert reason and "today" in reason


def test_actions_are_counted_separately(tmp_path):
    lim = ActionLimiter(caps={"send_email": (1, 5), "write_file": (1, 5)},
                        path=tmp_path / "l.json")
    lim.record("send_email")
    assert lim.check("send_email") is not None
    assert lim.check("write_file") is None


def test_unknown_actions_get_a_fallback_cap(tmp_path):
    lim = ActionLimiter(path=tmp_path / "l.json")
    assert lim.check("some_new_tool") is None   # permitted, but bounded


def test_corrupt_state_does_not_block_work(tmp_path):
    p = tmp_path / "l.json"
    p.write_text("{not json", encoding="utf-8")
    lim = ActionLimiter(path=p)
    assert lim.check("send_email") is None


def test_counts_survive_a_restart(tmp_path):
    path = tmp_path / "l.json"
    ActionLimiter(caps={"send_email": (1, 5)}, path=path).record("send_email")
    assert ActionLimiter(caps={"send_email": (1, 5)}, path=path).check("send_email")
