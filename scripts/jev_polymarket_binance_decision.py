#!/usr/bin/env python3
"""Compatibility entry point for the isolated Jev paper-research monitor.

The old single-contract demo used a hard-coded event and independently generated
trade advice. Use --once --db PATH to collect auditable research observations.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.jev_crypto_monitor import main

if __name__ == "__main__":
    main()
