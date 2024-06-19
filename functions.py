import requests
import os
import json
from datetime import datetime



def get_current_weather(latitude, longitude):
  """Get the current weather in a given latitude and longitude"""
  base = "https://api.openweathermap.org/data/2.5/weather"
  key = os.environ['WEATHERMAP_API_KEY']
  request_url = f"{base}?lat={latitude}&lon={longitude}&appid={key}&units=metric"
  response = requests.get(request_url)

  body = response.json()

  if not "main" in body:
    return json.dumps({
      "latitude": latitude,
      "longitude": longitude,
      "error": body
    })
  else:
    return json.dumps({
      "latitude": latitude,
      "longitude": longitude,
      **body["main"]
    })


# Function to get the current time
def get_current_time():
    """Get the current time in a simple 12-hour format."""
    now = datetime.now()
    return json.dumps({
        "current_time": now.strftime('%I:%M%p')
    })


def end_conversation(transcriber):
    """End the current conversation and close the channel."""
    print("Clearing conversation history and closing channel")
    # Clear conversation history
    transcriber.current_conversation = None
    # Close the channel
    transcriber.channel_open = False
    # Play close channel sound
    transcriber.play_close_channel_sound()
    return json.dumps({
        "message": "The conversation has been ended.",
        "action": "end_conversation"
    })


definitions = [
  # get current time
  {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "Get the current time in a simple 12-hour format (e.g., '11:58AM').",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },

    # get current weather
    {
        "type": "function",
        "function": {
          "name": "get_current_weather",
          "description": "Get the current weather in a given latitude and longitude",
          "parameters": {
            "type": "object",
            "properties": {
              "latitude": {
                "type": "number",
                "description": "The latitude of a place",
              },
              "longitude": {
                "type": "number",
                "description": "The longitude of a place",
              },
            },
            "required": ["latitude", "longitude"],
          },
        },
      },

    {
        "type": "function",
        "function": {
            "name": "end_conversation",
            "description": "End the current conversation and close the channel.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    }
]

functions_json = json.dumps(definitions)
