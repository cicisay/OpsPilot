"""Convenience launcher so the project runs without installation."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from ops_agent.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
