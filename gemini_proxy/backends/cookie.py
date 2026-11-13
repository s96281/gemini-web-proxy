import asyncio
import json
import os
from contextlib import aclosing

from gemini_webapi import GeminiClient
from gemini_webapi import exceptions as upstream
from gemini_webapi.constants import AccountStatus
from loguru import logger

from ..config import Settings
from ..errors import ProxyError
from .base import Model

# The pinned SDK logs raw upstream exceptions and cookie cache filenames. Disable its logger.
logger.disable("gemini_webapi")


class NoRetryClient(GeminiClient):
    """2.1.1's private decorator otherwise retries generation up to five times."""

    async def _generate(self, *args, **kwargs):
        kwargs["current_retry"] = 0
        async with aclosing(super()._generate(*args, **kwargs)) as outputs:
            async for output in outputs:
                yield output


def translate(exc: Exception, *, preflight: bool) -> ProxyError:
    if isinstance(exc, ProxyError):
        return exc
    if isinstance(exc, upstream.UsageLimitExceededError):
        return ProxyError("upstream_rate_limit", "Gemini quota exceeded; no automatic resend.", 429)
    if isinstance(exc, upstream.TemporarilyBlockedError):
        return ProxyError(
            "upstream_challenge", "Gemini blocked this session; check it manually.", 503
        )
    if isinstance(exc, upstream.AuthError):
        return ProxyError(
            "upstream_auth",
            "Gemini login expired; update Cookie or browser login.",
            503,
            safe_fallback=preflight,
        )
    if isinstance(exc, (TimeoutError, upstream.TimeoutError)):
        return ProxyError("upstream_timeout", "Gemini request timed out; no automatic resend.", 504)
    return ProxyError(
        "upstream_unavailable" if preflight else "upstream_protocol",
        "Gemini backend failed before dispatch."
        if preflight
        else "Gemini response failed; the request may already have been processed.",
        503 if preflight else 502,
        safe_fallback=preflight,
    )


class CookieBackend:
    name = "cookie"

    def __init__(self, settings: Settings, client_factory=NoRetryClient):
        self.settings = settings
        self.client_factory = client_factory
        self.client = None
        self.lock = asyncio.Lock()

    async def _init(self):
        async with self.lock:
            if self.client is not None and self.client._running:
                return
            await self.close()
            s = self.settings
            if not s.cookie_file.is_file():
                raise ProxyError(
                    "cookie_missing",
                    "Configure a Cookie file or browser login.",
                    503,
                    safe_fallback=True,
                )
            try:
                cookies = json.loads(s.cookie_file.read_text())
                if isinstance(cookies, list):
                    cookies = {c["name"]: c["value"] for c in cookies}
                psid = cookies.get("__Secure-1PSID")
                if not isinstance(psid, str) or not psid:
                    raise ValueError()
                s.cookie_cache.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.environ["GEMINI_COOKIE_PATH"] = str(s.cookie_cache.resolve())
                self.client = self.client_factory(
                    psid, cookies.get("__Secure-1PSIDTS"), proxy=s.upstream_proxy
                )
                async with asyncio.timeout(s.preflight_timeout):
                    await self.client.init(
                        timeout=s.request_timeout,
                        auto_refresh=True,
                        auto_close=False,
                        verbose=False,
                        watchdog_timeout=s.request_timeout,
                    )
                if self.client.account_status == AccountStatus.UNAUTHENTICATED:
                    raise upstream.AuthError()
                if self.client.account_status != AccountStatus.AVAILABLE:
                    raise ProxyError(
                        "upstream_challenge",
                        "Gemini account needs manual attention; check the website.",
                        503,
                    )
            except asyncio.CancelledError:
                await self.close()
                raise
            except Exception as exc:
                await self.close()
                raise translate(exc, preflight=True) from None

    async def models(self):
        await self._init()
        try:
            return [
                Model(m.model_name, m.display_name)
                for m in self.client.list_models() or []
                if m.is_available
            ]
        except Exception as exc:
            raise translate(exc, preflight=True) from None

    async def prepare(self, model):
        if model not in {m.id for m in await self.models()}:
            raise ProxyError("model_not_found", "Model is not available for this account.", 404)

    async def stream(self, prompt, model):
        try:
            # No ChatSession or upstream conversation metadata is reused between requests.
            async with aclosing(
                self.client.generate_content_stream(
                    prompt,
                    model=model,
                    temporary=True,
                )
            ) as outputs:
                async for output in outputs:
                    if output.text_delta:
                        yield output.text_delta
        except asyncio.CancelledError:
            await self.abort()
            raise
        except Exception as exc:
            await self.close()
            raise translate(exc, preflight=False) from None

    async def abort(self):
        await self.close()

    async def close(self):
        client, self.client = self.client, None
        if client is not None:
            await client.close()
