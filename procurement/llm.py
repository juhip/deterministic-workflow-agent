"""The model slot: how the agent talks to an open model.

Everything model-specific lives in this file and in config/model.json.

Why this shape: almost every way of serving an open model speaks the same
"/v1/chat/completions" protocol, an open wire format implemented by open-source servers
(vLLM, SGLang, TGI, Ollama, llama.cpp, LM Studio) and by hosted open-model providers.
A self-hosted open model is served the same way, so switching to it is a config change:
pick the "self-hosted" profile and set its address and model name.

Standard library only: no vendor SDK and no orchestration framework, so nothing here is
locked to one model provider. Callers depend only on the small ModelProvider interface,
so a different transport (for example an in-process Hugging Face model) could be added as
another class without touching the rest of the agent.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config" / "model.json"


class LLMError(RuntimeError):
    pass


class ModelProvider(Protocol):
    """What the rest of the agent needs from a model: one chat call, plus names for messages."""
    model_name: str
    endpoint: str

    def chat(self, messages: list[dict], json_mode: bool = True) -> str: ...


@dataclass
class LLMConfig:
    base_url: str          # e.g. http://localhost:8000/v1 (vLLM)
    model: str             # the model's name as the server knows it
    api_key: str = "not-needed"
    timeout_s: float = 300.0
    max_tokens: int = 6000   # generous: "thinking" models spend tokens reasoning before they answer
    profile: str = ""

    @classmethod
    def from_env(cls) -> "LLMConfig | None":
        """LLM_BASE_URL + LLM_MODEL (+ optional LLM_API_KEY). None if not configured."""
        url, model = os.environ.get("LLM_BASE_URL"), os.environ.get("LLM_MODEL")
        if not (url and model):
            return None
        return cls(url, model, os.environ.get("LLM_API_KEY", "not-needed"),
                   max_tokens=int(os.environ.get("LLM_MAX_TOKENS", 6000)), profile="environment")

    @classmethod
    def from_profile(cls, name: str, path: str | Path = DEFAULT_CONFIG) -> "LLMConfig | None":
        """A named profile from config/model.json. 'none' (or empty) means: run without a model."""
        if not name or name == "none":
            return None
        profiles = json.loads(Path(path).read_text()).get("profiles", {})
        if name not in profiles:
            raise LLMError(f"unknown model profile {name!r}; choose one of: {', '.join(sorted(profiles))}")
        p = profiles[name]
        key = os.environ.get(p.get("api_key_env", ""), "") or "not-needed"
        return cls(p["base_url"], p["model"], key, profile=name,
                   max_tokens=int(p.get("max_tokens", 6000)), timeout_s=float(p.get("timeout_s", 300)))


def resolve_config(base_url: str | None = None, model: str | None = None, api_key: str | None = None,
                   profile: str | None = None, config_path: str | Path = DEFAULT_CONFIG) -> LLMConfig | None:
    """Which model to use, most specific first:
    1. --llm-base-url and --llm-model on the command line
    2. LLM_BASE_URL and LLM_MODEL environment variables
    3. --llm-profile, or the LLM_PROFILE environment variable
    4. active_profile in config/model.json ("none" by default: no model)"""
    if base_url and model:
        return LLMConfig(base_url, model, api_key or "not-needed", profile="command line")
    env = LLMConfig.from_env()
    if env:
        return env
    chosen = profile or os.environ.get("LLM_PROFILE")
    if not chosen and Path(config_path).exists():
        chosen = json.loads(Path(config_path).read_text()).get("active_profile")
    return LLMConfig.from_profile(chosen or "none", config_path)


class OpenAICompatibleClient:
    """ModelProvider for any server that speaks the OpenAI-compatible chat API."""

    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.model_name = cfg.model
        self.endpoint = cfg.base_url
        self.url = cfg.base_url.rstrip("/") + "/chat/completions"

    def _post(self, body: dict) -> dict:
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.cfg.api_key}"})
        with urllib.request.urlopen(req, timeout=self.cfg.timeout_s) as resp:
            return json.load(resp)

    def chat(self, messages: list[dict], json_mode: bool = True) -> str:
        body = {"model": self.cfg.model, "messages": messages, "temperature": 0, "max_tokens": self.cfg.max_tokens}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            try:
                data = self._post(body)
            except urllib.error.HTTPError as e:
                if json_mode and e.code in (400, 422):   # some servers don't support JSON mode; ask without it
                    body.pop("response_format")
                    data = self._post(body)
                else:
                    raise
            choice = data["choices"][0]
            content = choice["message"].get("content") or ""
            if not content.strip():
                why = ("it ran out of tokens before answering (finish_reason=length); raise LLM_MAX_TOKENS"
                       if choice.get("finish_reason") == "length" else "it returned an empty answer")
                raise LLMError(f"model gave no usable reply: {why}")
            return content
        except (urllib.error.URLError, TimeoutError, OSError, KeyError, IndexError, ValueError) as e:
            raise LLMError(f"{type(e).__name__}: {e}") from e


def get_provider(cfg: LLMConfig) -> ModelProvider:
    """The one place that turns a config into a client. Add other transports here."""
    return OpenAICompatibleClient(cfg)
