"""Real loopback HTTP smoke check with synthetic credentials and a local test JWKS.

No real account, provider, cloud or identity-provider requests are made.
"""

import asyncio
import json
import os
import signal
import socket
import sys
import tempfile
import time
from pathlib import Path

import httpx
import jwt
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from trading212_mcp.auth import OwnerTokenVerifier
from trading212_mcp.config import Settings
from trading212_mcp.http import create_http_app
from trading212_mcp.observability import configure_logging
from trading212_mcp.runtime_config import RuntimeSettings


async def main() -> None:
    configure_logging()
    with tempfile.TemporaryDirectory(prefix="trading212-http-smoke-") as directory:
        # Storage is isolated; no operator credentials or persistence are loaded.
        os.environ["MARKET_RESOLUTION_CACHE_PATH"] = str(
            Path(directory) / "ids.sqlite3"
        )
        config = RuntimeSettings(
            transport="streamable-http",
            host="127.0.0.1",
            auth_mode="oauth",
            public_url="https://mcp.example.test/mcp",
            oauth_issuer="https://issuer.example.test/",
            oauth_jwks_url="https://issuer.example.test/jwks",
            oauth_subject="synthetic-owner",
        )
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
        jwk.update(kid="synthetic", use="sig", alg="RS256")
        verifier = OwnerTokenVerifier(
            config,
            httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda req: httpx.Response(200, json={"keys": [jwk]})
                )
            ),
        )
        app = create_http_app(
            Settings(api_key="synthetic", api_secret="synthetic", _env_file=None),  # pyright: ignore[reportCallIssue]
            config,
            verifier=verifier,
        )
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                access_log=False,
                log_config=None,
                proxy_headers=False,
                timeout_graceful_shutdown=5,
            )
        )
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            for _ in range(500):
                if server.started:
                    break
                if task.done():
                    await task
                    raise RuntimeError("HTTP startup failed")
                await asyncio.sleep(0.01)
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False
            ) as client:
                assert (await client.get("/readyz")).status_code == 200
                assert (await client.post("/mcp", json={})).status_code == 401
                token = jwt.encode(
                    {
                        "iss": config.oauth_issuer,
                        "aud": config.public_url,
                        "sub": config.oauth_subject,
                        "scope": config.oauth_scope,
                        "exp": int(time.time()) + 60,
                    },
                    key,
                    algorithm="RS256",
                    headers={"kid": "synthetic"},
                )
                client.headers["Authorization"] = "Bearer " + token
                async with streamable_http_client(
                    f"http://127.0.0.1:{port}/mcp", http_client=client
                ) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        assert len(tools.tools) == 12
                        assert all(
                            t.annotations and t.annotations.readOnlyHint
                            for t in tools.tools
                        )
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=10)
            sock.close()
        assert app.state.runtime.client is None
        # Exercise the actual CLI's SIGTERM path separately from the in-process server.
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        child_port = probe.getsockname()[1]
        probe.close()
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": directory,
            "TRADING212_API_KEY": "synthetic",
            "TRADING212_API_SECRET": "synthetic",
            "TRADING212_ENVIRONMENT": "demo",
            "MCP_AUTH_MODE": "local",
            "MARKET_RESOLUTION_CACHE_PATH": str(Path(directory) / "child-ids.sqlite3"),
        }
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "trading212_mcp",
            "--transport",
            "streamable-http",
            "--host",
            "127.0.0.1",
            "--port",
            str(child_port),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
                for _ in range(200):
                    try:
                        if (
                            await client.get(f"http://127.0.0.1:{child_port}/readyz")
                        ).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.05)
                else:
                    raise RuntimeError("CLI readiness failed")
            child.terminate()
            _, logs = await asyncio.wait_for(child.communicate(), timeout=10)
            assert child.returncode in (0, -signal.SIGTERM)
            assert b'"event":"runtime_stopped"' in logs
        finally:
            if child.returncode is None:
                child.kill()
                await child.communicate()
        print(
            "HTTP smoke passed: readiness, unauthenticated 401, authenticated MCP discovery (12 tools), graceful shutdown and CLI SIGTERM cleanup."
        )


if __name__ == "__main__":
    asyncio.run(main())
