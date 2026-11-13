import pytest
from pydantic import ValidationError

from gemini_proxy.errors import ProxyError
from gemini_proxy.prompts import parse_tool_output
from gemini_proxy.schema import ChatRequest

from .test_api import BASIC, TOOL


@pytest.mark.parametrize(
    "extra",
    [
        {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {}}]}]},
        {"messages": [{"role": "tool", "content": "no match", "tool_call_id": "unknown"}]},
        {"tools": [TOOL, TOOL]},
        {"tool_choice": "required"},
        {"tool_choice": {"type": "function", "function": {"name": "unknown"}}},
        {
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "bad",
                        "parameters": {"type": "object", "$ref": "https://example.com/private"},
                    },
                }
            ]
        },
    ],
)
def test_invalid_inputs_rejected(extra):
    with pytest.raises(ValidationError):
        ChatRequest(**(BASIC | extra))


@pytest.mark.parametrize(
    "raw,choice",
    [
        ('{"type":"text","content":"skip"}', "required"),
        ('{"type":"tool_call","name":"undefined","arguments":{}}', "auto"),
        ('{"type":"tool_call","name":"get_weather","arguments":{}}', "auto"),
        ('{"type":"tool_call","name":"get_weather","arguments":{"city":false}}', "auto"),
        ('{"type":"tool_call","name":"get_weather","arguments":{"city":"Taipei","x":1}}', "auto"),
        ('[{"type":"tool_call","name":"get_weather","arguments":{}}]', "auto"),
    ],
)
def test_invalid_tool_outputs_fail_closed(raw, choice):
    request = ChatRequest(**(BASIC | {"tools": [TOOL], "tool_choice": choice}))
    with pytest.raises(ProxyError) as exc:
        parse_tool_output(raw, request)
    assert exc.value.code == "invalid_tool_output"
