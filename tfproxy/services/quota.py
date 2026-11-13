"""每账号每日配额计数与换号。

上游限制：每个账号每天 10 次对话调用（FREE_MEMBER）。
- 每次成功创建 turn（上游 201）即计数；
- 计数持久化在 data/quota.json，按自然日（Asia/Shanghai）重置；
- 满额后由 AccountPool.switch_session 切到下一个有余额的账号。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _today() -> str:
    # 上游按韩国时区（KST, UTC+9）计"天"；用上海时区近似（同 UTC+8/9 区间差 1h，
    # 取 KST 更保守 —— 提前重置只会让我们少用不会超用，这里用 UTC+9）
    return datetime.utcnow().strftime("%Y-%m-%d") if False else time.strftime(
        "%Y-%m-%d", time.gmtime(time.time() + 9 * 3600))


class QuotaTracker:
    def __init__(self, cfg: Any):
        self.cfg = cfg
        self.limit: int = getattr(cfg.upstream, "daily_quota_per_account", 10)
        self.path: Optional[Path] = cfg.resolve_path(getattr(cfg.storage, "quota_file", None) or "./data/quota.json")
        self.usage: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.usage = data
            except Exception as e:
                logger.warning("quota load failed: %s", e)

    def _save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.usage, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning("quota save failed: %s", e)

    def _entry(self, email: str) -> Dict[str, Any]:
        ent = self.usage.get(email)
        today = _today()
        if not isinstance(ent, dict) or ent.get("date") != today:
            ent = {"date": today, "count": 0}
            self.usage[email] = ent
        return ent

    def used(self, email: str) -> int:
        return int(self._entry(email).get("count", 0))

    def remaining(self, email: str) -> int:
        return max(0, self.limit - self.used(email))

    def increment(self, email: str) -> None:
        if not email:
            return
        ent = self._entry(email)
        ent["count"] = int(ent.get("count", 0)) + 1
        self._save()
        if ent["count"] >= self.limit:
            logger.info("quota exhausted for %s (%d/%d)", email, ent["count"], self.limit)

    def mark_exhausted(self, email: str) -> None:
        """上游直接返回 429/CH-208 时，把该号今日配额记满。"""
        if not email:
            return
        ent = self._entry(email)
        ent["count"] = self.limit
        self._save()
        logger.info("marked quota exhausted for %s (upstream 429)", email)

    def set_count(self, email: str, count: int) -> None:
        """以上游 /api/v1/chat/quota 的真实 used 为准校正本地计数。"""
        if not email:
            return
        ent = self._entry(email)
        new = max(0, min(self.limit, int(count)))
        if int(ent.get("count", 0)) != new:
            ent["count"] = new
            self._save()
            logger.info("quota synced for %s: %d/%d", email, new, self.limit)

    def status(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for email, ent in self.usage.items():
            if ent.get("date") == _today():
                out[email] = {"used": ent.get("count", 0), "limit": self.limit,
                              "remaining": max(0, self.limit - ent.get("count", 0))}
        return out
