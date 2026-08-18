"""Discord webhook transport boundary (testable offline)."""

from __future__ import annotations

import httpx

from discord_format import DISCORD_CONTENT_LIMIT


class DiscordWebhookTransport:
    """POST markdown content to a Discord incoming webhook."""

    def __init__(self, webhook_url: str, timeout: float = 15.0):
        self.webhook_url = webhook_url
        self.timeout = timeout

    def send(self, text: str) -> str:
        if not self.webhook_url:
            raise RuntimeError("Discord webhook URL is not configured")
        content = text[:DISCORD_CONTENT_LIMIT]
        response = httpx.post(
            self.webhook_url,
            json={"content": content},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.text or f"discord_http_{response.status_code}"
