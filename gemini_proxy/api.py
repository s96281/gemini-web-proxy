import asyncio
import hmac
import json
import logging
import os
import uuid
from contextlib import aclosing, asynccontextmanager, suppress

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException
from starlette.requests import ClientDisconnect

from .backends.browser import BrowserBackend
from .backends.cookie import CookieBackend
from .config import Settings
from .errors import ProxyError
from .schema import ChatRequest
from .service import Service


class BodyLimit:
    def __init__(self, app, limit):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        # Buffer at most limit bytes before FastAPI's JSON parser allocates the body.
        if scope["method"] == "POST":
            chunks, size = [], 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                size += len(message.get("body", b""))
                if size > self.limit:
                    response = JSONResponse(
                        ProxyError("request_too_large", "Request too large.", 413).body(),
                        status_code=413,
                    )
                    return await response(scope, receive, send)
                chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    break
            pending = True

            async def limited_receive():
                nonlocal pending
                if pending:
                    pending = False
                    return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
                return await receive()

            return await self.app(scope, limited_receive, send)
        return await self.app(scope, receive, send)


async def connected(request, awaitable):
    """Cancel work if a client disappears while waiting for its first byte/JSON result."""

    async def disconnected():
        while (await request.receive())["type"] != "http.disconnect":
            pass

    operation = asyncio.ensure_future(awaitable)
    monitor = asyncio.create_task(disconnected())
    try:
        done, _ = await asyncio.wait([operation, monitor], return_when=asyncio.FIRST_COMPLETED)
        if monitor in done:
            raise ClientDisconnect()
        return await operation
    finally:
        for task in (operation, monitor):
            task.cancel()
        await asyncio.gather(operation, monitor, return_exceptions=True)


class Session:
    """Own the upstream generator in one task, including timeout and queue cleanup."""

    def __init__(self, source):
        self.source = source
        self.queue = asyncio.Queue(maxsize=1)
        self.task = asyncio.create_task(self._produce())

    async def _produce(self):
        try:
            async with aclosing(self.source):
                async for event in self.source:
                    await self.queue.put(event)
            await self.queue.put(("end", None))
        except ProxyError as exc:
            await self.queue.put(("error", exc))
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.queue.put(("error", ProxyError("internal_error", "Unexpected error.", 500)))

    async def next(self):
        return await self.queue.get()

    async def close(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


class ManagedStream(StreamingResponse):
    def __init__(self, content, session, **kwargs):
        super().__init__(content, **kwargs)
        self.session = session

    async def __call__(self, scope, receive, send):
        # Explicitly monitor disconnects on all ASGI versions, including during upstream silence.
        stream = asyncio.create_task(self.stream_response(send))
        disconnect = asyncio.create_task(self.listen_for_disconnect(receive))
        try:
            done, _ = await asyncio.wait([stream, disconnect], return_when=asyncio.FIRST_COMPLETED)
            if stream in done:
                with suppress(OSError):
                    await stream
        finally:
            stream.cancel()
            disconnect.cancel()
            await asyncio.gather(stream, disconnect, return_exceptions=True)
            await self.body_iterator.aclose()
            await self.session.close()


def create_app(settings: Settings | None = None, service: Service | None = None):
    os.umask(0o077)
    settings = settings or Settings()
    service = service or Service(
        settings,
        CookieBackend(settings),
        BrowserBackend(settings) if settings.browser_enabled else None,
    )

    @asynccontextmanager
    async def lifespan(app):
        yield
        await service.close()

    app = FastAPI(
        title="Gemini Web Proxy",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(BodyLimit, limit=settings.max_request_bytes)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["X-Request-ID", "X-Conversation-ID"],
    )
    app.state.service = service

    @app.exception_handler(ProxyError)
    async def proxy_error(request, exc):
        return JSONResponse(exc.body(), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic's errors include input values, which can contain private prompts.
        fields = sorted({str(e["loc"][1]) for e in exc.errors() if len(e["loc"]) > 1})
        error = ProxyError(
            "invalid_request",
            ("Unsupported or invalid request fields: " + ", ".join(fields))
            if fields
            else "Invalid request: "
            + "; ".join(
                str(e.get("ctx", {}).get("error", "Invalid request combination"))
                for e in exc.errors()
            ),
            400,
        )
        return JSONResponse(error.body(), status_code=400)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        error = ProxyError(
            "not_found" if exc.status_code == 404 else "http_error",
            "Endpoint not found." if exc.status_code == 404 else "HTTP request error.",
            exc.status_code,
        )
        return JSONResponse(error.body(), status_code=exc.status_code)

    async def authenticate(request: Request):
        expected = "Bearer " + settings.api_key.get_secret_value()
        if not hmac.compare_digest(
            request.headers.get("authorization", "").encode(), expected.encode()
        ):
            raise ProxyError("invalid_api_key", "A valid Bearer API key is required.", 401)

    @app.get("/health/live")
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready", dependencies=[Depends(authenticate)])
    async def ready():
        async with service.slot():
            models = await service.models()
            if not models:
                raise ProxyError("upstream_not_ready", "No usable Gemini models found.", 503)
        return {"status": "ready"}

    @app.get("/v1/models", dependencies=[Depends(authenticate)])
    async def models():
        async with service.slot():
            catalog = await service.models()
        return {
            "object": "list",
            "data": [
                {"id": m.id, "object": "model", "created": 0, "owned_by": "gemini-web"}
                for m in catalog
            ],
        }

    @app.post("/v1/chat/completions", dependencies=[Depends(authenticate)])
    async def chat(body: ChatRequest, request: Request):
        import time

        request_id = "chatcmpl-" + uuid.uuid4().hex
        created = int(time.time())
        session = Session(service.events(body, request_id))
        handed_off = False
        headers = {"X-Request-ID": request_id}

        def chunk(delta, finish=None):
            return {
                "id": request_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": body.model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }

        def sse(value):
            return "data: " + json.dumps(value, ensure_ascii=False) + "\n\n"

        try:
            first = await connected(request, session.next())
            if first[0] == "error":
                raise first[1]
            headers["X-Conversation-ID"] = body.conversation_id
            if not body.stream:

                async def collect():
                    event = first
                    text = []
                    message = None
                    while event[0] != "end":
                        kind, value = event
                        if kind == "error":
                            raise value
                        if kind == "text":
                            text.append(value)
                        if kind == "message":
                            message = value
                        event = await session.next()
                    return message or {"role": "assistant", "content": "".join(text)}

                message = await connected(request, collect())
                return JSONResponse(
                    {
                        "id": request_id,
                        "object": "chat.completion",
                        "created": created,
                        "model": body.model,
                        "choices": [
                            {
                                "index": 0,
                                "message": message,
                                "finish_reason": "tool_calls"
                                if message.get("tool_calls")
                                else "stop",
                            }
                        ],
                    },
                    headers=headers,
                )

            async def stream():
                try:
                    yield sse(chunk({"role": "assistant"}))
                    event = first
                    finish = "stop"
                    while event[0] != "end":
                        kind, value = event
                        if kind == "error":
                            yield sse(value.body())
                            return  # No success marker / [DONE] after a partial failure.
                        if kind == "text":
                            yield sse(chunk({"content": value}))
                        if kind == "message":
                            if value.get("tool_calls"):
                                finish = "tool_calls"
                                calls = [
                                    dict(call, index=i)
                                    for i, call in enumerate(value["tool_calls"])
                                ]
                                yield sse(chunk({"tool_calls": calls}))
                            else:
                                yield sse(chunk({"content": value["content"]}))
                        event = await session.next()
                    yield sse(chunk({}, finish))
                    yield "data: [DONE]\n\n"
                finally:
                    await session.close()

            response = ManagedStream(
                stream(),
                session,
                media_type="text/event-stream",
                headers=headers | {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
            handed_off = True
            return response
        finally:
            if not handed_off:
                await session.close()

    return app


def factory():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return create_app()
