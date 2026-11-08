from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TRADING212_",
        env_file=".env",
        extra="ignore",
        hide_input_in_errors=True,
    )

    api_key: SecretStr
    api_secret: SecretStr
    environment: Literal["demo", "live"] = "demo"

    @field_validator("api_key", "api_secret")
    @classmethod
    def require_credentials(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("Credential must not be blank")
        return value

    @property
    def base_url(self) -> str:
        return f"https://{self.environment}.trading212.com/api/v0/"
