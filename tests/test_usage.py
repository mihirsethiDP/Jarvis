from __future__ import annotations

from jarvis.usage import TurnBudget


def test_allows_until_limit_then_blocks(tmp_path):
    b = TurnBudget(3, path=tmp_path / "u.json", today=lambda: "2026-07-29")
    for _ in range(3):
        assert b.allow() is True
        b.record()
    assert b.allow() is False
    assert b.used_today() == 3


def test_budget_resets_on_a_new_day(tmp_path):
    day = ["2026-07-29"]
    b = TurnBudget(1, path=tmp_path / "u.json", today=lambda: day[0])
    b.record()
    assert b.allow() is False
    day[0] = "2026-07-30"
    assert b.allow() is True
    assert b.used_today() == 0


def test_zero_limit_means_unlimited(tmp_path):
    b = TurnBudget(0, path=tmp_path / "u.json")
    for _ in range(5):
        b.record()
    assert b.allow() is True


def test_corrupt_usage_file_recovers(tmp_path):
    p = tmp_path / "u.json"
    p.write_text("{broken", encoding="utf-8")
    b = TurnBudget(2, path=p, today=lambda: "2026-07-29")
    assert b.allow() is True
    b.record()
    assert b.used_today() == 1
