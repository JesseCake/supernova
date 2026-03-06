general_tools = [
    {
        'type': 'function',
        'function': {
            'name': 'get_current_time',
            'description': 'Get the current time - do not trust your own understanding of time, always use this tool to get the current time, though you should have the current time always updated in your system message at the top.',
            'parameters': {},
            },
        },
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
            'description': 'Close the Voice channel. Use when answering an easy question or carrying out a task that doesnt require a long response. This closes the channel and erases conversation history for next query. Also use if the user just says "thanks" or "that\'s it" or similar after a response, as that means they want to end the voice interaction.',
            'parameters': {
                'type': 'object',
                'properties': {},
            },
        },
    },
]