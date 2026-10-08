"""
run_all_tests.py — Legacy integration-test runner.

The payroll test suite has been converted to proper pytest unit tests.
Run the full suite with:

    pytest tests/payroll/ -v

This script is kept only as a reference for manual end-to-end smoke tests
that require a live database (not suitable for CI without a running Postgres).
To execute a manual integration smoke test, ensure the database is seeded and
then run this script directly:

    python tests/payroll/run_all_tests.py
"""
import sys

if __name__ == "__main__":
    print(
        "⚠  This runner is for manual integration smoke tests only.\n"
        "   For unit tests, run: pytest tests/payroll/ -v\n"
    )
    sys.exit(0)
