"""Streamable HTTP boundary. Only metadata and process probes are public."""

import os
import secrets
import tempfile
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from trading212_mcp.auth import OwnerTokenVerifier
from trading212_mcp.config import Settings
from trading212_mcp.market.config import MarketSettings
from trading212_mcp.observability import logger, request_id
from trading212_mcp.runtime_config import RuntimeSettings
from trading212_mcp.server import ServerRuntime, create_server


class HttpTelemetry:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token = request_id.set(secrets.token_hex(8))
        start, size, status = time.perf_counter(), 0, 500

        async def measured(message: Message) -> None:
            nonlocal size, status
            if message["type"] == "http.response.start":
                status = message["status"]
            if message["type"] == "http.response.body":
                size += len(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, receive, measured)
        finally:
            logger.info(
                "",
                extra={
                    "event": "http_request",
                    "request_id": request_id.get(),
                    "duration_ms": round((time.perf_counter() - start) * 1000, 3),
                    "status_code": status,
                    "response_bytes": size,
                },
            )
            request_id.reset(token)


def create_http_app(
    settings: Settings,
    config: RuntimeSettings,
    *,
    runtime: ServerRuntime | None = None,
    verifier: OwnerTokenVerifier | None = None,
) -> Starlette:
    if config.transport != "streamable-http":
        raise ValueError(
            "HTTP application requires streamable-http transport configuration."
        )
    runtime = runtime or ServerRuntime(settings)
    verifier = (
        (verifier or OwnerTokenVerifier(config))
        if config.auth_mode == "oauth"
        else None
    )
    auth = (
        AuthSettings(
            issuer_url=AnyHttpUrl(config.oauth_issuer or ""),
            resource_server_url=AnyHttpUrl(config.public_url or ""),
            required_scopes=[config.oauth_scope],
            validate_token_resource=True,
        )
        if verifier
        else None
    )
    hostname = urlsplit(config.public_url or "").hostname
    hosts = (
        ["127.0.0.1", "localhost"]
        + ([hostname] if hostname else [])
        + config.allowed_hosts
    )
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[v for h in hosts for v in (h, h + ":*")],
        allowed_origins=config.allowed_origins,
    )
    server = create_server(
        settings,
        runtime,
        auth=auth,
        token_verifier=verifier,
        transport_security=security,
    )

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncGenerator[None, None]:
        app.state.ready = False
        try:
            runtime.market_settings = MarketSettings(_env_file=None)  # pyright: ignore[reportCallIssue]
            for path in (
                runtime.market_settings.resolution_cache_path,
                runtime.market_settings.cache_path,
            ):
                if path is not None:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if path.exists() and (
                        not path.is_file() or not os.access(path, os.W_OK)
                    ):
                        raise RuntimeError("Configured public cache is not writable")
                    with tempfile.TemporaryFile(dir=path.parent):
                        pass
            await runtime.start()
            async with server.session_manager.run():
                app.state.ready = True
                yield
        finally:
            app.state.ready = False
            try:
                await runtime.close()
            finally:
                if verifier:
                    await verifier.aclose()

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})

    async def ready(request: Request) -> JSONResponse:
        running = getattr(request.app.state, "ready", False)
        return JSONResponse(
            {"status": "ready" if running else "not_ready"},
            status_code=200 if running else 503,
            headers={"Cache-Control": "no-store"},
        )

    mcp_app = server.streamable_http_app()
    mcp_app.router.redirect_slashes = False
    app = Starlette(
        routes=[
            Route("/healthz", health),
            Route("/readyz", ready),
            Mount("/", app=mcp_app),
        ],
        lifespan=lifespan,
        middleware=[
            Middleware(HttpTelemetry),
            Middleware(TrustedHostMiddleware, allowed_hosts=hosts),
            Middleware(
                CORSMiddleware,
                allow_origins=config.allowed_origins,
                allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
                allow_headers=[
                    "Authorization",
                    "Content-Type",
                    "Accept",
                    "MCP-Protocol-Version",
                    "Mcp-Session-Id",
                    "Last-Event-ID",
                ],
                expose_headers=["Mcp-Session-Id", "WWW-Authenticate"],
            ),
        ],
    )
    app.router.redirect_slashes = False
    app.state.server, app.state.runtime = server, runtime
    app.state.ready = False
    return app
