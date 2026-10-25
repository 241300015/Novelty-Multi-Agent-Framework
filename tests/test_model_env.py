from backend.env import ModelRuntimeConfig


def test_default_runtime_is_local_and_never_borrows_private_key(monkeypatch):
    for prefix in ("NOVELTY", "LLM"):
        for name in ("MODEL", "BASE_URL", "API_KEY"):
            monkeypatch.delenv(f"{prefix}_{name}", raising=False)
    monkeypatch.setenv("LOCAL_VLLM_API_KEY", "local-test")
    monkeypatch.setenv("NOVELTY_API_KEY", "private-test")
    config = ModelRuntimeConfig.from_env()
    assert config.model == "qwen2.5-7b-instruct"
    assert config.base_url == "http://127.0.0.1:8000/v1"
    assert config.api_key == "local-test"


def test_model_runtime_config_prefers_project_env(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "shared-model")
    monkeypatch.setenv("NOVELTY_MODEL", "novelty-model")
    monkeypatch.setenv("NOVELTY_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("NOVELTY_API_KEY", "project-key")

    config = ModelRuntimeConfig.from_env("NOVELTY")

    assert config.model == "novelty-model"
    assert config.base_url == "https://example.test/v1"
    assert config.api_key == "project-key"
