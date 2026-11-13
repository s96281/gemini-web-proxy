import json

import pytest

from gemini_proxy.errors import ProxyError

from .conftest import FakeBackend
from .test_api import BASIC, CALL, TOOL

URL = "/v1/chat/completions"


def followup(first, message, **extra):
    return (
        first
        | {
            "messages": first["messages"] + [message, {"role": "user", "content": "next question"}],
        }
        | extra
    )


async def test_auto_continuation_sends_only_new_messages_and_survives_restart(
    make_client, settings
):
    http, _, first = make_client(FakeBackend([["first answer"]]))
    async with http:
        response = await http.post(URL, json=BASIC)
    message = response.json()["choices"][0]["message"]
    conversation_id = response.headers["X-Conversation-ID"]
    # A new Service reads the persisted cursor and identifies the full history.
    http, _, second = make_client(FakeBackend([["second answer"]]))
    async with http:
        response = await http.post(URL, json=followup(BASIC, message))
    assert response.status_code == 200
    assert response.headers["X-Conversation-ID"] == conversation_id
    assert first.states == [{}] and second.states == [{"turn": 1}]
    assert '"Hi"' not in second.prompts[0]
    assert "first answer" not in second.prompts[0]
    assert "next question" in second.prompts[0]
    saved = settings.conversation_file.read_text()
    assert "first answer" not in saved and "next question" not in saved
    assert settings.conversation_file.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("stream", [False, True])
async def test_explicit_id_continuation_and_conflicting_retry(make_client, stream):
    http, _, backend = make_client(FakeBackend([["answer"], ["continued"]]))
    body = BASIC | {"conversation_id": "my-thread"}
    async with http:
        response = await http.post(URL, json=body | {"stream": stream})
        assert response.status_code == 200
        assert response.headers["X-Conversation-ID"] == "my-thread"
        if stream:
            assert "[DONE]" in response.text
        retry = await http.post(URL, json=body)
        assert retry.status_code == 409
        next_body = followup(body, {"role": "assistant", "content": "answer"})
        result = await http.post(URL, json=next_body)
        assert result.status_code == 200
    assert backend.states == [{}, {"turn": 1}]


@pytest.mark.parametrize("change", ["history", "model"])
async def test_explicit_id_rejects_edited_history_or_model(make_client, change):
    http, _, backend = make_client(FakeBackend([["answer"]]))
    body = BASIC | {"conversation_id": "thread"}
    async with http:
        response = await http.post(URL, json=body)
        next_body = followup(body, response.json()["choices"][0]["message"])
        if change == "history":
            next_body["messages"][0] = {"role": "user", "content": "edited"}
        else:
            next_body["model"] = "other"
        result = await http.post(URL, json=next_body)
    assert result.status_code == 409
    assert result.json()["error"]["code"] == "conversation_conflict"
    assert len(backend.prompts) == 1


async def test_independent_identical_prompts_start_distinct_threads(make_client):
    http, _, backend = make_client(FakeBackend([["same"], ["same"], ["selected"]]))
    async with http:
        first = await http.post(URL, json=BASIC)
        second = await http.post(URL, json=BASIC)
        assert first.headers["X-Conversation-ID"] != second.headers["X-Conversation-ID"]
        body = followup(BASIC, first.json()["choices"][0]["message"])
        ambiguous = await http.post(URL, json=body)
        assert ambiguous.status_code == 409
        selected = await http.post(
            URL, json=body | {"conversation_id": first.headers["X-Conversation-ID"]}
        )
        assert selected.status_code == 200
    assert backend.states == [{}, {}, {"turn": 1}]


async def test_failed_continuation_is_not_retried_or_switched(make_client):
    backend = FakeBackend([["answer"], ["partial", ProxyError("upstream_protocol", "failed")]])
    browser = FakeBackend(name="browser")
    http, _, _ = make_client(backend, browser)
    body = BASIC | {"conversation_id": "thread"}
    async with http:
        first = await http.post(URL, json=body)
        next_body = followup(body, first.json()["choices"][0]["message"])
        failed = await http.post(URL, json=next_body)
        assert failed.status_code == 502
        retry = await http.post(URL, json=next_body)
        assert retry.status_code == 409
        assert retry.json()["error"]["code"] == "conversation_uncertain"
    assert len(backend.prompts) == 2 and not browser.prepared


async def test_existing_cookie_thread_does_not_fall_back_on_preflight_failure(make_client):
    cookie = FakeBackend([["answer"]])
    browser = FakeBackend(name="browser")
    http, _, _ = make_client(cookie, browser)
    async with http:
        first = await http.post(URL, json=BASIC)
        cookie.prepare_error = ProxyError("upstream_auth", "expired", 503, safe_fallback=True)
        result = await http.post(URL, json=followup(BASIC, first.json()["choices"][0]["message"]))
    assert result.status_code == 503 and not browser.prepared


async def test_browser_thread_stays_on_browser_when_cookie_recovers(make_client):
    cookie = FakeBackend(
        prepare_error=ProxyError("cookie_missing", "missing", 503, safe_fallback=True)
    )
    browser = FakeBackend([["answer"], ["continued"]], name="browser")
    http, _, _ = make_client(cookie, browser)
    async with http:
        first = await http.post(URL, json=BASIC)
        cookie.prepare_error = None
        result = await http.post(URL, json=followup(BASIC, first.json()["choices"][0]["message"]))
    assert result.status_code == 200
    assert browser.states == [{}, {"turn": 1}] and not cookie.prompts


async def test_tool_result_continues_after_format_repair(make_client):
    backend = FakeBackend([["invalid"], [CALL], ['{"type":"text","content":"done"}']])
    http, _, _ = make_client(backend)
    body = BASIC | {"tools": [TOOL], "tool_choice": "required"}
    async with http:
        first = await http.post(URL, json=body)
        message = first.json()["choices"][0]["message"]
        call_id = message["tool_calls"][0]["id"]
        result = await http.post(
            URL,
            json=body
            | {
                "tool_choice": "auto",
                "messages": body["messages"]
                + [
                    message,
                    {
                        "role": "tool",
                        "content": "weather result",
                        "tool_call_id": call_id,
                    },
                ],
            },
        )
    assert result.status_code == 200
    assert backend.states == [{}, {"turn": 1}, {"turn": 2}]
    history = json.loads(backend.prompts[2].split("Conversation JSON:\n")[1])
    assert len(history) == 1 and history[0]["role"] == "tool"


async def test_conversation_cache_is_bounded(make_client, settings):
    settings.max_conversations = 1
    http, service, _ = make_client(FakeBackend([["one"], ["two"]]))
    async with http:
        await http.post(URL, json=BASIC | {"conversation_id": "first"})
        await http.post(URL, json=BASIC | {"conversation_id": "second"})
    assert list(service.conversations.entries) == ["second"]
