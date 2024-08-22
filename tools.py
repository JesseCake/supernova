tools = [
    {
        'type': 'function',
        'function': {
            'name': 'end_conversation',
            'description': 'End the conversation. Only for use when you have answered a user request or the conversation has naturally come to a stop.',
            'parameters': {
                'type': 'object',
                'properties': {},
            },
        },
    },

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
            'name': 'open_web_link',
            'description': 'Open Web Link to view page contents for knowledge seeking',
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
]
