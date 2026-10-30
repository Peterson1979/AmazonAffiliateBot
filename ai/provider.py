"""Content-generation provider selection."""

from typing import Any, Dict
import os

from ai.groq import GroqClient
from ai.ollama import OllamaClient


def create_content_provider(config: Dict[str, Any]):
    provider_cfg = config.get("content_generation", {})
    provider = str(provider_cfg.get("provider", "groq")).strip().lower()
    if provider == "groq":
        cfg = config.get("groq", {})
        api_key_env = cfg.get("api_key_env", "GROQ_API_KEY")
        return GroqClient(
            api_key=os.getenv(api_key_env, ""),
            base_url=cfg.get("base_url", "https://api.groq.com/openai/v1"),
            model=cfg.get("model", "openai/gpt-oss-120b"),
            timeout=cfg.get("timeout", provider_cfg.get("timeout", 30)),
            max_attempts=cfg.get("max_attempts", provider_cfg.get("max_attempts", 2)),
            num_predict=cfg.get("num_predict", 120),
            reasoning_effort=cfg.get("reasoning_effort", "low"),
        )
    if provider == "ollama":
        cfg = config.get("ollama", {})
        return OllamaClient(
            base_url=cfg.get("base_url", "http://localhost:11434"),
            model=cfg.get("model", "qwen2.5:7b"),
            timeout=cfg.get("timeout", 60),
            max_attempts=cfg.get("max_attempts", 2),
            num_predict=cfg.get("num_predict", 90),
            num_ctx=cfg.get("num_ctx", 1024),
        )
    raise ValueError(f"Unsupported content-generation provider: {provider}")
