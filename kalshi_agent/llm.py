"""Provider-agnostic AI client for the agent (bet parsing + exit advice).

Pick a provider by setting ONE env key — no code change, no billing wall:

  GROQ_API_KEY        Groq cloud (FREE tier) — default model openai/gpt-oss-120b
  GEMINI_API_KEY      Google AI Studio (FREE tier) — default model gemini-2.5-flash
                      (GOOGLE_API_KEY also accepted)
  CEREBRAS_API_KEY    Cerebras (FREE tier) — default model llama-3.3-70b
  OPENROUTER_API_KEY  OpenRouter — default model meta-llama/llama-3.3-70b-instruct:free
  MISTRAL_API_KEY     Mistral (free experiment tier) — default mistral-small-latest
  OPENAI_API_KEY      OpenAI — default gpt-4o-mini
  ANTHROPIC_API_KEY   Anthropic (paid) — default Haiku 4.5 (ANTHROPIC_MODEL override)

Groq, Gemini, Cerebras, OpenRouter, Mistral and OpenAI all speak the OpenAI
chat-completions shape, so one code path covers them. Anthropic uses its own
Messages API. Precedence is Groq → Gemini → Cerebras → OpenRouter → Mistral →
OpenAI → Anthropic, or force one with LLM_PROVIDER=name.

Overrides: LLM_PROVIDER, LLM_API_KEY, LLM_MODEL, LLM_BASE_URL.
No key anywhere -> AI features disabled; ticket syntax keeps working.
Errors never raise — callers get None and fall back to deterministic behavior.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import requests

log = logging.getLogger("kalshi.llm")

_PROVIDERS: dict[str, dict[str, str]] = {
    "groq": {
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "model": "openai/gpt-oss-120b",
        "key_env": "GROQ_API_KEY",
    },
    "gemini": {
        "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "model": "gemini-2.5-flash",
        "key_env": "GEMINI_API_KEY",           # GOOGLE_API_KEY also honored
    },
    "cerebras": {
        "url": "https://api.cerebras.ai/v1/chat/completions",
        "model": "llama-3.3-70b",
        "key_env": "CEREBRAS_API_KEY",
    },
    "openrouter": {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "model": "meta-llama/llama-3.3-70b-instruct:free",
        "key_env": "OPENROUTER_API_KEY",
    },
    "mistral": {
        "url": "https://api.mistral.ai/v1/chat/completions",
        "model": "mistral-small-latest",
        "key_env": "MISTRAL_API_KEY",
    },
    "openai": {
        "url": "https://api.openai.com/v1/chat/completions",
        "model": "gpt-4o-mini",
        "key_env": "OPENAI_API_KEY",
    },
}
_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_ORDER = ["groq", "gemini", "cerebras", "openrouter", "mistral", "openai", "anthropic"]


def _env_key(provider: str) -> str | None:
    if provider == "anthropic":
        return os.environ.get("ANTHROPIC_API_KEY") or None
    if provider == "gemini":
        return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or None
    return os.environ.get(_PROVIDERS[provider]["key_env"]) or None


class LLMClient:
    """Tiny chat-completions wrapper. complete() -> text or None (never raises)."""

    def __init__(self, session: Any | None = None, *, api_key: str | None = None,
                 provider: str | None = None, model: str | None = None,
                 base_url: str | None = None):
        self.session = session
        forced = (provider or os.environ.get("LLM_PROVIDER") or "auto").strip().lower()
        if api_key and forced in ("auto", ""):
            forced = "anthropic"          # back-compat: loose key means anthropic
        generic_key = api_key or os.environ.get("LLM_API_KEY") or None
        generic_base = base_url or os.environ.get("LLM_BASE_URL") or None
        generic_model = model or os.environ.get("LLM_MODEL") or None

        self.provider: str | None = None
        self.api_key: str | None = None
        self.base_url: str | None = None
        self.model: str | None = None

        if generic_key and (generic_base or forced not in ("auto", "")):
            # fully generic endpoint (any OpenAI-compatible server)
            self.provider = forced if forced not in ("auto", "") else "generic"
            self.api_key = generic_key
            self.base_url = generic_base or _PROVIDERS.get(self.provider, {}).get(
                "url", "https://api.openai.com/v1/chat/completions")
            self.model = generic_model or _PROVIDERS.get(self.provider, {}).get("model", "gpt-4o-mini")
        elif forced not in ("auto", ""):
            key = _env_key(forced)
            if key:
                self.provider = forced
                self.api_key = key
                self.base_url = _ANTHROPIC_URL if forced == "anthropic" else _PROVIDERS[forced]["url"]
                self.model = generic_model or (
                    os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5") if forced == "anthropic"
                    else _PROVIDERS[forced]["model"])
        else:
            for name in _ORDER:
                key = _env_key(name)
                if key:
                    self.provider = name
                    self.api_key = key
                    self.base_url = (_ANTHROPIC_URL if name == "anthropic" else _PROVIDERS[name]["url"])
                    self.model = generic_model or (
                        os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5") if name == "anthropic"
                        else _PROVIDERS[name]["model"])
                    break
        if generic_base and self.provider:
            self.base_url = generic_base
        if generic_model and self.provider:
            self.model = generic_model

    @property
    def configured(self) -> bool:
        return bool(self.provider and self.api_key)

    def describe(self) -> str:
        if not self.configured:
            return ("AI not configured — set a FREE key: GROQ_API_KEY (console.groq.com) "
                    "or GEMINI_API_KEY (aistudio.google.com) — or ANTHROPIC_API_KEY (paid)")
        return f"AI via {self.provider} · {self.model}"

    # ------------------------------------------------------------------ api --
    def complete(self, *, system: str, user: str, max_tokens: int = 400) -> str | None:
        if not self.configured:
            log.warning("LLM not configured — call skipped")
            return None
        http = self.session or requests
        try:
            if self.provider == "anthropic":
                resp = http.post(self.base_url, timeout=45, headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                }, json={
                    "model": self.model,
                    "max_tokens": max_tokens,
                    "system": system,
                    "messages": [{"role": "user", "content": user}],
                })
            else:  # OpenAI chat completions (groq/gemini/cerebras/openrouter/mistral/openai)
                body = {
                    "model": self.model,
                    "max_tokens": max_tokens,
                    "temperature": 0.2,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                }
                if self.provider in ("groq", "gemini"):
                    # both guarantee valid-JSON content with this flag
                    body["response_format"] = {"type": "json_object"}
                resp = http.post(self.base_url, timeout=45, headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "content-type": "application/json",
                }, json=body)
        except Exception as exc:  # noqa: BLE001
            log.error("LLM call to %s failed: %s", self.provider, exc)
            return None
        return self._extract_text(resp)

    def _extract_text(self, resp) -> str | None:
        status = getattr(resp, "status_code", 0)
        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001
            log.error("LLM %s HTTP %s: unreadable body", self.provider, status)
            return None
        if status != 200:
            detail = ""
            try:
                detail = (payload.get("error", {}) or {}).get("message", "") if isinstance(payload, dict) else str(payload)
                detail = detail or str(payload)[:200]
            except Exception:  # noqa: BLE001
                detail = str(payload)[:200]
            log.error("LLM %s HTTP %s: %s", self.provider, status, detail)
            return None
        try:
            if self.provider == "anthropic":
                return payload["content"][0]["text"]
            return payload["choices"][0]["message"]["content"]
        except Exception:  # noqa: BLE001
            log.error("LLM %s returned an unexpected shape", self.provider)
            return None


def get_client(session: Any | None = None) -> LLMClient:
    return LLMClient(session=session)
