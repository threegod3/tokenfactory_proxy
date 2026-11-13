from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import uvicorn

from .config import load_config
from .logging_setup import setup_logging
from .server import create_app


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tfproxy", description="OpenAI-compatible reverse proxy for KT TokenFactory")
    ap.add_argument("-c", "--config", default="config.yaml", help="Path to config YAML")
    ap.add_argument("--host", default=None, help="Override listen host")
    ap.add_argument("--port", type=int, default=None, help="Override listen port")
    ap.add_argument("--log-level", default=None, help="Override log level")
    ap.add_argument("--reload", action="store_true", help="Enable uvicorn reload (dev only)")
    args = ap.parse_args(argv)

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(f"[tfproxy] config not found: {cfg_path}. Copy config.example.yaml to config.yaml first.", file=sys.stderr)
        return 2
    cfg = load_config(cfg_path)
    setup_logging(args.log_level or cfg.server.log_level, cfg.base_dir / "logs")

    host = args.host or cfg.server.host
    port = args.port or cfg.server.port
    logger = logging.getLogger("tfproxy")
    logger.info("starting tfproxy on %s:%s (upstream=%s)", host, port, cfg.upstream.base_url)

    if args.reload:
        uvicorn.run(
            "tfproxy.server:get_app",
            factory=True,
            host=host,
            port=port,
            log_level=args.log_level or cfg.server.log_level,
            reload=True,
        )
    else:
        app = create_app(cfg)
        uvicorn.run(app, host=host, port=port, log_level=args.log_level or cfg.server.log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
