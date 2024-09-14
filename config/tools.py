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
            'description': 'Perform a search on the web or Wikipedia.',
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
                'required': ['action_type', 'entity_id'],
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
                        'description': 'Only enter a location if looking for anywhere other than home. The name of the location (e.g., city) to get the weather for. Default is here at home: "Brunswick, VIC, Australia".',
                        'default': 'Brunswick, VIC, Australia'
                    },
                    'forecast': {
                        'type': 'boolean',
                        'description': 'Set to true to get a 5-day weather forecast. Default is false.',
                        'default': False
                    },
                },
                'required': [],
            },
        },
    },
]

voice_tools = [
    {
        'type': 'function',
        'function': {
            'name': 'close_voice_channel',
            'description': 'Close the Voice channel. Only for use when you have answered a user request or the conversation has naturally come to an end.',
            'parameters': {
                'type': 'object',
                'properties': {},
            },
        },
    },
]