"""Weather — current conditions and a short forecast.

Uses Open-Meteo, which needs no API key and no account, so every employee
gets working weather with nothing to configure. Two calls: a geocoding
lookup to turn "Nashik" into coordinates, then the forecast itself.

Location names come from the user, and the answer is public data, so this
is a plain read: permission-gated like any other capability, but nothing
here is a side effect.
"""

from __future__ import annotations

import httpx
from anthropic import beta_tool

from . import ToolContext, as_document

_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT = 15.0

# WMO weather interpretation codes — the API returns numbers, people want words.
_CONDITIONS = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "heavy freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail",
    99: "thunderstorm with heavy hail",
}


def _describe(code) -> str:
    try:
        return _CONDITIONS.get(int(code), "unknown conditions")
    except (TypeError, ValueError):
        return "unknown conditions"


def build_tools(ctx: ToolContext) -> list:
    @beta_tool
    def get_weather(location: str, days: int = 1) -> str:
        """Get current weather and a short forecast for a place.

        Args:
            location: City or place name, e.g. "Nashik" or "Pune, India".
            days: How many days of forecast to include (1-5). 1 = today only.
        """
        place = location.strip()
        if not place:
            return "Which place should I check the weather for?"
        if not ctx.permissions.require("weather_read", "look up the weather"):
            return "The user declined weather lookups."

        try:
            geo = httpx.get(_GEOCODE_URL,
                            params={"name": place, "count": 1, "language": "en"},
                            timeout=_TIMEOUT).json()
            matches = geo.get("results") or []
            if not matches:
                return (f"I couldn't find a place called '{place}'. Try adding the "
                        "country, like 'Nashik, India'.")
            spot = matches[0]
            label = ", ".join(
                str(p) for p in (spot.get("name"), spot.get("admin1"), spot.get("country")) if p
            )

            forecast = httpx.get(_FORECAST_URL, params={
                "latitude": spot["latitude"], "longitude": spot["longitude"],
                "current": ("temperature_2m,apparent_temperature,relative_humidity_2m,"
                            "precipitation,weather_code,wind_speed_10m"),
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,weather_code",
                "timezone": "auto",
                "forecast_days": max(1, min(int(days), 5)),
            }, timeout=_TIMEOUT).json()
        except (httpx.HTTPError, ValueError, KeyError) as e:
            ctx.audit.record("tool_call", tool="get_weather", detail=place, ok=False)
            return f"Couldn't reach the weather service: {e}"

        now = forecast.get("current", {})
        lines = [
            f"{label} — right now: {_describe(now.get('weather_code'))}, "
            f"{now.get('temperature_2m')}°C "
            f"(feels like {now.get('apparent_temperature')}°C), "
            f"humidity {now.get('relative_humidity_2m')}%, "
            f"wind {now.get('wind_speed_10m')} km/h."
        ]
        daily = forecast.get("daily", {})
        dates = daily.get("time") or []
        for i, date in enumerate(dates):
            lines.append(
                f"{date}: {_describe(daily.get('weather_code', [None] * len(dates))[i])}, "
                f"{daily.get('temperature_2m_min', [None] * len(dates))[i]}–"
                f"{daily.get('temperature_2m_max', [None] * len(dates))[i]}°C, "
                f"{daily.get('precipitation_sum', [None] * len(dates))[i]} mm rain."
            )

        ctx.audit.record("tool_call", tool="get_weather", detail=label, decision="ok")
        return as_document(f"weather:{label}", "\n".join(lines))

    return [get_weather]
