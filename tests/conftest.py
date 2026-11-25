"""Test-wide environment setup — imported before any kalshi_agent module.

The real app password is injected only via the KALSHI_WEB_PASSWORD
environment variable in deployed environments.  Tests use their own
throwaway password so this repository never contains the production one.
"""
import os

TEST_PASSWORD = "unit-test-gate-password-9f3a"
os.environ["KALSHI_WEB_PASSWORD"] = TEST_PASSWORD
