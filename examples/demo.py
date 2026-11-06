"""Kept for `python examples/demo.py`; the demo now ships as `pl demo`."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plowline.demo import ENV, run, sh  # noqa: E402,F401

if __name__ == "__main__":
    run()
