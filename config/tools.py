general_tools = [
    {
        'type': 'function',
        'function': {
            'name': 'get_current_time',
            'description': 'Get the current time - do not trust your own understanding of time, always use this tool to get the current time',
            'parameters': {},
            },
        },
    {
        'type': 'function',
        'function': {
            'name': 'perform_search',
            'description': 'Perform a search on the Web or Wikipedia if you need to research or have been asked to look for something, then use results to answer the user (do not use for simple calculation you can do yourself)',
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
            'description': 'Fetch the current weather or forecast information for a location.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'location': {
                        'type': 'string',
                        'description': 'The name of the location (e.g., city) to get the weather for. Use commas and state initials, country name to ensure correct city. Default is here at home: "Brunswick, VIC, Australia". For queries without a place name, leave as default',
                        'default': 'Brunswick, VIC, Australia'
                    },
                    'forecast': {
                        'type': 'boolean',
                        'description': 'Set to true to get a 5-day weather forecast. Default is false.',
                        'default': False
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "perform_math_operation",
            "description": "Perform basic mathematical operations if requested by the user.",
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
            "description": "Add a rule for yourself in future to change your own behaviour. Usually if asked to change the way you behave, this is how you do it. Keep rules short and imperative.",
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
            "description": "List all active behaviour rules.",
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
            'description': 'Close the Voice channel. Use when answering an easy question or carrying out a task that doesnt require a long response. This closes the channel and erases conversation history for next query. Also use if the user just says "thanks" or "that\'s it" or similar after a response, as that means they want to end the voice interaction.',
            'parameters': {
                'type': 'object',
                'properties': {},
            },
        },
    },
]