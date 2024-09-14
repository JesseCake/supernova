general_tools = [
    {
        'type': 'function',
        'function': {
            'name': 'get_current_time',
            'description': 'Get the current time',
            'parameters': {
                'type': 'object',
                'properties': {},
                },
            },
        },

    {
        'type': 'function',
        'function': {
            'name': 'web_search',
            'description': 'Perform a web search, receive links and headers for further knowledge seeking',
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {
                        'type': 'string',
                        'description': 'The query to search the internet with'
                    },
                    'number': {
                        'type': 'integer',
                        'description': 'Number of results to return. Default is 10.'
                    }
                },
                'required': ['query'],
            },
        },
    },

    {
        'type': 'function',
        'function': {
            'name': 'wikipedia_search',
            'description': 'Perform a wikipedia search, receive titles, summaries, and links further knowledge seeking',
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {
                        'type': 'string',
                        'description': 'The query to search the Wikipedia with'
                    },
                },
                'required': ['query'],
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
            'name': 'ha_set_switch',
            'description': 'Set the state of a switch in the Home Automation. IMPORTANT: you must check the real entity id before using this',
            'parameters': {
                'type': 'object',
                'properties': {
                    'entity_id': {
                        'type': 'string',
                        'description': 'The id of the switch entity'
                    },
                    'state': {
                        'type': 'string',
                        'description': 'The desired state. Either on or off'
                    },
                },
                'required': ['entity_id', 'state'],
            },
        },
    },

{
        'type': 'function',
        'function': {
            'name': 'ha_activate_scene',
            'description': 'Activate lighting scene in the Home Automation.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'scene_id': {
                        'type': 'string',
                        'description': 'The id of the scene entity'
                    },
                },
                'required': ['scene_id'],
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