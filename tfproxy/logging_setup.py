from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(level: str = "info", log_dir: str | os.PathLike | None = None) -> None:
    lvl = getattr(logging, str(level).upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(lvl)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # avoid duplicate handlers on reload
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler) for h in root.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        sh.setLevel(lvl)
        root.addHandler(sh)

    if log_dir:
        p = Path(log_dir)
        p.mkdir(parents=True, exist_ok=True)
        log_path = p / "tfproxy.log"
        if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
            fh = RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
            fh.setFormatter(fmt)
            fh.setLevel(lvl)
            root.addHandler(fh)

    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(max(lvl, logging.WARNING))
