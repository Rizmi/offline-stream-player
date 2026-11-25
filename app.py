#!/usr/bin/env python3
"""Entrypoint for Offline Stream Player web application."""

import sys

from osp.config import ConfigError, load_config
from osp.server import serve


def main() -> int:
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1
    serve(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
