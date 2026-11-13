import asyncio
import json

import pytest
from openai import AsyncOpenAI

from gemini_proxy.errors import ProxyError

from .conftest import AUTH, KEY, FakeBackend

BASIC = {"model": "gemini-test", "messages": [{"role": "user", "content": "Hi"}]}
TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
            "additionalProperties": False,
        },
    },
}
CALL = json.dumps({"type": "tool_call", "name": "get_weather", "arguments": {"city": "Taipei"}})


async def test_sdk_text_stream_and_models(make_client):
    http, _, _ = make_client(FakeBackend([["Hello", " world"], ["a", "b"]]))
    async with (
        http,
        AsyncOpenAI(api_key=KEY, base_url="http://test/v1", http_client=http, max_retries=0) as sdk,
    ):
        models = await sdk.models.list()
        assert models.data[0].id == "gemini-test"
        result = await sdk.chat.completions.create(**BASIC)
        assert result.choices[0].message.content == "Hello world"
        assert result.usage is None
        chunks = [c async for c in await sdk.chat.completions.create(**BASIC, stream=True)]
        assert "".join(c.choices[0].delta.content or "" for c in chunks) == "ab"
        assert chunks[-1].choices[0].finish_reason == "stop"


async def test_auth_validation_and_health(make_client):
    http, _, backend = make_client()
    async with http:
        assert (await http.get("/health/live", headers={"Authorization": ""})).status_code == 200
        assert (await http.get("/health/ready")).status_code == 200
        assert (await http.get("/v1/models", headers={"Authorization": "wrong"})).status_code == 401
        for extra in (
            {"temperature": 0.5},
            {"response_format": {"type": "json_object"}},
            {"parallel_tool_calls": True},
            {"stream_options": {"include_usage": "invalid"}},
        ):
            response = await http.post("/v1/chat/completions", json=BASIC | extra)
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "invalid_request"
        assert (await http.post("/v1/responses", json={})).status_code == 404
        assert not backend.prompts


async def test_full_tool_round_trip_via_sdk(make_client):
    http, _, backend = make_client(FakeBackend([[CALL], ['{"type":"text","content":"晴天"}']]))
    async with AsyncOpenAI(
        api_key=KEY, base_url="http://test/v1", http_client=http, max_retries=0
    ) as sdk:
        history = BASIC["messages"].copy()
        response = await sdk.chat.completions.create(
            model="gemini-test", messages=history, tools=[TOOL], tool_choice="required"
        )
        choice = response.choices[0]
        assert choice.finish_reason == "tool_calls"
        call = choice.message.tool_calls[0]
        assert json.loads(call.function.arguments) == {"city": "Taipei"}
        history += [
            choice.message.model_dump(exclude_none=True),
            {"role": "tool", "tool_call_id": call.id, "content": "晴天"},
        ]
        answer = await sdk.chat.completions.create(
            model="gemini-test", messages=history, tools=[TOOL]
        )
        assert answer.choices[0].message.content == "晴天"
        assert call.id in backend.prompts[1]
        assert '"role": "tool"' in backend.prompts[1]


async def test_validated_tool_stream(make_client):
    http, _, _ = make_client(FakeBackend([[CALL[:20], CALL[20:]]]))
    async with AsyncOpenAI(api_key=KEY, base_url="http://test/v1", http_client=http) as sdk:
        chunks = [
            c
            async for c in await sdk.chat.completions.create(
                **BASIC,
                tools=[TOOL],
                tool_choice={"type": "function", "function": {"name": "get_weather"}},
                stream=True,
            )
        ]
        calls = [c.choices[0].delta.tool_calls for c in chunks if c.choices[0].delta.tool_calls]
        assert len(calls) == 1
        assert calls[0][0].index == 0
        assert calls[0][0].function.name == "get_weather"
        assert chunks[-1].choices[0].finish_reason == "tool_calls"


async def test_invalid_tool_repair_once_and_no_unvalidated_output(make_client):
    bad = '{"type":"tool_call","name":"get_weather","arguments":{"city":123}}'
    backend = FakeBackend([[bad], [CALL], [bad], [bad]])
    http, _, _ = make_client(backend)
    async with http:
        result = await http.post("/v1/chat/completions", json=BASIC | {"tools": [TOOL]})
        assert result.status_code == 200
        assert len(backend.prompts) == 2
        assert "Previous invalid response" in backend.prompts[1]
        result = await http.post(
            "/v1/chat/completions", json=BASIC | {"tools": [TOOL], "stream": True}
        )
        assert result.status_code == 502
        assert result.json()["error"]["code"] == "invalid_tool_output"
        assert len(backend.prompts) == 4
        assert "123" not in result.text


async def test_isolation_role_serialization_and_no_tools_mode(make_client):
    http, _, backend = make_client(FakeBackend([["one"], ["two"]]))
    async with http:
        first = BASIC | {
            "messages": [
                {"role": "system", "content": "private first"},
                {"role": "developer", "content": "rules"},
                {"role": "user", "content": "question"},
            ]
        }
        await http.post("/v1/chat/completions", json=first)
        await http.post(
            "/v1/chat/completions", json=BASIC | {"tools": [TOOL], "tool_choice": "none"}
        )
        assert "private first" in backend.prompts[0]
        assert "private first" not in backend.prompts[1]
        assert "Tools: " not in backend.prompts[1]


async def test_safe_fallback_only_before_dispatch(make_client):
    primary = FakeBackend(
        prepare_error=ProxyError("upstream_auth", "Expired", 503, safe_fallback=True)
    )
    browser = FakeBackend(name="browser")
    http, _, _ = make_client(primary, browser)
    async with http:
        assert (await http.get("/v1/models")).status_code == 200
        response = await http.post("/v1/chat/completions", json=BASIC)
        assert response.status_code == 200
        assert len(browser.prompts) == 1
        assert not primary.prompts


@pytest.mark.parametrize(
    "code,status",
    [("upstream_rate_limit", 429), ("upstream_challenge", 503), ("model_not_found", 404)],
)
async def test_no_fallback_for_restricted_errors(make_client, code, status):
    primary = FakeBackend(prepare_error=ProxyError(code, "Stop", status))
    browser = FakeBackend(name="browser")
    http, _, _ = make_client(primary, browser)
    async with http:
        response = await http.post("/v1/chat/completions", json=BASIC)
        assert response.status_code == status
        assert not browser.prepared


async def test_partial_stream_failure_has_no_success_marker_or_fallback(make_client):
    browser = FakeBackend(name="browser")
    primary = FakeBackend([["partial", ProxyError("upstream_protocol", "Failed")]])
    http, service, _ = make_client(primary, browser)
    async with http:
        response = await http.post("/v1/chat/completions", json=BASIC | {"stream": True})
        assert "partial" in response.text and '"error"' in response.text
        assert "[DONE]" not in response.text and '"finish_reason": "stop"' not in response.text
        assert not browser.prepared
        assert not service.lock.locked()


async def test_timeout_releases_slot_and_does_not_resend(make_client, settings):
    settings.request_timeout = 0.03
    backend = FakeBackend([["late"]], delay=1)
    http, service, _ = make_client(backend)
    async with http:
        response = await http.post("/v1/chat/completions", json=BASIC)
        assert response.status_code == 504
        assert backend.aborted
        assert len(backend.prompts) == 1
        assert backend.active == 0 and not service.lock.locked()


async def test_queue_bounds_and_serialization(make_client, settings):
    settings.max_queue = 0
    backend = FakeBackend([["a"], ["b"]], delay=0.1)
    http, service, _ = make_client(backend)
    async with http:
        task = asyncio.create_task(http.post("/v1/chat/completions", json=BASIC))
        while not backend.active:
            await asyncio.sleep(0)
        second = await http.post("/v1/chat/completions", json=BASIC)
        assert second.status_code == 429
        assert (await task).status_code == 200
        assert backend.max_active == 1 and service.waiting == 0


async def test_request_size_limit_and_chunked_body(make_client, settings):
    settings.max_request_bytes = 128
    http, _, backend = make_client()

    async def parts():
        yield b'{"messages": "'
        yield b"x" * 200
        yield b'"}'

    async with http:
        response = await http.post("/v1/chat/completions", content=parts())
        assert response.status_code == 413 and not backend.prompts


async def test_cancellation_cleans_up_service(make_client):
    backend = FakeBackend([["pending"]], delay=10)
    http, service, _ = make_client(backend)
    task = asyncio.create_task(http.post("/v1/chat/completions", json=BASIC))
    while not backend.active:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert backend.active == 0 and not service.lock.locked()
    assert service.waiting == 0 and backend.aborted
    await http.aclose()


async def test_disconnect_during_silent_upstream(make_client):
    backend = FakeBackend([["pending"]], delay=10)
    http, service, _ = make_client(backend)
    app = http._transport.app
    incoming = asyncio.Queue()
    await incoming.put({"type": "http.request", "body": json.dumps(BASIC).encode()})

    async def receive():
        return await incoming.get()

    sent = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "method": "POST",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "headers": [
            (b"authorization", AUTH["Authorization"].encode()),
            (b"content-type", b"application/json"),
        ],
        "server": ("test", 80),
        "client": ("test", 1),
        "scheme": "http",
    }
    task = asyncio.create_task(app(scope, receive, send))
    while not backend.active:
        await asyncio.sleep(0)
    await incoming.put({"type": "http.disconnect"})
    try:
        await asyncio.wait_for(task, 1)
    except Exception as exc:
        from starlette.requests import ClientDisconnect

        assert isinstance(exc, ClientDisconnect)
    assert not service.lock.locked() and backend.active == 0
    await http.aclose()


async def test_queue_wait_timeout_preserves_active_request(make_client, settings):
    settings.queue_timeout = 0.02
    backend = FakeBackend([["a"], ["b"]], delay=0.1)
    http, service, _ = make_client(backend)
    async with http:
        first = asyncio.create_task(http.post("/v1/chat/completions", json=BASIC))
        while not backend.active:
            await asyncio.sleep(0)
        response = await http.post("/v1/chat/completions", json=BASIC)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "queue_timeout"
        assert (await first).status_code == 200
        assert service.waiting == 0 and not service.lock.locked()


async def test_cancel_during_browser_preparation_cleans_candidate(make_client):
    class SlowPrepare(FakeBackend):
        async def prepare(self, model, state=None):
            self.prepared.append(model)
            await asyncio.sleep(10)

    cookie = FakeBackend(
        prepare_error=ProxyError("cookie_missing", "missing", 503, safe_fallback=True)
    )
    browser = SlowPrepare(name="browser")
    http, service, _ = make_client(cookie, browser)
    task = asyncio.create_task(http.post("/v1/chat/completions", json=BASIC))
    while not browser.prepared:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert browser.aborted == 1
    assert not service.lock.locked()
    await http.aclose()


async def test_disconnect_after_first_streamed_content(make_client):
    backend = FakeBackend([["first", "second"]], delay=0.2)
    http, service, _ = make_client(backend)
    app = http._transport.app
    incoming = asyncio.Queue()
    await incoming.put(
        {"type": "http.request", "body": json.dumps(BASIC | {"stream": True}).encode()}
    )

    async def receive():
        return await incoming.get()

    sent = []

    async def send(message):
        sent.append(message)
        if b'"content": "first"' in message.get("body", b""):
            await incoming.put({"type": "http.disconnect"})

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "method": "POST",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "headers": [
            (b"authorization", AUTH["Authorization"].encode()),
            (b"content-type", b"application/json"),
        ],
        "server": ("test", 80),
        "client": ("test", 1),
        "scheme": "http",
    }
    await asyncio.wait_for(app(scope, receive, send), 2)
    assert backend.active == 0 and backend.aborted
    assert not service.lock.locked()
    assert not any(b"[DONE]" in message.get("body", b"") for message in sent)
    await http.aclose()


async def test_logs_do_not_contain_prompt_or_tool_output(make_client, caplog):
    import logging

    backend = FakeBackend([["PRIVATE_OUTPUT", ProxyError("upstream_protocol", "Failed")]])
    http, _, _ = make_client(backend)
    with caplog.at_level(logging.INFO, logger="gemini_proxy"):
        async with http:
            await http.post(
                "/v1/chat/completions",
                json=BASIC | {"messages": [{"role": "user", "content": "PRIVATE_PROMPT"}]},
            )
    assert "PRIVATE_PROMPT" not in caplog.text and "PRIVATE_OUTPUT" not in caplog.text
    assert "request=chatcmpl-" in caplog.text and "outcome=upstream_protocol" in caplog.text


async def test_chatbox_include_usage_is_accepted_without_fabricated_counts(make_client):
    http, _, _ = make_client()
    async with http:
        response = await http.post(
            "/v1/chat/completions",
            json=BASIC | {"stream": True, "stream_options": {"include_usage": True}},
        )
        assert response.status_code == 200
        assert "[DONE]" in response.text
        assert '"usage"' not in response.text


async def test_root_validation_error_has_explanation(make_client):
    http, _, _ = make_client()
    async with http:
        response = await http.post(
            "/v1/chat/completions", json=BASIC | {"stream_options": {"include_usage": "invalid"}}
        )
        assert response.status_code == 400
        assert "stream_options" in response.json()["error"]["message"]
