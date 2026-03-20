"""
check_weather tool — current weather and 5-day forecast via OpenWeatherMap.
Config is read from config/check_weather.yaml.
"""
from typing import Annotated
from pydantic import Field
from collections import defaultdict
from datetime import datetime
import requests
import json


# ── Schema function ───────────────────────────────────────────────────────────

def check_weather(
    location: Annotated[str, Field(
        default="Brunswick, VIC, Australia",
        description=(
            "Optional. The location to fetch weather (temperature and conditions) for. "
            "Use commas and state initials/country name for accuracy e.g. 'Sydney, NSW, Australia'. "
            "Leave as default if the user just asks about the weather without specifying a location."
        )
    )] = "Brunswick, VIC, Australia",
    forecast: Annotated[bool, Field(
        default=False,
        description="Set to true to get a 5-day weather forecast instead of today's current weather/temperature/conditions."
    )] = False,
) -> str:
    """
    Fetch today's current weather or 5-day forecast for a location.
    Use when asked about the weather, temperature, or conditions, or when weather is relevant to another query.
    Use the default location if no location is specified.
    """
    ...


# ── Executor ──────────────────────────────────────────────────────────────────

def execute(tool_args: dict, session, core, tool_config: dict) -> str:
    params = tool_args.get('parameters', {})
    location = params.get('location', tool_config.get('default_location', 'Brunswick, VIC, Australia'))
    forecast = params.get('forecast', False)

    # API key comes from tool_config (set in check_weather.yaml) or falls back to core
    api_key = tool_config.get('api_key') or getattr(core, 'weather_api_key', None)
    home_coords = tool_config.get('home_coords', {})
    home_lat = home_coords.get('lat', -37.7746)
    home_lon = home_coords.get('lon', 144.9631)
    home_location = tool_config.get('default_location', 'Brunswick, VIC, Australia')

    core.send_whole_response(f"\n\rFetching weather for {location}. ", session)

    try:
        if forecast:
            core.send_whole_response("(5 day forecast). \n\r", session)
            if location == home_location:
                url = f"http://api.openweathermap.org/data/2.5/forecast?lat={home_lat}&lon={home_lon}&appid={api_key}&units=metric"
            else:
                url = f"http://api.openweathermap.org/data/2.5/forecast?q={location}&appid={api_key}&units=metric"

            response = requests.get(url)
            weather_data = response.json()

            if response.status_code == 200:
                days = defaultdict(list)
                for entry in weather_data['list']:
                    day = entry['dt_txt'].split(' ')[0]
                    days[day].append(entry)

                forecast_data = []
                for day, entries in sorted(days.items())[:5]:
                    midday = next((e for e in entries if '12:00' in e['dt_txt']), entries[0])
                    forecast_data.append({
                        'date': datetime.strptime(day, '%Y-%m-%d').strftime('%A, %d %B'),
                        'min_temp': f"{round(min(e['main']['temp_min'] for e in entries), 1)}°C",
                        'max_temp': f"{round(max(e['main']['temp_max'] for e in entries), 1)}°C",
                        'description': midday['weather'][0]['description'],
                    })

                return core._wrap_tool_result("check_weather", {"forecast": {"location": location, "days": forecast_data}})
            else:
                return core._wrap_tool_result("check_weather", {"text": f"Failed to fetch forecast: {weather_data.get('message', 'Unknown error')}"})

        else:
            core.send_whole_response("(current)\n\r", session)
            if location == home_location:
                url = f"http://api.openweathermap.org/data/2.5/weather?lat={home_lat}&lon={home_lon}&appid={api_key}&units=metric"
            else:
                url = f"http://api.openweathermap.org/data/2.5/weather?q={location}&appid={api_key}&units=metric"

            response = requests.get(url)
            weather_data = response.json()

            if response.status_code == 200:
                main = weather_data['main']
                return core._wrap_tool_result("check_weather", {"current_weather": {
                    'location': location,
                    'temperature': f"{main['temp']}°C",
                    'feels_like': f"{main['feels_like']}°C",
                    'humidity': f"{main['humidity']}%",
                    'description': weather_data['weather'][0]['description'],
                }})
            else:
                return core._wrap_tool_result("check_weather", {"text": f"Failed to fetch weather: {weather_data.get('message', 'Unknown error')}"})

    except Exception as e:
        return core._wrap_tool_result("check_weather", {"text": f"Error fetching weather: {str(e)}"})