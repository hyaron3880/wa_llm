import logging

import httpx

logger = logging.getLogger(__name__)

_WMO_CODES = {
    0: "בהיר ☀️",
    1: "בהיר בעיקר 🌤️",
    2: "מעונן חלקית ⛅",
    3: "מעונן ☁️",
    45: "ערפל 🌫️",
    48: "ערפל קפוא 🌫️",
    51: "טפטוף קל 🌦️",
    53: "טפטוף 🌦️",
    55: "טפטוף חזק 🌧️",
    61: "גשם קל 🌧️",
    63: "גשם 🌧️",
    65: "גשם חזק 🌧️",
    71: "שלג קל 🌨️",
    73: "שלג 🌨️",
    75: "שלג כבד 🌨️",
    80: "ממטרים קלים 🌦️",
    81: "ממטרים 🌧️",
    82: "ממטרים חזקים ⛈️",
    95: "סופת רעמים ⛈️",
    96: "סופת ברד ⛈️",
    99: "סופת ברד חזקה ⛈️",
}


async def get_weather(location: str) -> str:
    """Get current weather for a location using Open-Meteo (free, no API key)."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            geo_resp = await client.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": location, "count": 1, "language": "he"},
            )
            geo_resp.raise_for_status()
            geo_data = geo_resp.json()

            results = geo_data.get("results")
            if not results:
                return f"לא הצלחתי למצוא את המיקום '{location}'."

            place = results[0]
            lat = place["latitude"]
            lon = place["longitude"]
            place_name = place.get("name", location)

            weather_resp = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
                    "timezone": "auto",
                },
            )
            weather_resp.raise_for_status()
            current = weather_resp.json().get("current", {})

            temp = current.get("temperature_2m", "?")
            humidity = current.get("relative_humidity_2m", "?")
            wind = current.get("wind_speed_10m", "?")
            code = current.get("weather_code", 0)
            condition = _WMO_CODES.get(code, "לא ידוע")

            return (
                f"מזג האוויר ב{place_name}:\n"
                f"{condition}\n"
                f"🌡️ טמפרטורה: {temp}°C\n"
                f"💧 לחות: {humidity}%\n"
                f"💨 רוח: {wind} קמ״ש"
            )
    except Exception as e:
        logger.error(f"Weather fetch failed: {e}")
        return "לא הצלחתי לקבל נתוני מזג אוויר. נסו שוב מאוחר יותר."
