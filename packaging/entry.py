"""PyInstaller entry point.

`raise SystemExit(main())`, not a bare `main()`: the packaged app must
report its exit code, or `--smoke` can fail inside a frozen build and CI
will still call the run green.
"""
import sys

from mouseshare.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
