#!/usr/bin/env python3
"""Deprecated compatibility imports for the accumulation evaluator.

Use ``accumulation_evaluator.py`` under PM2. This module no longer owns scanner
handoff files or alert state, preventing a second daemon from racing the evaluator.
"""

from accumulation_detection import check_accumulation, confluence, get_hourly_buckets


def main() -> None:
    print("accumulation_monitor is deprecated; use accumulation-evaluator.")


if __name__ == "__main__":
    main()
