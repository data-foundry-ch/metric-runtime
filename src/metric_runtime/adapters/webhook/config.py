"""``type: webhook`` connection config (``notifier`` only)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator

__all__ = ["WebhookConnectionConfig"]


class WebhookConnectionConfig(BaseModel):
    """HTTP(S) JSON webhook for outbox delivery (``notifier`` role)."""

    type: Literal["webhook"] = "webhook"
    url: SecretStr
    secret: SecretStr | None = None
    headers: dict[str, SecretStr] = Field(default_factory=dict)
    timeout: float = Field(default=10.0, gt=0)

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: SecretStr) -> SecretStr:
        from urllib.parse import urlsplit

        parts = urlsplit(value.get_secret_value())
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("webhook url must be an absolute http(s) URL")
        return value
