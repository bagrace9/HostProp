import sys
from pathlib import Path

if __package__ in (None, ""):
    # See the note in webapp.py: this also lets you run the file directly,
    # `python reverse_proxy_automation/__main__.py`, as well as `python -m reverse_proxy_automation`.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "reverse_proxy_automation"

from .webapp import main

if __name__ == "__main__":
    raise SystemExit(main())
