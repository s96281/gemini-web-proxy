import asyncio
from collections import deque

import httpx
import pytest

from gemini_proxy.api import create_app
from gemini_proxy.backends.base import Model
from gemini_proxy.config import Settings
from gemini_proxy.service import Service

KEY = "test-secret-at-least-16"
AUTH = {"Authorization": "Bearer " + KEY}


class FakeBackend:
    def __init__(self, responses=None, *, name="cookie", prepare_error=None, delay=0):
        self.name = name
        self.responses = deque(responses or [["Hello", " world"]])
        self.prepare_error = prepare_error
        self.delay = delay
        self.prompts = []
        self.prepared = []
        self.aborted = 0
        self.active = 0
        self.max_active = 0

    async def models(self):
        if self.prepare_error:
            raise self.prepare_error
        return [Model("gemini-test", "Test")]

    async def prepare(self, model):
        self.prepared.append(model)
        if self.prepare_error:
            raise self.prepare_error

    async def stream(self, prompt, model):
        self.prompts.append(prompt)
        response = self.responses.popleft()
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            for part in response:
                if self.delay:
                    await asyncio.sleep(self.delay)
                if isinstance(part, Exception):
                    raise part
                yield part
        finally:
            self.active -= 1

    async def abort(self):
        self.aborted += 1

    async def close(self):
        pass


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        api_key=KEY,
        cookie_file=tmp_path / "cookies.json",
        cookie_cache=tmp_path / "cache",
        browser_profile=tmp_path / "profile",
        browser_enabled=False,
        request_timeout=3,
        preflight_timeout=3,
    )


@pytest.fixture
def make_client(settings):
    def make(cookie=None, browser=None):
        cookie = cookie or FakeBackend()
        service = Service(settings, cookie, browser)
        app = create_app(settings, service)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=AUTH
        )
        return client, service, cookie

    return make
