#!/usr/bin/env python3
"""
TEST SCRIPT FOR OVER 2.5 GOALS PREDICTION SYSTEM
Demonstrates the algorithm using sample data

NOTE (2026-09): The legacy `apply_algorithm` helper from the deleted
`over25_predictor.py` is no longer available. The live rule engine
lives in `over25_soccerbase.process_match` (with defence gates,
symmetry checks, H2H vetoes, etc.). Run the live pipeline instead
via `daily_runner.py` or `python3 test_regressions.py`.
"""

import sys
from datetime import datetime

print("=" * 70)
print("OVER 2.5 GOALS PREDICTION SYSTEM - TEST MODE")
print(f"Date: {datetime.now().strftime('%Y-%m-%d')}")
print("=" * 70)
print("\n[INFO] Legacy test entry point retired.")
print("  Run the live pipeline:")
print("     Local (primary):   run_local.bat")
print("     Direct:            python3 daily_runner.py")
print("     Regression tests:  python3 test_regressions.py")
sys.exit(0)
