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

    def __init__(self, api_key: str, model: str, timeout_seconds: int, base_url: str | None = None):
        if not api_key:
            raise ClientConfigurationError("LLM_API_KEY is required")
        if not model:
            raise ClientConfigurationError("LLM_MODEL is required")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.base_url = base_url or "https://api.openai.com/v1/chat/completions"

    def complete(self, system_prompt: str, task: str, canonical_input: str) -> Completion:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        # OpenRouter best practice headers (harmless for OpenAI)
        if "openrouter" in self.base_url:
            headers["HTTP-Referer"] = "https://research-analyst.local"
            headers["X-Title"] = "Research Analyst"

        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"{task}\n\n{canonical_input}"},
            ],
        }
        # Only request json_object when it is likely supported
        if "openai" in self.base_url or self.model.startswith("gpt-"):
            body["response_format"] = {"type": "json_object"}

        response = httpx.post(
            self.base_url,
            headers=headers,
            json=body,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        try:
            return Completion(payload["choices"][0]["message"]["content"], payload.get("usage") or {})
        except (IndexError, KeyError, TypeError) as error:
            raise ValueError("provider returned no structured completion") from error


def configured_client(settings) -> OpenAICompletionClient:
    provider = (settings.LLM_PROVIDER or "openai").lower()
    if provider == "openai":
        base = "https://api.openai.com/v1/chat/completions"
    elif provider in ("openrouter", "openrouter.ai"):
        base = "https://openrouter.ai/api/v1/chat/completions"
    else:
        raise ClientConfigurationError(f"unsupported LLM_PROVIDER: {provider}")
    return OpenAICompletionClient(
        settings.LLM_API_KEY, settings.LLM_MODEL, settings.LLM_TIMEOUT_SECONDS, base
    )
