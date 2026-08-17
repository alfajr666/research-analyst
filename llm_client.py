"""Provider-neutral bounded structured-completion clients."""

from __future__ import annotations

from dataclasses import dataclass

import httpx


class ClientConfigurationError(ValueError):
    """The configured provider cannot safely make a completion."""


@dataclass(frozen=True)
class Completion:
    output: str
    usage: dict


class OpenAICompletionClient:
    """Small OpenAI-compatible JSON completion adapter with no domain access."""

    def __init__(self, api_key: str, model: str, timeout_seconds: int):
        if not api_key:
            raise ClientConfigurationError("LLM_API_KEY is required")
        if not model:
            raise ClientConfigurationError("LLM_MODEL is required")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds

    def complete(self, system_prompt: str, task: str, canonical_input: str) -> Completion:
        response = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"{task}\n\n{canonical_input}"},
                ],
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        try:
            return Completion(payload["choices"][0]["message"]["content"], payload.get("usage") or {})
        except (IndexError, KeyError, TypeError) as error:
            raise ValueError("provider returned no structured completion") from error


def configured_client(settings) -> OpenAICompletionClient:
    if settings.LLM_PROVIDER != "openai":
        raise ClientConfigurationError("unsupported LLM_PROVIDER")
    return OpenAICompletionClient(settings.LLM_API_KEY, settings.LLM_MODEL, settings.LLM_TIMEOUT_SECONDS)
