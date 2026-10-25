"""Shim keeping the spec path ``tools/probe.py``; the implementation is ``edu09.probe``."""

from edu09.probe import main

if __name__ == "__main__":
    raise SystemExit(main())
