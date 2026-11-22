"""Set safe, non-production credentials before application module import."""

import os

os.environ.setdefault(key="UI_PASSWORD", value="test-dashboard-password")
os.environ.setdefault(
    key="SQUID_MANAGER_PASSWORD", value="test-manager-password-123456"
)
