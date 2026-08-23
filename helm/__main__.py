"""Executable entry point selected by bin/helm's dependency-aware launcher."""
from .cli import main

raise SystemExit(main() or 0)
