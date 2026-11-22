"""Set safe, non-production credentials before application module import."""

import os

os.environ.setdefault("UI_PASSWORD", "test-dashboard-password")
os.environ.setdefault("SQUID_MANAGER_PASSWORD", "test-manager-password-123456")
