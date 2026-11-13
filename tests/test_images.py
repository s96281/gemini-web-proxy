import asyncio
import base64
import json
from pathlib import Path

import pytest
from gemini_webapi.constants import AccountStatus
from gemini_webapi.types import Candidate, ModelOutput
from pydantic import ValidationError

from gemini_proxy.backends.cookie import CookieBackend, NoRetryClient
from gemini_proxy.images import prepare_images
from gemini_proxy.schema import ChatRequest

from .conftest import FakeBackend
from .test_api import BASIC, CALL, TOOL
from .test_conversations import followup
from .test_cookie import Client

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII="
)
IMAGE_URL = "data:image/png;base64," + base64.b64encode(PNG).decode()
IMAGE_PART = {"type": "image_url", "image_url": {"url": IMAGE_URL, "detail": "auto"}}
IMAGE_REQUEST = BASIC | {
    "messages": [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What is this image?"},
                IMAGE_PART,
            ],
        }
    ],
}
URL = "/v1/chat/completions"


@pytest.mark.parametrize("stream", [False, True])
async def test_images_reach_backend_as_bytes_not_prompt_text(make_client, stream, settings):
    http, _, backend = make_client()
    async with http:
        response = await http.post(URL, json=IMAGE_REQUEST | {"stream": stream})
    assert response.status_code == 200
    image = backend.images[0][0]
    assert image.data == PNG and image.mime_type == "image/png" and image.name.endswith(".png")
    assert image.name in backend.prompts[0]
    assert IMAGE_URL not in backend.prompts[0]
    assert "What is this image?" in backend.prompts[0]
    assert "base64" not in settings.conversation_file.read_text()


async def test_image_only_and_multiple_images_preserve_attachment_order(make_client):
    http, _, backend = make_client()
    async with http:
        response = await http.post(
            URL,
            json=BASIC
            | {
                "messages": [{"role": "user", "content": [IMAGE_PART, IMAGE_PART]}],
            },
        )
    assert response.status_code == 200
    assert len(backend.images[0]) == 2
    assert backend.images[0][0].name != backend.images[0][1].name
    parts = json.loads(backend.prompts[0].split("Conversation JSON:\n")[1])[0]["content"]
    assert [p["filename"] for p in parts] == [image.name for image in backend.images[0]]


async def test_history_images_are_not_reuploaded_but_new_images_are(make_client):
    http, _, backend = make_client(FakeBackend([["first"], ["second"], ["third"]]))
    async with http:
        first = await http.post(URL, json=IMAGE_REQUEST)
        body = followup(IMAGE_REQUEST, first.json()["choices"][0]["message"])
        second = await http.post(URL, json=body)
        third = await http.post(
            URL,
            json=body
            | {
                "messages": body["messages"]
                + [
                    second.json()["choices"][0]["message"],
                    {"role": "user", "content": [IMAGE_PART]},
                ]
            },
        )
    assert third.status_code == 200
    assert [len(images) for images in backend.images] == [1, 0, 1]
    assert backend.images[2][0].name != backend.images[0][0].name
    assert third.headers["X-Conversation-ID"] == first.headers["X-Conversation-ID"]


async def test_image_with_tools_repairs_without_reupload(make_client):
    http, _, backend = make_client(FakeBackend([["bad"], [CALL]]))
    async with http:
        result = await http.post(URL, json=IMAGE_REQUEST | {"tools": [TOOL]})
    assert result.status_code == 200
    assert result.json()["choices"][0]["message"]["tool_calls"]
    assert [len(images) for images in backend.images] == [1, 0]


@pytest.mark.parametrize(
    "url,code",
    [
        ("https://example.com/image.png", "unsupported_image_url"),
        ("file:///private/image.png", "unsupported_image_url"),
        ("data:image/svg+xml;base64,PHN2Zz4=", "unsupported_image_url"),
        ("data:image/png;base64,not base64", "invalid_image"),
        ("data:image/png;base64,", "invalid_image"),
        ("data:image/jpeg;base64," + base64.b64encode(PNG).decode(), "invalid_image"),
    ],
)
async def test_bad_images_are_rejected_before_backend_or_conversation_creation(
    make_client, url, code
):
    http, service, backend = make_client()
    async with http:
        result = await http.post(
            URL,
            json=BASIC
            | {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": url}},
                        ],
                    }
                ]
            },
        )
    assert result.status_code == 400
    assert result.json()["error"]["code"] == code
    assert not backend.prepared and not service.conversations.entries
    assert url not in result.text


@pytest.mark.parametrize(
    "setting,value,code",
    [
        ("max_image_bytes", 2, "image_too_large"),
        ("max_total_image_bytes", 2, "image_too_large"),
        ("max_images", 1, "too_many_images"),
    ],
)
async def test_image_limits(make_client, settings, setting, value, code):
    setattr(settings, setting, value)
    http, _, backend = make_client()
    async with http:
        result = await http.post(
            URL,
            json=BASIC
            | {
                "messages": [
                    {"role": "user", "content": [IMAGE_PART, IMAGE_PART]},
                ]
            },
        )
    assert result.status_code == (400 if setting == "max_images" else 413)
    assert result.json()["error"]["code"] == code and not backend.prepared


@pytest.mark.parametrize(
    "part,role",
    [
        (IMAGE_PART, "system"),
        (IMAGE_PART, "assistant"),
        ({"type": "image_url", "image_url": {"url": IMAGE_URL, "detail": "invalid"}}, "user"),
        ({"type": "input_audio", "input_audio": {}}, "user"),
    ],
)
def test_invalid_image_parts(part, role):
    with pytest.raises(ValidationError):
        ChatRequest(**(BASIC | {"messages": [{"role": role, "content": [part]}]}))


async def test_pinned_sdk_uploads_paths_with_image_names_and_cleans_files(settings, monkeypatch):
    uploaded = []
    generated = []

    class SDK(NoRetryClient):
        async def init(self, **kwargs):
            self._running = True
            self.account_status = AccountStatus.AVAILABLE
            self.client = object()

        def list_models(self):
            return Client.list_models(self)

        async def _sync_activity(self):
            pass

        def _check_account_status(self, **kwargs):
            return True

        async def _generate(self, **kwargs):
            generated.append(kwargs)
            kwargs["chat"].metadata = ["c_image", "r_image", "rc_image"]
            yield ModelOutput(
                metadata=["c_image", "r_image"],
                candidates=[
                    Candidate(rcid="rc_image", text="seen", text_delta="seen"),
                ],
            )

        async def close(self):
            self.client = None
            self._running = False

    async def upload(file, **kwargs):
        assert file.read_bytes() == PNG and file.suffix == ".png"
        uploaded.append(file)
        return "/uploaded/image"

    monkeypatch.setattr("gemini_webapi.client.upload_file", upload)
    settings.cookie_file.write_text('{"__Secure-1PSID":"private-cookie"}')
    backend = CookieBackend(settings, SDK)

    images = prepare_images(ChatRequest(**IMAGE_REQUEST), 0, settings)
    await backend.prepare("account-model")
    state = {}
    result = "".join([s async for s in backend.stream("inspect", "account-model", state, images)])
    assert result == "seen"
    assert generated[0]["req_file_data"] == [[["/uploaded/image"], images[0].name]]
    assert generated[0]["temporary"] is False
    assert uploaded and all(not path.exists() for path in uploaded)
    await backend.close()


@pytest.mark.parametrize("cancel", [False, True])
async def test_image_temp_files_removed_after_failure_or_cancellation(settings, cancel):
    files = []
    ready = asyncio.Event()

    class Failing(Client):
        async def generate_content_stream(self, prompt, **kwargs):
            files.extend(kwargs["files"])
            assert Path(files[0]).read_bytes() == PNG
            ready.set()
            if cancel:
                await asyncio.sleep(10)
            raise RuntimeError("private upstream failure")
            yield

    settings.cookie_file.write_text('{"__Secure-1PSID":"private-cookie"}')
    backend = CookieBackend(settings, Failing)
    await backend.prepare("account-model")
    images = prepare_images(ChatRequest(**IMAGE_REQUEST), 0, settings)

    async def consume():
        async for _ in backend.stream("inspect", "account-model", {}, images):
            pass

    task = asyncio.create_task(consume())
    await ready.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else Exception):
        await task
    assert files and all(not path.exists() for path in files)
