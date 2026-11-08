"""Transport settings, separate from account secrets and scientific settings."""

import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing_extensions import Self


class RuntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MCP_",
        extra="ignore",
        hide_input_in_errors=True,
        populate_by_name=True,
    )
    transport: Literal["stdio", "streamable-http"] = "stdio"
    host: str = "127.0.0.1"
    port: int = Field(
        default=8000, ge=1, le=65535, validation_alias=AliasChoices("MCP_PORT", "PORT")
    )
    auth_mode: Literal["oauth", "local"] = "oauth"
    public_url: str | None = None
    oauth_issuer: str | None = None
    oauth_jwks_url: str | None = None
    oauth_subject: str | None = None
    oauth_scope: str = Field(default="portfolio:read", min_length=1, max_length=100)
    allowed_hosts: list[str] = Field(default_factory=list)
    allowed_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:6274", "http://127.0.0.1:6274"]
    )
    shutdown_seconds: int = Field(default=30, ge=1, le=120)

    @model_validator(mode="after")
    def secure_http(self) -> Self:
        if self.transport == "stdio":
            return self
        if any(
            not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", host)
            for host in self.allowed_hosts
        ):
            raise ValueError(
                "Allowed hosts must be explicit hostnames without scheme/port/wildcards."
            )
        if any(
            "*" in origin
            or urlsplit(origin).scheme not in ("http", "https")
            or not urlsplit(origin).netloc
            or urlsplit(origin).path not in ("", "/")
            or urlsplit(origin).username is not None
            or urlsplit(origin).query
            or urlsplit(origin).fragment
            for origin in self.allowed_origins
        ):
            raise ValueError("Allowed origins must be explicit HTTP(S) origins.")
        if self.auth_mode == "local":
            if self.host not in ("127.0.0.1", "localhost"):
                raise ValueError("Local HTTP mode must bind to loopback.")
        else:
            for name in ("public_url", "oauth_issuer", "oauth_jwks_url"):
                value = getattr(self, name)
                parsed = urlsplit(value or "")
                if (
                    parsed.scheme != "https"
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                    or parsed.query
                    or parsed.fragment
                ):
                    raise ValueError(
                        f"{name} must be an explicit HTTPS URL without credentials/query/fragment."
                    )
            if urlsplit(self.public_url or "").path != "/mcp":
                raise ValueError(
                    "public_url must end at /mcp without a trailing slash."
                )
            if not self.oauth_subject or not self.oauth_subject.strip():
                raise ValueError("Configure the single allowed OAuth subject.")
        return self
