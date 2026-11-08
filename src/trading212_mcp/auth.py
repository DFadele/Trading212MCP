"""OAuth resource-server verification only; identity and consent live at the IdP."""

import asyncio
import time
from typing import Any, cast

import httpx
import jwt
from mcp.server.auth.provider import AccessToken

from trading212_mcp.market.normalization import is_list, is_mapping
from trading212_mcp.runtime_config import RuntimeSettings


class OwnerTokenVerifier:
    def __init__(
        self, settings: RuntimeSettings, client: httpx.AsyncClient | None = None
    ) -> None:
        self.settings = settings
        self.client = client or httpx.AsyncClient(
            timeout=5, follow_redirects=False, trust_env=False
        )
        self.keys: dict[str, Any] = {}
        self.expires = 0.0
        self.next_refresh = 0.0
        self.lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self.client.aclose()

    async def signing_key(self, kid: str) -> Any:
        now = time.monotonic()
        if kid in self.keys and now < self.expires:
            return self.keys[kid]
        async with self.lock:
            now = time.monotonic()
            if kid in self.keys and now < self.expires:
                return self.keys[kid]
            if now < self.next_refresh:
                return None
            self.next_refresh = now + 15  # Bound unknown-kid/rotation fetch storms.
            response = await self.client.get(self.settings.oauth_jwks_url or "")
            response.raise_for_status()
            if len(response.content) > 100_000:
                return None
            payload = response.json()
            records = payload.get("keys") if is_mapping(payload) else None
            if not is_list(records) or len(records) > 32:
                return None
            keys: dict[str, Any] = {}
            for raw in records:
                if (
                    not is_mapping(raw)
                    or raw.get("kty") != "RSA"
                    or raw.get("use", "sig") != "sig"
                    or raw.get("alg", "RS256") != "RS256"
                ):
                    continue
                key_id = raw.get("kid")
                if (
                    not isinstance(key_id, str)
                    or not 1 <= len(key_id) <= 128
                    or key_id in keys
                ):
                    return None
                key = jwt.PyJWK.from_dict(
                    cast(dict[str, Any], raw), algorithm="RS256"
                ).key
                if key.key_size < 2048:
                    continue
                keys[key_id] = key
            self.keys, self.expires = keys, time.monotonic() + 300
            return self.keys.get(kid)

    async def verify_token(self, token: str) -> AccessToken | None:
        if len(token) > 8192:
            return None
        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if (
                header.get("alg") != "RS256"
                or not isinstance(kid, str)
                or not 1 <= len(kid) <= 128
            ):
                return None
            key = await self.signing_key(kid)
            if key is None:
                return None
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self.settings.public_url,
                issuer=self.settings.oauth_issuer,
                options={"require": ["exp", "iss", "aud", "sub"]},
                leeway=5,
            )
            if claims["sub"] != self.settings.oauth_subject:
                return None
            scope = claims.get("scope", "")
            if (
                not isinstance(scope, str)
                or self.settings.oauth_scope not in scope.split()
            ):
                return None
            return AccessToken(
                token=token,
                client_id=str(claims.get("client_id") or claims.get("azp") or "oauth"),
                subject=claims["sub"],
                scopes=scope.split(),
                expires_at=int(claims["exp"]),
                resource=self.settings.public_url,
            )
        except (
            jwt.PyJWTError,
            httpx.HTTPError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
        ):
            return None
