import asyncio
import logging
import re
import time
from contextlib import aclosing, asynccontextmanager

from .conversations import Conversations, fingerprints
from .errors import ProxyError
from .images import prepare_images
from .prompts import build_prompt, parse_tool_output, repair_prompt, tool_mode

log = logging.getLogger("gemini_proxy")


class Service:
    def __init__(self, settings, cookie, browser=None):
        self.settings = settings
        self.cookie = cookie
        self.browser = browser
        self.lock = asyncio.Lock()
        self.waiting = 0
        self.conversations = Conversations(settings.conversation_file, settings.max_conversations)

    async def abort_backend(self, backend):
        try:
            async with asyncio.timeout(5):
                await backend.abort()
        except Exception:
            log.warning("backend=%s cleanup_failed", backend.name)

    @asynccontextmanager
    async def slot(self):
        # All checks/updates run without awaits on one event loop. Includes models/readiness.
        if self.lock.locked() and self.waiting >= self.settings.max_queue:
            raise ProxyError("queue_full", "Request queue is full.", 429)
        self.waiting += 1
        acquired = False
        try:
            try:
                async with asyncio.timeout(self.settings.queue_timeout):
                    await self.lock.acquire()
                    acquired = True
            except TimeoutError:
                raise ProxyError(
                    "queue_timeout", "Timed out waiting for an upstream slot.", 503
                ) from None
        finally:
            self.waiting -= 1
        try:
            yield
        finally:
            if acquired:
                self.lock.release()

    async def models(self):
        # Keep the cookie model catalog authoritative while it is healthy. Browser aliases
        # are returned when the Cookie backend is unavailable.
        try:
            return await self.cookie.models()
        except ProxyError as exc:
            if not exc.safe_fallback or self.browser is None:
                raise
            return await self.browser.models()

    async def conversation_info(self, conversation_id):
        entry = self.conversations.entries.get(conversation_id)
        if entry is None:
            raise ProxyError("conversation_not_found", "Conversation mapping was not found.", 404)
        state = entry["state"]
        metadata = state.get("metadata", [])
        url = state.get("url")
        if entry["backend"] == "cookie" and metadata:
            match = re.fullmatch(r"c_([A-Za-z0-9_-]+)", metadata[0])
            if match:
                url = "https://gemini.google.com/app/" + match[1]
        info = {
            "conversation_id": conversation_id,
            "backend": entry["backend"],
            "model": entry["model"],
            "local_state": "completed" if entry["valid"] else "uncertain",
            "temporary": False,
            "gemini_chat_url": url,
            "listed_in_recent_history": None,
            "latest_turn_readable": None,
        }
        if entry["backend"] == "cookie":
            try:
                info.update(await self.cookie.history_status(state))
            except ProxyError as exc:
                info["history_check_error"] = exc.code
        return info

    async def select(self, model, request_id, entry=None):
        candidate = self.cookie if entry is None or entry["backend"] == "cookie" else self.browser
        if candidate is None:
            raise ProxyError("conversation_backend_unavailable", "Thread backend is disabled.", 503)
        try:
            try:
                await candidate.prepare(model, entry["state"] if entry else None)
            except ProxyError as exc:
                if entry is not None or not exc.safe_fallback or self.browser is None:
                    raise
                log.info("request=%s fallback=cookie->browser reason=%s", request_id, exc.code)
                candidate = self.browser
                await candidate.prepare(model)
            return candidate
        except BaseException:
            # prepare() can be cancelled before events() has received its backend reference.
            await self.abort_backend(candidate)
            raise

    async def collect(self, backend, prompt, model, state, images=None):
        pieces = []
        size = 0
        async with aclosing(backend.stream(prompt, model, state, images=images)) as outputs:
            async for text in outputs:
                size += len(text)
                if size > self.settings.max_output_chars:
                    raise ProxyError("output_too_large", "Upstream response exceeded output limit.")
                pieces.append(text)
        text = "".join(pieces)
        if not text:
            raise ProxyError("empty_response", "Gemini returned no text.")
        return text

    async def events(self, request, request_id):
        """Yield ('text', delta) or ('message', validated message); hold slot for whole stream."""
        started = time.monotonic()
        backend = None
        outcome = "ok"
        try:
            async with self.slot():
                try:
                    async with asyncio.timeout(self.settings.request_timeout):
                        entry, hashes = self.conversations.resolve(request)
                        start = len(entry["hashes"]) if entry else 0
                        images = prepare_images(request, start, self.settings)
                        prompt = build_prompt(request, start)
                        backend = await self.select(request.model, request_id, entry)
                        state = dict(entry["state"]) if entry else {}
                        # Persist uncertainty before dispatch, including process crashes.
                        entry = {
                            "model": request.model,
                            "backend": backend.name,
                            "state": state,
                            "hashes": entry["hashes"] if entry else hashes,
                            "valid": False,
                        }
                        self.conversations.save(request.conversation_id, entry)
                        if tool_mode(request):
                            raw = await self.collect(backend, prompt, request.model, state, images)
                            try:
                                message = parse_tool_output(raw, request)
                            except ProxyError:
                                # Intentional format repair only, never transport recovery.
                                await backend.prepare(request.model, state)
                                raw = await self.collect(
                                    backend,
                                    repair_prompt(
                                        build_prompt(request, len(request.messages)), raw
                                    ),
                                    request.model,
                                    state,
                                )
                                message = parse_tool_output(raw, request)
                            self.commit(request, entry, hashes, message)
                            yield "message", message
                        else:
                            size = 0
                            pieces = []
                            async with aclosing(
                                backend.stream(prompt, request.model, state, images=images)
                            ) as outputs:
                                async for text in outputs:
                                    size += len(text)
                                    if size > self.settings.max_output_chars:
                                        raise ProxyError(
                                            "output_too_large", "Output limit exceeded."
                                        )
                                    pieces.append(text)
                                    yield "text", text
                            if not size:
                                raise ProxyError("empty_response", "Gemini returned no text.")
                            self.commit(
                                request,
                                entry,
                                hashes,
                                {"role": "assistant", "content": "".join(pieces)},
                            )
                except BaseException:
                    # Abort on failure/cancellation while holding the slot, before next request.
                    if backend is not None:
                        await self.abort_backend(backend)
                    raise
        except TimeoutError:
            outcome = "upstream_timeout"
            raise ProxyError(
                "upstream_timeout", "Request timed out; no automatic resend.", 504
            ) from None
        except (asyncio.CancelledError, GeneratorExit):
            outcome = "cancelled"
            raise
        except ProxyError as exc:
            outcome = exc.code
            raise
        except Exception:
            outcome = "internal_error"
            raise ProxyError("internal_error", "Unexpected proxy error.", 500) from None
        finally:
            log.info(
                "request=%s backend=%s elapsed_ms=%d outcome=%s",
                request_id,
                backend.name if backend else "none",
                (time.monotonic() - started) * 1000,
                outcome,
            )

    def commit(self, request, entry, hashes, message):
        self.conversations.save(
            request.conversation_id,
            entry | {"hashes": hashes + fingerprints([message]), "valid": True},
        )

    async def close(self):
        await self.cookie.close()
        if self.browser:
            await self.browser.close()
