general_tools = [
    {
        'type': 'function',
        'function': {
            'name': 'get_current_time',
            'description': 'Get the current time',
            'parameters': {},
            },
        },
    {
        'type': 'function',
        'function': {
            'name': 'perform_search',
            'description': 'Perform a search on Google or Wikipedia. (do not use for simple calculation you can do yourself)',
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {
                        'type': 'string',
                        'description': 'The query to search with.'
                    },
                    'source': {
                        'type': 'string',
                        'description': 'The source to search (options: "web", "wikipedia").'
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
            'description': 'Open Website to see contents to answer user requests',
            'parameters': {
                'type': 'object',
                'properties': {
                    'url': {
                        'type': 'string',
                        'description': 'The full URL of the web page to view contents of'
                    },
                },
                'required': ['url'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'home_automation_action',
            'description': 'Perform actions in the Home Automation system (e.g., set a switch, activate a scene) as requested by user.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'action_type': {
                        'type': 'string',
                        'description': 'The type of action (options: "set_switch", "activate_scene").'
                    },
                    'entity_id': {
                        'type': 'string',
                        'description': 'The ID of the switch or scene entity.'
                    },
                    'state': {
                        'type': 'string',
                        'description': 'The desired state for switches (either "on" or "off"). Required if action_type is "set_switch".'
                    }
                },
                'required': ['action_type', 'entity_id', 'state'],
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
            "description": "Perform basic mathematical operations as requested by the user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "description": "The type of mathematical operation (options: 'addition', 'subtraction', 'multiplication', 'division', 'power', 'square_root')."
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
]

voice_tools = [
    {
        'type': 'function',
        'function': {
            'name': 'close_voice_channel',
            'description': 'Close the Voice channel. Only for use when you have answered a user request or the conversation has naturally come to an end. Do not say "the conversation has ended" when using this tool, just call it',
            'parameters': {
                'type': 'object',
                'properties': {},
            },
        },
    },
]