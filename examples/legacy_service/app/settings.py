"""Runtime settings for the order service."""

import os


def inventory_url() -> str:
    return os.environ.get("INVENTORY_URL", "http://localhost:9001")


def pricing_url() -> str:
    return os.environ.get("PRICING_URL", "http://localhost:9002")
