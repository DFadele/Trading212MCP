import asyncio
import json
import logging
import time
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import ValidationError

from trading212_mcp.auth import OwnerTokenVerifier
from trading212_mcp.config import Settings
from trading212_mcp.http import create_http_app
from trading212_mcp.observability import PrivateFormatter, span
from trading212_mcp.runtime_config import RuntimeSettings
from trading212_mcp.server import ServerRuntime


@pytest.fixture
def oauth(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKET_RESOLUTION_CACHE_PATH", str(tmp_path / "ids.sqlite3"))
    return RuntimeSettings(
        transport="streamable-http",
        public_url="https://mcp.example.test/mcp",
        oauth_issuer="https://id.example.test/",
        oauth_jwks_url="https://id.example.test/jwks",
        oauth_subject="owner-immutable-id",
    )


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def verifier(oauth, rsa_key):
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(rsa_key.public_key()))
    public.update(kid="test-key", alg="RS256", use="sig")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"keys": [public]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OwnerTokenVerifier(oauth, client), calls


def access_token(key, config, **overrides):
    claims = {
        "iss": config.oauth_issuer,
        "aud": config.public_url,
        "sub": config.oauth_subject,
        "scope": config.oauth_scope,
        "exp": int(time.time()) + 300,
    }
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-key"})


@pytest.mark.parametrize(
    "override", [{}, {"auth_mode": "local", "host": "0.0.0.0"}, {"auth_mode": "none"}]
)
def test_http_rejects_insecure_or_incomplete_config(override):
    with pytest.raises(ValidationError):
        RuntimeSettings(transport="streamable-http", **override)


@pytest.mark.parametrize(
    "field,value",
    [
        ("public_url", "http://mcp.example.test/mcp"),
        ("public_url", "https://mcp.example.test/mcp/"),
        ("oauth_issuer", "https://secret@id.example.test/"),
        ("allowed_origins", ["*"]),
        ("oauth_subject", ""),
    ],
)
def test_oauth_security_settings_reject_unsafe_values(oauth, field, value):
    with pytest.raises(ValidationError):
        RuntimeSettings(**{**oauth.model_dump(), field: value})


def test_stdio_still_default_and_managed_port_supported(monkeypatch):
    assert RuntimeSettings().transport == "stdio"
    monkeypatch.setenv("PORT", "10000")
    monkeypatch.delenv("MCP_PORT", raising=False)
    assert RuntimeSettings().port == 10000
    monkeypatch.setenv("MCP_PORT", "9000")
    assert RuntimeSettings().port == 9000


async def test_verifier_validates_and_caches_real_signature(verifier, oauth, rsa_key):
    auth, calls = verifier
    token = access_token(rsa_key, oauth)
    results = await asyncio.gather(*(auth.verify_token(token) for _ in range(8)))
    assert all(
        r and r.subject == oauth.oauth_subject and r.resource == oauth.public_url
        for r in results
    )
    assert len(calls) == 1
    await auth.aclose()


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "another-resource"},
        {"iss": "https://evil.test/"},
        {"sub": "another-user"},
        {"scope": "unrelated"},
        {"exp": int(time.time()) - 60},
        {"nbf": int(time.time()) + 600},
    ],
)
async def test_verifier_rejects_wrong_identity_audience_scope_and_time(
    verifier, oauth, rsa_key, overrides
):
    auth, _ = verifier
    assert await auth.verify_token(access_token(rsa_key, oauth, **overrides)) is None
    await auth.aclose()


async def test_verifier_rejects_forgery_none_algorithm_and_missing_expiry(
    verifier, oauth, rsa_key
):
    auth, _ = verifier
    forged = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert await auth.verify_token(access_token(forged, oauth)) is None
    assert (
        await auth.verify_token(
            jwt.encode(
                {"sub": oauth.oauth_subject},
                key="",
                algorithm="none",
                headers={"kid": "test-key"},
            )
        )
        is None
    )
    assert (
        await auth.verify_token(
            jwt.encode(
                {
                    "iss": oauth.oauth_issuer,
                    "aud": oauth.public_url,
                    "sub": oauth.oauth_subject,
                },
                rsa_key,
                algorithm="RS256",
                headers={"kid": "test-key"},
            )
        )
        is None
    )
    assert await auth.verify_token("broken") is None
    await auth.aclose()


async def test_jwks_failure_and_unknown_key_fetch_storm_fail_closed(oauth, rsa_key):
    calls = []

    def outage(request):
        calls.append(request)
        return httpx.Response(503)

    auth = OwnerTokenVerifier(
        oauth, httpx.AsyncClient(transport=httpx.MockTransport(outage))
    )
    for _ in range(5):
        assert await auth.verify_token(access_token(rsa_key, oauth)) is None
    assert len(calls) == 1
    await auth.aclose()


async def test_unauthenticated_requests_cannot_initialize_or_call_portfolio(
    oauth, verifier
):
    auth, _ = verifier
    settings = Settings(
        api_key="fixture-key", api_secret="fixture-secret", _env_file=None
    )
    runtime = ServerRuntime(settings)
    app = create_http_app(settings, oauth, runtime=runtime, verifier=auth)
    async with app.router.lifespan_context(app):
        runtime.client.get_account_summary = AsyncMock(
            side_effect=AssertionError("Must not reach account")
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=oauth.public_url.removesuffix("/mcp"),
        ) as client:
            for method in ("GET", "POST", "DELETE"):
                response = await client.request(
                    method,
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "get_account_summary", "arguments": {}},
                    },
                )
                assert response.status_code == 401
                assert "resource_metadata=" in response.headers["www-authenticate"]
                assert "fixture-secret" not in response.text
            metadata = await client.get("/.well-known/oauth-protected-resource/mcp")
            assert metadata.status_code == 200
            assert metadata.json()["authorization_servers"] == [oauth.oauth_issuer]
            assert (await client.get("/healthz")).json() == {"status": "ok"}
            assert (await client.get("/readyz")).json() == {"status": "ready"}
            runtime.client.get_account_summary.assert_not_awaited()
    assert runtime.client is None and not app.state.ready


async def test_authenticated_mcp_client_discovers_twelve_tools_and_preserves_response(
    oauth, verifier, rsa_key, trading, caplog
):
    auth, _ = verifier
    settings = Settings(
        api_key="fixture-key", api_secret="fixture-secret", _env_file=None
    )
    runtime = ServerRuntime(settings)
    app = create_http_app(settings, oauth, runtime=runtime, verifier=auth)
    with caplog.at_level(logging.INFO, logger="trading212.telemetry"):
        async with app.router.lifespan_context(app):
            original = runtime.client
            runtime.client.get_account_summary = trading.get_account_summary
            headers = {"Authorization": "Bearer " + access_token(rsa_key, oauth)}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), headers=headers
            ) as client:
                for _ in range(2):
                    async with streamable_http_client(
                        oauth.public_url, http_client=client
                    ) as (read, write, _):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            catalog = await session.list_tools()
                            assert len(catalog.tools) == 12
                            assert all(
                                t.annotations.readOnlyHint for t in catalog.tools
                            )
                            result = await session.call_tool("get_account_summary", {})
                            assert not result.isError
                            assert (
                                result.structuredContent["account"]["totalValue"]
                                == "1100"
                            )
                    assert runtime.client is original
    assert runtime.client is None
    assert any(
        r.__dict__.get("stage") == "response_serialization" for r in caplog.records
    )


async def test_local_mode_readiness_origin_and_host_guards(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKET_RESOLUTION_CACHE_PATH", str(tmp_path / "ids.sqlite3"))
    settings = Settings(api_key="fixture", api_secret="fixture", _env_file=None)
    app = create_http_app(
        settings, RuntimeSettings(transport="streamable-http", auth_mode="local")
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
    ) as client:
        assert (await client.get("/readyz")).status_code == 503
        async with app.router.lifespan_context(app):
            assert (await client.get("/readyz")).status_code == 200
            assert (
                await client.post(
                    "/mcp", json={}, headers={"Origin": "https://evil.test"}
                )
            ).status_code == 403
            assert (
                await client.post("/mcp", headers={"Host": "evil.test"})
            ).status_code == 400


def test_logs_cannot_expose_arguments_headers_tokens_errors_or_amounts(caplog):
    formatter = PrivateFormatter()
    record = logging.LogRecord(
        "httpx",
        logging.ERROR,
        "",
        1,
        "Authorization: Bearer SECRET; account=5000; URL?apikey=SECRET",
        (),
        None,
    )
    assert "SECRET" not in formatter.format(record)
    assert "5000" not in formatter.format(record)
    with caplog.at_level(logging.INFO):
        with pytest.raises(ValueError), span("quantitative_calculation", "test"):
            raise ValueError("sensitive account details")
    rendered = formatter.format(caplog.records[-1])
    assert "sensitive" not in rendered
    assert json.loads(rendered)["outcome"] == "error_or_cancelled"


async def test_expired_jwks_cache_is_not_used_during_outage(oauth, rsa_key):
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(rsa_key.public_key()))
    public.update(kid="test-key", alg="RS256")
    status = [200]
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(status[0], json={"keys": [public]})
        )
    )
    auth = OwnerTokenVerifier(oauth, client)
    token = access_token(rsa_key, oauth)
    assert await auth.verify_token(token)
    status[0] = 503
    auth.expires = time.monotonic() - 1
    auth.next_refresh = time.monotonic() - 1
    assert await auth.verify_token(token) is None
    await auth.aclose()


async def test_unwritable_or_invalid_storage_prevents_readiness(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKET_RESOLUTION_CACHE_PATH", str(tmp_path))
    settings = Settings(api_key="fixture", api_secret="fixture", _env_file=None)
    app = create_http_app(
        settings, RuntimeSettings(transport="streamable-http", auth_mode="local")
    )
    with pytest.raises(RuntimeError, match="cache"):
        async with app.router.lifespan_context(app):
            pytest.fail("Invalid cache path accepted")
    assert not app.state.ready and app.state.runtime.client is None


async def test_noncanonical_path_never_redirects_bearer_to_plain_http(oauth, verifier):
    auth, _ = verifier
    app = create_http_app(
        Settings(api_key="fixture", api_secret="fixture", _env_file=None),
        oauth,
        verifier=auth,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://mcp.example.test"
    ) as client:
        response = await client.post("/mcp/", json={})
        assert response.status_code == 404
        assert "location" not in response.headers
    await auth.aclose()
