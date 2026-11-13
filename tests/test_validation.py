import pytest
from pydantic import ValidationError

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
