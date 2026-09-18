"""Provider adapters for the independent LLM thesis review.

The review module owns all policy; adapters only translate one prompt into one
raw model string. No adapter knows about candidates, scores, or publication.
``NullThesisReviewer`` always fails so tests can exercise the fail-open path
without network access; production selects the configured HTTP provider.
"""
from __future__ import annotations

import httpx


class NullThesisReviewer:
    """Deterministic unavailable provider: every call fails closed-fast."""

    model_version = "null-provider-v1"

    def complete(self, prompt: str, *, timeout_seconds: float) -> str:
        raise RuntimeError("thesis review provider is not configured")


class ZaiThesisReviewer:
    """Minimal chat-completions adapter for the configured Z.ai endpoint."""

    model_version: str

    def __init__(self, *, api_key: str, model: str, base_url: str = "",
                 transport: httpx.BaseTransport | None = None):
        self.api_key = api_key
        self.model = model
        self.base_url = (base_url or "https://api.z.ai/api/paas/v4").rstrip("/")
        self.model_version = f"zai:{model}" if model else "zai:unconfigured"
        self._transport = transport

    def complete(self, prompt: str, *, timeout_seconds: float) -> str:
        if not self.api_key or not self.model:
            raise RuntimeError("thesis review provider is not configured")
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "thinking": {"type": "disabled"},
        }
        with httpx.Client(timeout=timeout_seconds, transport=self._transport) as client:
            response = client.post(url, headers=headers, json=body)
            if response.status_code == 429:
                raise RuntimeError("thesis review provider rate limit (429)")
            response.raise_for_status()
            payload = response.json()
        choices = payload.get("choices") if isinstance(payload, dict) else None
        if not choices or not isinstance(choices[0], dict):
            raise RuntimeError("thesis review provider returned no choices")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("thesis review provider returned empty content")
        return content
