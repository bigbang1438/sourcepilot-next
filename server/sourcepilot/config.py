"""Bounded local-only configuration."""

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


def local_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "http" or parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("模型地址必须为本机 HTTP 服务")
    return value.rstrip("/")


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    chat_url: str = "http://127.0.0.1:11435"
    chat_model: str = "portfolio-qwen3-4b-instruct:latest"
    embedding_model: str = "portfolio-bge-m3:latest"
    vision_url: str = "http://127.0.0.1:11436"
    vision_model: str = "qwen3-vl:2b"
    reranker_model: str = ""
    max_steps: int = 12
    max_seconds: int = 240
    max_tokens: int = 24000

    @classmethod
    def from_env(cls, data_dir=None):
        return cls(
            data_dir=Path(
                data_dir
                or os.getenv("SOURCEPILOT_DATA", Path(__file__).resolve().parents[1] / "data")
            ),
            chat_url=local_url(os.getenv("SOURCEPILOT_OLLAMA_URL", "http://127.0.0.1:11435")),
            chat_model=os.getenv("SOURCEPILOT_CHAT_MODEL", "portfolio-qwen3-4b-instruct:latest"),
            embedding_model=os.getenv("SOURCEPILOT_EMBEDDING_MODEL", "portfolio-bge-m3:latest"),
            vision_url=local_url(os.getenv("SOURCEPILOT_VISION_URL", "http://127.0.0.1:11436")),
            vision_model=os.getenv("SOURCEPILOT_VISION_MODEL", "qwen3-vl:2b"),
            reranker_model=os.getenv("SOURCEPILOT_RERANKER_MODEL", ""),
            max_steps=min(30, max(2, int(os.getenv("SOURCEPILOT_MAX_STEPS", "12")))),
            max_seconds=min(900, max(5, int(os.getenv("SOURCEPILOT_MAX_SECONDS", "240")))),
            max_tokens=min(100000, max(1000, int(os.getenv("SOURCEPILOT_MAX_TOKENS", "24000")))),
        )
