"""`<role>.model` in the mapping form: the local model leads the chain.

Since 2026-10-23 scoring asks Qwen through Ollama first and the hosted :free
ids only after it. The mapping must keep its own endpoint all the way down —
a lone local entry sent through `client_from_config` would go to OpenRouter.
"""

from __future__ import annotations

from src.config import Config
from src.llm import ModelChain, chain_from_config, model_entries, primary_model
from src.local_llm import local_model
from tests.conftest import write_config

LOCAL = {"model": "qwen3.8:27b", "base_url": "http://localhost:11434/v1",
         "timeout": 600, "max_retries": 0, "reasoning_effort": "none"}


def test_a_mapping_primary_leads_the_chain_with_its_own_endpoint(tmp_path):
    cfg = write_config(tmp_path, {"scoring": {
        "model": LOCAL, "fallback_models": ["google/gemma-4-31b-it:free"]}})
    entries = model_entries(cfg, "scoring")
    assert [(e["model"], e["base_url"]) for e in entries] == [
        ("qwen3.8:27b", "http://localhost:11434/v1"), ("google/gemma-4-31b-it:free", None)]
    assert entries[0]["timeout"] == 600 and entries[0]["reasoning_effort"] == "none"
    assert primary_model(cfg, "scoring", "default") == "qwen3.8:27b"
    assert local_model(cfg)["model"] == "qwen3.8:27b"


def test_a_lone_local_primary_is_never_sent_to_the_global_provider(tmp_path):
    cfg = write_config(tmp_path, {"scoring": {"model": LOCAL, "fallback_models": []}})
    assert isinstance(chain_from_config(cfg, "scoring"), ModelChain)


def test_the_string_form_is_unchanged(tmp_path):
    cfg = write_config(tmp_path, {"scoring": {"model": "google/gemma-4-31b-it:free"}})
    assert primary_model(cfg, "scoring", "default") == "google/gemma-4-31b-it:free"
    assert model_entries(cfg, "scoring")[0]["base_url"] is None


def test_a_mapping_primary_is_validated_like_a_fallback(tmp_path):
    cfg = write_config(tmp_path, {"scoring": {"model": {**LOCAL, "timeout": 0,
                                                        "reasoning_effort": "max"}}})
    problems = cfg.validate(require_llm=False)
    assert any("scoring.model.timeout" in p for p in problems)
    assert any("scoring.model.reasoning_effort" in p for p in problems)
    # A local name has no vendor prefix, and must not be told it needs one.
    assert not any("vendor-qualified" in p for p in problems)


def test_the_shipped_config_scores_locally_first_without_nemotron():
    shipped = Config.load()
    names = [e["model"] for e in model_entries(shipped, "scoring")]
    assert names[0] == "qwen3.8:27b"
    assert not any("nemotron" in e["model"]
                   for role in ("scoring", "tailoring")
                   for e in model_entries(shipped, role))
