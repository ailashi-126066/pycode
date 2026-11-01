# Pi 没有，pycode 新增
"""Unambiguous module entry point."""

from .app.cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())
