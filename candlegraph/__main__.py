"""``python -m candlegraph <command>`` -> the CLI."""

from __future__ import annotations

import sys

from candlegraph.cli import main

if __name__ == "__main__":
    sys.exit(main())
