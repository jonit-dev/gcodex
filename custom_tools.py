"""Bridge Responses freeform tools through Gemini function declarations.

Streaming event names follow the OpenAI Responses streaming API:
https://platform.openai.com/docs/api-reference/responses-streaming
"""
from __future__ import annotations

import copy
import json


def custom_names(request):
    return {tool['name'] for tool in (request.get('tools') or [])
            if isinstance(tool, dict) and tool.get('type') == 'custom'
            and isinstance(tool.get('name'), str)}


def encode_request(request):
    result = copy.deepcopy(request)
    for tool in (result.get('tools') or []):
        if not isinstance(tool, dict) or tool.get('type') != 'custom':
            continue
        description = tool.get('description') or ''
        description += '\nSupply the exact raw tool input as the input string. Do not wrap it in markdown fences.'
        if tool.get('name') == 'apply_patch':
            description += (
                '\nFor update hunks, write a bare @@ line, followed by context/removal/addition lines. '
                'Unified-diff headers such as @@ -1,3 +1,4 @@ are NOT supported. '
                'Use this exact structural example with your actual file and lines:\n'
                '*** Begin Patch\n*** Update File: example.py\n@@\n-old_line\n+new_line\n*** End Patch'
            )
        grammar = tool.get('format', {})
        if isinstance(grammar, dict) and grammar.get('type') == 'grammar':
            description += '\nRequired input grammar:\n' + str(grammar.get('definition', ''))
        tool_format = {
            'type': 'function', 'name': tool['name'], 'description': description,
            'parameters': {'type': 'object', 'properties': {'input': {'type': 'string'}},
                           'required': ['input'], 'additionalProperties': False},
        }
        tool.clear()
        tool.update(tool_format)
    if isinstance(result.get('input'), list):
        for item in result['input']:
            if not isinstance(item, dict):
                continue
            if item.get('type') == 'custom_tool_call':
                item['type'] = 'function_call'
                item['arguments'] = json.dumps({'input': item.pop('input', '')})
            elif item.get('type') == 'custom_tool_call_output':
                item['type'] = 'function_call_output'
    choice = result.get('tool_choice')
    if isinstance(choice, dict) and choice.get('type') == 'custom':
        choice['type'] = 'function'
    return result


def raw_input(arguments):
    if arguments == '':
        return ''
    payload = json.loads(arguments)
    if not isinstance(payload, dict) or not isinstance(payload.get('input'), str):
        raise ValueError('custom tool call requires a string input')
    return payload['input']


class CustomToolDecoder:
    def __init__(self, request):
        self.names = custom_names(request)
        self.item_ids = set()

    def item(self, item):
        if item.get('type') != 'function_call' or item.get('name') not in self.names:
            return item
        result = dict(item)
        result['type'] = 'custom_tool_call'
        result['input'] = raw_input(result.pop('arguments', ''))
        self.item_ids.add(result.get('id'))
        return result

    def response(self, response):
        return {**response, 'output': [self.item(item) for item in response.get('output', [])]}

    def __call__(self, event):
        result = dict(event)
        if isinstance(result.get('item'), dict):
            result['item'] = self.item(result['item'])
        if isinstance(result.get('response'), dict):
            result['response'] = self.response(result['response'])
        if result.get('item_id') in self.item_ids:
            if result['type'] == 'response.function_call_arguments.delta':
                # The installed Google adapter emits complete arguments in one
                # delta per function call, not partial JSON fragments.
                result['type'] = 'response.custom_tool_call_input.delta'
                result['delta'] = raw_input(result['delta'])
            elif result['type'] == 'response.function_call_arguments.done':
                result['type'] = 'response.custom_tool_call_input.done'
                result['input'] = raw_input(result.pop('arguments'))
                result.pop('name', None)
        return result
