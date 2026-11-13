from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from ..config import AppConfig

logger = logging.getLogger(__name__)

DEFAULT_MODELS: List[Dict[str, str]] = [
    {"id": "auto", "owned_by": "kt", "name": "오토 모델 라우터"},
    {"id": "openai/gpt-6-astra", "owned_by": "openai", "name": "gpt-6-astra"},
    {"id": "openai/gpt-6.1-sol", "owned_by": "openai", "name": "gpt-6.1-sol"},
    {"id": "openai/gpt-6-sol", "owned_by": "openai", "name": "gpt-6-sol"},
    {"id": "openai/gpt-6-luna", "owned_by": "openai", "name": "gpt-6-luna"},
    {"id": "anthropic/claude-fable-5", "owned_by": "anthropic", "name": "Claude Fable 5"},
    {"id": "anthropic/claude-opus-5", "owned_by": "anthropic", "name": "Claude Opus 5"},
    {"id": "anthropic/claude-sonnet-5", "owned_by": "anthropic", "name": "Claude Sonnet 5"},
    {"id": "kt/midm-k-3.0-pro", "owned_by": "kt", "name": "Mi:dm K 3.0 Pro"},
    {"id": "moonshotai/kimi-k3", "owned_by": "moonshotai", "name": "Kimi K3"},
    {"id": "google/gemma-4-31b-it", "owned_by": "google", "name": "Gemma 4 31B"},
]


class ModelRegistry:
    """Caches the upstream model catalog and resolves aliases."""

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.models: List[Dict[str, Any]] = list(DEFAULT_MODELS)
        self.aliases: Dict[str, str] = dict(cfg.models.aliases or {})
        self.last_refresh: float = 0.0
        self.last_error: str = ""
        self.source: str = "builtin"

    def resolve(self, model: str) -> str:
        if not model:
            return self.aliases.get("*", "auto")
        if model in self.aliases:
            return self.aliases[model]
        # case-insensitive alias match
        low = model.lower()
        for k, v in self.aliases.items():
            if k != "*" and k.lower() == low:
                return v
        # if model matches a known id, keep it
        ids = {m["id"] for m in self.models}
        if model in ids:
            return model
        # fuzzy: accept bare name like "gpt-6-astra"
        for m in self.models:
            if m["id"].split("/")[-1].lower() == low:
                return m["id"]
        # unknown model: pass through (upstream will error) unless wildcard alias set
        return self.aliases.get("*", model) if self.aliases.get("*") and model not in ids else model

    def upsert_from_upstream(self, payload: Any) -> int:
        items = []
        if isinstance(payload, dict):
            for key in ("content", "data", "models", "items"):
                v = payload.get(key)
                if isinstance(v, list):
                    items = v
                    break
        elif isinstance(payload, list):
            items = payload
        if not items:
            return len(self.models)
        seen: Dict[str, Dict[str, Any]] = {m["id"]: m for m in self.models}
        for it in items:
            if not isinstance(it, dict):
                continue
            mid = (
                it.get("catalogModelId")
                or it.get("id")
                or it.get("modelId")
                or it.get("model_id")
            )
            if not isinstance(mid, str) or not mid:
                continue
            name = it.get("name") or it.get("displayName") or mid
            developer = it.get("developer") or it.get("ownedBy") or (mid.split("/")[0] if "/" in mid else "upstream")
            seen[mid] = {"id": mid, "owned_by": str(developer).lower().replace(" ", "-"), "name": name}
        self.models = list(seen.values())
        self.source = "upstream"
        self.last_refresh = time.time()
        self.last_error = ""
        logger.info("model catalog refreshed: %d models", len(self.models))
        return len(self.models)

    def list(self) -> List[Dict[str, Any]]:
        if self.cfg.models.expose_all_upstream:
            return list(self.models)
        ids = set(self.aliases.keys()) | {self.aliases.get("*", "auto")}
        return [m for m in self.models if m["id"] in ids] or list(self.models)

    def get(self, model_id: str) -> Optional[Dict[str, Any]]:
        for m in self.models:
            if m["id"] == model_id:
                return m
        for m in self.models:
            if m["id"].split("/")[-1] == model_id:
                return m
        return None
