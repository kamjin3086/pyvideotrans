"""CLI entry point using the same local-service profile as ``run_local.sh``."""

from __future__ import annotations

import runpy
import sys

from start_local import configure, ROOT


if __name__ == "__main__":
    configure()
    runpy.run_path(str(ROOT / "cli.py"), run_name="__main__")
