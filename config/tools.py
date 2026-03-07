general_tools = [
    {
        'type': 'function',
        'function': {
            'name': 'perform_search',
            'description': 'Perform a search on the Web or Wikipedia if you need to research or have been asked to look for something, then use results to answer the user (do not use for simple calculation you can do yourself). Use the "web" source for general web search and "wikipedia" for more specific factual queries that are likely to be well-covered by Wikipedia.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {
                        'type': 'string',
                        'description': 'Search query. Required'
                    },
                    'source': {
                        'type': 'string',
                        'description': 'The source to search (options: "web", "wikipedia"). Required.'
                    },
                    'number': {
                        'type': 'integer',
                        'description': 'Number of results to return. Default is 10.',
                        'default': 10
                    }
                },
                'required': ['query', 'source'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'open_website',
            'description': 'Open a Website to see contents to answer user requests or do research. Use perform_search if you just want to search the web, this is for when you need to actually view a webpage to answer the user\'s question.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'url': {
                        'type': 'string',
                        'description': 'The full URL of the web page to view contents of. Required'
                    },
                },
                'required': ['url'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'check_weather',
            'description': 'Fetch the current weather or forecast information for a location. Use this when asked about the weather, or if you think it is relevant in the scope of another query or task. Use the default location if just asked about the weather rather than asking for a location.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'location': {
                        'type': 'string',
                        'description': 'Optional. Use this if the user is giving a location, otherwise leave as default. If using, use commas and state initials, country name to ensure correct city. Default is here at home: "Brunswick, VIC, Australia".',
                        'default': 'Brunswick, VIC, Australia'
                    },
                    'forecast': {
                        'type': 'boolean',
                        'description': 'Set to true to get a 5-day weather forecast. Default is false.',
                        'default': False
                    },
                },
            },
            'required' : [],
        },
    },
    {
        "type": "function",
        "function": {
            "name": "perform_math_operation",
            "description": "Perform basic mathematical operations if requested by the user. Use this for calculations you don't feel confident doing yourself, or seem terribly important that could cause harm if incorrect.",
            "parameters": {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "description": "The type of mathematical operation (options: 'addition', 'subtraction', 'multiplication', 'division', 'power', 'square_root'). Required."
                    },
                    "number1": {
                        "type": "number",
                        "description": "The first number involved in the calculation."
                    },
                    "number2": {
                        "type": "number",
                        "description": "The second number involved in the calculation (required for all operations except 'square_root')."
                    }
                },
                "required": ["operation", "number1"],
                "dependencies": {
                    "number2": ["addition", "subtraction", "multiplication", "division", "power"]
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_behaviour",
            "description": "Add a rule for yourself in future to change your own behaviour. Use this if asked to change the way you behave or have responded. Keep rules short and instructional.",
            "parameters": {
                "type": "object",
                "properties": {
                    "rule": {
                        "type": "string",
                        "description": "Short imperative rule, e.g. 'Keep replies under 10 words.' or 'Be more sarcastic' etc. Required."
                    }
                },
                "required": ["rule"],
                "additionalProperties": False
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "remove_behaviour",
            "description": "Remove a previously added behaviour rule (exact text match).",
            "parameters": {
                "type": "object",
                "properties": {
                    "rule": {
                        "type": "string",
                        "description": "Exact rule text to remove. Required."
                    }
                },
                "required": ["rule"],
                "additionalProperties": False
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_behaviour",
            "description": "List all active behaviour rules available to you. Useful to check what rules you have in place or have forgotten, so you can either update or add new ones to address a shortfall.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
]

voice_tools = [
    {
        'type': 'function',
        'function': {
            'name': 'close_voice_channel',
            'description': 'Call this after answering an easy question or responding with the results of another tool call to answer a question. This keeps voice communication brief and to the point. IMPORTANT: Do not ask a question when closing the voice channel, and do not ask if the user would like to close the voice channel, just close it as a final step after giving the answer or information.',
            'parameters': {
                'type': 'object',
                'properties': {},
            },
        },
    },
]

# --- Train departures tool definition (only included if ptv config present) ---
_train_departures_tool = {
    "type": "function",
    "function": {
        "name": "get_train_departures",
        "description": (
            "Get the next train departures from the local station. "
            "Use when asked about trains, the next train, how long until a train, when is my next train, when is my train,"
            "or whether to leave for the station. Returns scheduled and live departure times."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "description": "Number of upcoming departures to return. Default is 2.",
                    "default": 2,
                }
            },
            "required": [],
        },
    },
}


def get_tools(config=None):
    """
    Return the tool list for the current session.
    Pass an AppConfig to conditionally include optional tools.
    """
    tools = list(general_tools)
    if config is not None and getattr(config, "ptv", None):
        import os
        cache_ok = os.path.exists(config.ptv.cache_file)
        if cache_ok:
            tools.append(_train_departures_tool)
    return tools