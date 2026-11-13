import pytest

from .conftest import FakeBackend
from .test_api import BASIC, TOOL
from .test_conversations import followup

URL = "/v1/chat/completions"


@pytest.mark.parametrize("stream", [False, True])
async def test_single_user_text_is_exact_and_output_is_not_constrained(make_client, stream):
    text = '  原樣保留\n{"user_json":true}\n'
    answer = '# 自由回答\n```python\nprint("hello")\n```'
    http, _, backend = make_client(FakeBackend([[answer]]))
    async with http:
        response = await http.post(
            URL,
            json=BASIC
            | {
                "messages": [{"role": "user", "content": text}],
                "stream": stream,
            },
        )
    assert response.status_code == 200
    assert backend.prompts == [text]
    if not stream:
        assert response.json()["choices"][0]["message"]["content"] == answer
    else:
        assert "[DONE]" in response.text


async def test_system_and_developer_are_plain_text_and_not_repeated(make_client):
    http, _, backend = make_client(FakeBackend([["one"], ["two"]]))
    body = BASIC | {
        "messages": [
            {"role": "system", "content": "使用繁體中文"},
            {"role": "developer", "content": "保持簡潔"},
            {"role": "user", "content": "第一個問題"},
        ]
    }
    async with http:
        first = await http.post(URL, json=body)
        second = await http.post(URL, json=followup(body, first.json()["choices"][0]["message"]))
    assert first.status_code == second.status_code == 200
    assert backend.prompts[0] == (
        "請在這段對話中遵循以下指示：\n使用繁體中文\n\n保持簡潔\n\n使用者的訊息：\n第一個問題"
    )
    assert backend.prompts[1] == "next question"
    assert first.headers["X-Conversation-ID"] == second.headers["X-Conversation-ID"]


@pytest.mark.parametrize("role", ["system", "developer"])
@pytest.mark.parametrize("change", ["replace", "remove", "add"])
async def test_instruction_changes_continue_same_thread(make_client, role, change):
    http, _, backend = make_client(FakeBackend([["one"], ["two"], ["three"]]))
    instructions = [] if change == "add" else [{"role": role, "content": "old directive"}]
    body = BASIC | {"conversation_id": "same-thread", "messages": instructions + BASIC["messages"]}
    async with http:
        first = await http.post(URL, json=body)
        next_body = followup(body, first.json()["choices"][0]["message"])
        new_instructions = (
            [] if change == "remove" else [{"role": role, "content": "new directive"}]
        )
        next_body["messages"] = new_instructions + next_body["messages"][len(instructions) :]
        second = await http.post(URL, json=next_body)
        assert second.status_code == 200
        third = await http.post(
            URL, json=followup(next_body, second.json()["choices"][0]["message"])
        )
        assert third.status_code == 200
    assert second.headers["X-Conversation-ID"] == "same-thread"
    assert backend.states == [{}, {"turn": 1}, {"turn": 2}]
    if change == "remove":
        assert "指示已取消" in backend.prompts[1]
    else:
        assert "new directive" in backend.prompts[1]
    assert "old directive" not in backend.prompts[1]
    assert "Hi" not in backend.prompts[1]
    assert backend.prompts[2] == "next question"


async def test_auto_matching_allows_changed_system(make_client):
    http, _, _ = make_client(FakeBackend([["answer"], ["continued"]]))
    body = BASIC | {"messages": [{"role": "system", "content": "old"}] + BASIC["messages"]}
    async with http:
        first = await http.post(URL, json=body)
        next_body = followup(body, first.json()["choices"][0]["message"])
        next_body["messages"][0] = {"role": "system", "content": "new"}
        second = await http.post(URL, json=next_body)
    assert second.status_code == 200
    assert first.headers["X-Conversation-ID"] == second.headers["X-Conversation-ID"]


async def test_imported_history_is_readable_text_and_only_imported_once(make_client):
    http, _, backend = make_client(FakeBackend([["new answer"], ["continued"]]))
    body = BASIC | {
        "messages": [
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "new question"},
        ]
    }
    async with http:
        first = await http.post(URL, json=body)
        await http.post(URL, json=followup(body, first.json()["choices"][0]["message"]))
    assert backend.prompts[0] == (
        "先前的對話：\n\n使用者：\nold question\n\n助手：\nold answer\n\n現在的訊息：\nnew question"
    )
    assert backend.prompts[1] == "next question"


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "choice", ["auto", "required", {"type": "function", "function": {"name": "get_weather"}}]
)
async def test_tools_rejected_before_generation(make_client, stream, choice):
    http, service, backend = make_client()
    async with http:
        response = await http.post(
            URL, json=BASIC | {"tools": [TOOL], "tool_choice": choice, "stream": stream}
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_tools"
    assert not backend.prepared and not backend.prompts and not service.conversations.entries


async def test_disabled_tools_do_not_add_any_instructions_or_parse_outputs(make_client):
    raw = '{"type":"tool_call","name":"get_weather","arguments":{}}'
    http, _, backend = make_client(FakeBackend([[raw]]))
    async with http:
        result = await http.post(URL, json=BASIC | {"tools": [TOOL], "tool_choice": "none"})
    assert result.status_code == 200
    assert backend.prompts == ["Hi"]
    assert result.json()["choices"][0]["message"] == {"role": "assistant", "content": raw}
    assert result.json()["choices"][0]["finish_reason"] == "stop"


async def test_tool_history_rejected_even_when_tools_disabled(make_client):
    http, _, backend = make_client()
    async with http:
        result = await http.post(
            URL,
            json=BASIC
            | {
                "tool_choice": "none",
                "messages": BASIC["messages"]
                + [
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "get_weather", "arguments": "{}"},
                            }
                        ],
                    },
                    {"role": "tool", "content": "sunny", "tool_call_id": "call_1"},
                    {"role": "user", "content": "summarize"},
                ],
            },
        )
    assert result.status_code == 400
    assert result.json()["error"]["code"] == "unsupported_tool_history"
    assert not backend.prepared


async def test_legacy_thread_does_not_carry_structured_instructions(make_client):
    http, service, backend = make_client()
    service.conversations.save(
        "legacy-thread",
        {
            "model": "gemini-test",
            "backend": "cookie",
            "state": {},
            "hashes": [],
            "valid": True,
        },
    )
    async with http:
        explicit = await http.post(URL, json=BASIC | {"conversation_id": "legacy-thread"})
        assert explicit.status_code == 409
        assert explicit.json()["error"]["code"] == "conversation_mode_changed"
        automatic = await http.post(URL, json=BASIC)
    assert automatic.status_code == 200
    assert automatic.headers["X-Conversation-ID"] != "legacy-thread"
    assert backend.states == [{}] and backend.prompts == ["Hi"]
