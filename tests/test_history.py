from types import SimpleNamespace

import pytest

from gemini_proxy.backends.cookie import CookieBackend

from .test_cookie import Client


class HistoryClient(Client):
    listed = True
    readable = True
    fail = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.refreshed = False

    async def _fetch_recent_chats(self, recent):
        assert recent == 50
        if self.fail:
            raise RuntimeError("private upstream payload")
        self.refreshed = True

    def list_chats(self):
        assert self.refreshed  # Require a fresh server read, not the local generation cache.
        return [SimpleNamespace(cid="c_abc123")] if self.listed else []

    async def read_chat(self, cid, limit):
        assert cid == "c_abc123" and limit == 1
        if not self.readable:
            return None
        return SimpleNamespace(
            cid=cid,
            turns=[
                SimpleNamespace(
                    model_output=SimpleNamespace(metadata=[cid, "reply1"]),
                )
            ],
        )


@pytest.mark.parametrize(
    "listed,readable,fail",
    [
        (True, True, False),
        (False, True, False),
        (False, False, False),
        (False, False, True),
    ],
)
async def test_diagnostics_check_google_without_sending_generation(
    make_client, settings, listed, readable, fail
):
    class Configured(HistoryClient):
        pass

    Configured.listed, Configured.readable, Configured.fail = listed, readable, fail
    settings.cookie_file.write_text('{"__Secure-1PSID":"private-cookie"}')
    backend = CookieBackend(settings, Configured)
    http, service, _ = make_client(backend)
    service.conversations.save(
        "my-thread",
        {
            "backend": "cookie",
            "model": "account-model",
            "valid": True,
            "hashes": ["opaque"],
            "state": {"metadata": ["c_abc123", "reply1", "candidate1"]},
        },
    )
    async with http:
        listing = await http.get("/v1/conversations")
        assert listing.json()["data"] == [
            {"id": "my-thread", "backend": "cookie", "local_state": "completed"},
        ]
        result = await http.get("/v1/conversations/my-thread")
        assert result.status_code == 200
        info = result.json()
        assert info["temporary"] is False
        assert info["gemini_chat_url"] == "https://gemini.google.com/app/abc123"
        assert info["listed_in_recent_history"] == (None if fail else listed)
        assert info["latest_turn_readable"] == (True if readable and not fail else None)
        assert "private" not in result.text and "candidate1" not in result.text
        assert not backend.client.requests
        denied = await http.get("/v1/conversations/my-thread", headers={"Authorization": ""})
        assert denied.status_code == 401
        missing = await http.get("/v1/conversations/missing")
        assert missing.status_code == 404
    await backend.close()


async def test_old_turn_is_not_claimed_as_latest_saved_turn(settings):
    settings.cookie_file.write_text('{"__Secure-1PSID":"private-cookie"}')
    backend = CookieBackend(settings, HistoryClient)
    status = await backend.history_status({"metadata": ["c_abc123", "new-reply", "rc"]})
    assert status["listed_in_recent_history"] is True
    assert status["latest_turn_readable"] is None
    await backend.close()


async def test_live_endpoint_identifies_available_capabilities(make_client):
    http, _, _ = make_client()
    async with http:
        result = await http.get("/health/live", headers={"Authorization": ""})
    assert "history_diagnostics" in result.json()["capabilities"]
