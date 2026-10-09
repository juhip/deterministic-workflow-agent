"""Where the open model plugs in: config profiles and precedence."""
import json

import pytest

from procurement.llm import LLMError, OpenAICompatibleClient, get_provider, resolve_config, DEFAULT_CONFIG


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "LLM_PROFILE", "OPEN_MODEL_API_KEY"):
        monkeypatch.delenv(k, raising=False)


def test_default_is_no_model():
    assert json.loads(DEFAULT_CONFIG.read_text())["active_profile"] == "none"
    assert resolve_config() is None


def test_self_hosted_profile_ready(monkeypatch):
    monkeypatch.setenv("OPEN_MODEL_API_KEY", "secret")
    cfg = resolve_config(profile="self-hosted")
    assert cfg.profile == "self-hosted" and cfg.base_url.endswith("/v1") and cfg.api_key == "secret"
    client = get_provider(cfg)
    assert isinstance(client, OpenAICompatibleClient) and client.url.endswith("/v1/chat/completions")


@pytest.mark.parametrize("name", ["vllm", "sglang", "tgi", "ollama", "llamacpp", "lmstudio", "hosted-open-model"])
def test_every_listed_server_has_a_working_profile(name):
    cfg = resolve_config(profile=name)
    assert cfg.base_url.startswith("http") and cfg.model


def test_precedence_cli_then_env_then_profile(monkeypatch):
    monkeypatch.setenv("LLM_PROFILE", "ollama")
    assert resolve_config().profile == "ollama"
    monkeypatch.setenv("LLM_BASE_URL", "http://env:1/v1")
    monkeypatch.setenv("LLM_MODEL", "env-model")
    assert resolve_config().model == "env-model"
    assert resolve_config("http://cli:2/v1", "cli-model").model == "cli-model"


def test_unknown_profile_is_a_clear_error():
    with pytest.raises(LLMError, match="unknown model profile"):
        resolve_config(profile="no-such-model")


def test_no_vendor_sdk_or_framework_imported():
    """The tech requirement: nothing locked to one provider. Only the standard library is used."""
    import pathlib
    banned = ("openai", "anthropic", "langchain", "llama_index", "crewai", "autogen", "google.generativeai", "boto3")
    for f in pathlib.Path(DEFAULT_CONFIG).parents[1].joinpath("procurement").glob("*.py"):
        for line in f.read_text().splitlines():
            if line.startswith(("import ", "from ")):
                assert not any(line.split()[1].startswith(b) for b in banned), f"{f.name}: {line}"
