from __future__ import annotations

from unittest.mock import MagicMock

from jarvis.config import Config
from jarvis.security.confirm import Confirmer
from jarvis.security.permissions import PermissionManager
from jarvis.tools import ToolContext
from jarvis.tools import weather as weather_mod

from conftest import FakeIO


def make_ctx(tmp_path, audit, answers):
    io = FakeIO(answers)
    pm = PermissionManager(io, audit, store_path=tmp_path / "perms.json")
    return ToolContext(config=Config(raw={}), permissions=pm,
                       confirmer=Confirmer(io, audit), audit=audit)


def _responses(monkeypatch, geo, forecast):
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append(url)
        resp = MagicMock()
        resp.json.return_value = geo if "geocoding" in url else forecast
        return resp

    monkeypatch.setattr(weather_mod.httpx, "get", fake_get)
    return calls


GEO = {"results": [{"name": "Nashik", "admin1": "Maharashtra", "country": "India",
                    "latitude": 20.0, "longitude": 73.79}]}
FORECAST = {
    "current": {"temperature_2m": 31.2, "apparent_temperature": 34.0,
                "relative_humidity_2m": 55, "weather_code": 2, "wind_speed_10m": 9.4},
    "daily": {"time": ["2026-07-30"], "temperature_2m_max": [33.1],
              "temperature_2m_min": [24.0], "precipitation_sum": [0.2],
              "weather_code": [61]},
}


def test_weather_renders_conditions_in_words(tmp_path, audit, monkeypatch):
    _responses(monkeypatch, GEO, FORECAST)
    ctx = make_ctx(tmp_path, audit, ["allow once"])
    out = {t.name: t for t in weather_mod.build_tools(ctx)}["get_weather"]("Nashik")
    assert "Nashik, Maharashtra, India" in out
    assert "partly cloudy" in out      # code 2 translated, not a raw number
    assert "light rain" in out         # daily code 61 translated
    assert "31.2" in out
    assert "untrusted data" in out


def test_unknown_place_is_reported(tmp_path, audit, monkeypatch):
    _responses(monkeypatch, {"results": []}, FORECAST)
    ctx = make_ctx(tmp_path, audit, ["allow once"])
    out = {t.name: t for t in weather_mod.build_tools(ctx)}["get_weather"]("Zzzz")
    assert "couldn't find" in out


def test_network_failure_degrades_gracefully(tmp_path, audit, monkeypatch):
    def boom(*a, **k):
        raise weather_mod.httpx.ConnectError("offline")

    monkeypatch.setattr(weather_mod.httpx, "get", boom)
    ctx = make_ctx(tmp_path, audit, ["allow once"])
    out = {t.name: t for t in weather_mod.build_tools(ctx)}["get_weather"]("Pune")
    assert "Couldn't reach" in out


def test_denied_permission_makes_no_request(tmp_path, audit, monkeypatch):
    calls = _responses(monkeypatch, GEO, FORECAST)
    ctx = make_ctx(tmp_path, audit, ["deny"])
    out = {t.name: t for t in weather_mod.build_tools(ctx)}["get_weather"]("Pune")
    assert "declined" in out
    assert calls == []


def test_unknown_weather_code_does_not_crash():
    assert weather_mod._describe(9999) == "unknown conditions"
    assert weather_mod._describe(None) == "unknown conditions"
