import pytest


@pytest.mark.parametrize("origin", ["http://localhost:5173", "http://127.0.0.1:5173"])
async def test_preflight_and_authenticated_models(make_client, origin):
    client, _, _ = make_client()
    async with client:
        preflight = await client.options(
            "/v1/models",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization,content-type",
                "Authorization": "",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == origin
        result = await client.get("/v1/models", headers={"Origin": origin})
        assert result.status_code == 200
        assert result.headers["access-control-allow-origin"] == origin
        denied = await client.get("/v1/models", headers={"Origin": origin, "Authorization": ""})
        assert denied.status_code == 401
        assert denied.headers["access-control-allow-origin"] == origin


async def test_unlisted_origin_and_post_preflight(make_client):
    client, _, _ = make_client()
    async with client:
        result = await client.options(
            "/v1/chat/completions",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        assert result.status_code == 200
        denied = await client.options(
            "/v1/models",
            headers={
                "Origin": "https://unlisted.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert denied.status_code == 400
        assert "access-control-allow-origin" not in denied.headers
