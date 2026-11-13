import asyncio
import functools
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from gemini_proxy.api import create_app
from gemini_proxy.backends.browser import BrowserBackend
from gemini_proxy.errors import ProxyError
from gemini_proxy.images import prepare_images
from gemini_proxy.schema import ChatRequest
from gemini_proxy.service import Service

from .conftest import AUTH, FakeBackend
from .test_api import BASIC
from .test_conversations import followup
from .test_images import IMAGE_REQUEST, PNG


@pytest.fixture(scope="module")
def fixture_url():
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    handler = functools.partial(QuietHandler, directory=str(Path(__file__).parent / "fixtures"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/gemini.html"
    server.shutdown()
    thread.join()
    server.server_close()


@pytest.fixture
async def browser(settings, fixture_url):
    # Full Chromium cold starts can take several seconds on shared CI workers.
    settings.preflight_timeout = 10
    settings.request_timeout = 15
    backend = BrowserBackend(settings, url=fixture_url)
    yield backend
    await backend.close()


async def test_browser_discovery_exact_model_and_incremental_generation(browser):
    models = await browser.models()
    assert [(m.id, m.label) for m in models] == [("web-fast", "Fast"), ("web-pro", "Pro")]
    await browser.prepare("web-pro")
    chunks = [s async for s in browser.stream("first request", "web-pro")]
    assert "".join(chunks) == "Hello browser"
    assert len(chunks) >= 2
    await browser.prepare("web-fast")
    assert await browser.page.locator("model-response").count() == 0
    assert (
        "".join([s async for s in browser.stream("isolated second", "web-fast")]) == "Hello browser"
    )


async def test_browser_reopens_saved_thread_and_waits_for_new_response(browser):
    state = {}
    await browser.prepare("web-fast")
    assert "".join([s async for s in browser.stream("first", "web-fast", state)]) == "Hello browser"
    saved_url = state["url"]
    await browser.close()
    await browser.prepare("web-fast", state)
    assert browser.page.url == saved_url
    assert await browser.page.locator("model-response").count() == 1
    assert (
        "".join([s async for s in browser.stream("second", "web-fast", state)]) == "Hello browser"
    )
    assert state["url"] == saved_url
    assert await browser.page.locator("model-response").count() == 2


async def test_browser_explicit_alias_and_no_silent_model_switch(browser):
    browser.settings.browser_model_map = {"gemini-flash": "Fast"}
    assert "gemini-flash" in {m.id for m in await browser.models()}
    await browser.prepare("gemini-flash")
    with pytest.raises(ProxyError, match="Cannot confirm"):
        await browser.prepare("gemini-unknown")


@pytest.mark.parametrize(
    "query,code",
    [
        ("login", "browser_login_required"),
        ("challenge", "upstream_challenge"),
        ("unconfirmed", "browser_model_unconfirmed"),
    ],
)
async def test_browser_preflight_failures(browser, fixture_url, query, code):
    browser.url = fixture_url + "?" + query
    browser.settings.preflight_timeout = 4
    with pytest.raises(ProxyError) as error:
        await browser.prepare("web-pro")
    assert error.value.code == code


@pytest.mark.parametrize(
    "query,code", [("quota", "upstream_rate_limit"), ("rewrite", "browser_output_rewritten")]
)
async def test_browser_generation_failures_do_not_complete(browser, fixture_url, query, code):
    browser.url = fixture_url + "?" + query
    await browser.prepare("web-fast")
    with pytest.raises(ProxyError) as error:
        async for _ in browser.stream("test", "web-fast"):
            pass
    assert error.value.code == code
    assert browser.context is None


async def test_real_browser_fallback_through_openai_endpoint(browser, settings):
    primary = FakeBackend(
        prepare_error=ProxyError("cookie_missing", "No cookie", 503, safe_fallback=True)
    )
    service = Service(settings, primary, browser)
    app = create_app(settings, service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=AUTH
    ) as client:
        response = await client.post("/v1/chat/completions", json=BASIC | {"model": "web-fast"})
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "Hello browser"
        assert not primary.prompts


async def test_browser_uploads_actual_images_then_continues_without_reupload(browser, settings):
    primary = FakeBackend(
        prepare_error=ProxyError("cookie_missing", "No cookie", 503, safe_fallback=True)
    )
    service = Service(settings, primary, browser)
    app = create_app(settings, service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=AUTH
    ) as client:
        body = IMAGE_REQUEST | {"model": "web-fast"}
        first = await client.post("/v1/chat/completions", json=body)
        assert first.status_code == 200
        uploaded = await browser.page.evaluate("window.sentImages")
        assert len(uploaded) == 1 and uploaded[0]["bytes"] == list(PNG)
        assert uploaded[0]["type"] == "image/png" and uploaded[0]["name"].endswith(".png")
        assert not await browser.page.evaluate("window.sentWhileUploading")
        url = browser.page.url
        second = await client.post(
            "/v1/chat/completions", json=followup(body, first.json()["choices"][0]["message"])
        )
        assert second.status_code == 200 and browser.page.url == url
        assert await browser.page.evaluate("window.sentImages") == []


async def test_browser_never_sends_when_upload_does_not_finish(browser, fixture_url, settings):
    browser.url = fixture_url + "?upload-hang"
    await browser.prepare("web-fast")
    settings.preflight_timeout = 1
    sent = []
    await browser.page.expose_function("recordSend", lambda: sent.append(True))
    images = prepare_images(ChatRequest(**IMAGE_REQUEST), 0, settings)
    with pytest.raises(ProxyError) as error:
        async for _ in browser.stream("inspect", "web-fast", {}, images):
            pass
    assert error.value.code == "browser_upload_failed"
    assert browser.context is None and not sent


async def test_browser_timeout_closes_profile_and_releases_queue(browser, settings, fixture_url):
    browser.url = fixture_url + "?hang"
    settings.request_timeout = 1.5
    primary = FakeBackend(
        prepare_error=ProxyError("cookie_missing", "No cookie", 503, safe_fallback=True)
    )
    service = Service(settings, primary, browser)
    request = ChatRequest(**(BASIC | {"model": "web-fast"}))
    with pytest.raises(ProxyError) as error:
        async for _ in service.events(request, "browser-timeout"):
            pass
    assert error.value.code == "upstream_timeout"
    assert not service.lock.locked() and browser.context is None


async def test_browser_cancellation_closes_context(browser, fixture_url):
    browser.url = fixture_url + "?hang"
    await browser.prepare("web-fast")

    async def consume():
        async for _ in browser.stream("pending", "web-fast"):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.4)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert browser.context is None
