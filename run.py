#!/usr/bin/env python
"""Convenience launcher: python run.py -c config.yaml"""
from tfproxy.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
