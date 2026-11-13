import json
from types import SimpleNamespace

import pytest
from gemini_webapi import GeminiClient
from gemini_webapi.constants import AccountStatus
from gemini_webapi.exceptions import APIError, AuthError, UsageLimitExceededError
from gemini_webapi.utils.decorators import running

from gemini_proxy.backends.cookie import CookieBackend, NoRetryClient
from gemini_proxy.errors import ProxyError


class Client:
    def __init__(self, psid, psidts, **kwargs):
        assert psid == "private-cookie"
        self._running = False
        self.account_status = AccountStatus.AVAILABLE
        self.requests = []
        self.closed = False

    async def init(self, **kwargs):
        assert kwargs["auto_refresh"] is True
        self._running = True

    def list_models(self):
        return [
            SimpleNamespace(
                model_name="account-model", display_name="Account model", is_available=True
            )
        ]

    async def generate_content_stream(self, prompt, **kwargs):
        assert kwargs == {"model": "account-model", "temporary": True}
        self.requests.append(prompt)
        yield SimpleNamespace(text_delta="first")
        yield SimpleNamespace(text_delta=" second")

    async def close(self):
        self.closed = True
        self._running = False


async def test_cookie_adapter_dynamic_models_and_isolated_calls(settings, monkeypatch):
    monkeypatch.setenv("GEMINI_COOKIE_PATH", str(settings.cookie_cache))
    settings.cookie_file.write_text(json.dumps({"__Secure-1PSID": "private-cookie"}))
    backend = CookieBackend(settings, Client)
    assert [m.id for m in await backend.models()] == ["account-model"]
    await backend.prepare("account-model")
    assert "".join([s async for s in backend.stream("one", "account-model")]) == "first second"
    assert "".join([s async for s in backend.stream("two", "account-model")]) == "first second"
    assert backend.client.requests == ["one", "two"]
    with pytest.raises(ProxyError) as exc:
        await backend.prepare("unavailable")
    assert exc.value.status == 404 and not exc.value.safe_fallback
    await backend.close()


@pytest.mark.parametrize(
    "failure,code,safe",
    [
        (AuthError(), "upstream_auth", True),
        (UsageLimitExceededError(), "upstream_rate_limit", False),
    ],
)
async def test_cookie_preflight_error_classification(settings, failure, code, safe):
    class Failing(Client):
        async def init(self, **kwargs):
            raise failure

    settings.cookie_file.write_text('{"__Secure-1PSID":"private-cookie"}')
    backend = CookieBackend(settings, Failing)
    with pytest.raises(ProxyError) as exc:
        await backend.prepare("account-model")
    assert exc.value.code == code and exc.value.safe_fallback == safe
    assert backend.client is None


async def test_cookie_does_not_accept_guest_session(settings):
    class Guest(Client):
        async def init(self, **kwargs):
            self._running = True
            self.account_status = AccountStatus.UNAUTHENTICATED

    settings.cookie_file.write_text('{"__Secure-1PSID":"private-cookie"}')
    backend = CookieBackend(settings, Guest)
    with pytest.raises(ProxyError) as exc:
        await backend.models()
    assert exc.value.code == "upstream_auth" and exc.value.safe_fallback


async def test_sdk_generation_retry_decorator_is_disabled(monkeypatch):
    calls = []

    @running(retry=5)
    async def failing(self, *args, **kwargs):
        calls.append(1)
        raise APIError("uncertain whether the generation was dispatched")
        yield  # pragma: no cover; deliberately make this an async generator

    monkeypatch.setattr(GeminiClient, "_generate", failing)
    client = NoRetryClient("private-cookie")
    client._running = True
    with pytest.raises(APIError):
        async for _ in client._generate("test"):
            pass
    assert len(calls) == 1


async def test_post_dispatch_failure_is_never_safe_to_fallback(settings):
    class Failing(Client):
        async def generate_content_stream(self, *args, **kwargs):
            yield SimpleNamespace(text_delta="partial")
            raise APIError("sensitive upstream payload")

    settings.cookie_file.write_text('{"__Secure-1PSID":"private-cookie"}')
    backend = CookieBackend(settings, Failing)
    await backend.prepare("account-model")
    with pytest.raises(ProxyError) as exc:
        async for _ in backend.stream("test", "account-model"):
            pass
    assert not exc.value.safe_fallback
    assert "sensitive" not in exc.value.message
    assert backend.client is None
