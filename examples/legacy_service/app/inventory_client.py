"""Client for the upstream inventory service."""

import logging
import time

import requests

from app.settings import inventory_url

logger = logging.getLogger(__name__)

RETRY_DELAY_SECONDS = 0.05


def fetch_stock(sku, retries=2):
    """Return the quantity in stock for a SKU, retrying transient failures."""
    for attempt in range(retries + 1):
        try:
            response = requests.get(f"{inventory_url()}/stock/{sku}", timeout=2)
            response.raise_for_status()
            return response.json()["quantity"]
        except requests.RequestException:
            if attempt == retries:
                raise
            logger.warn("inventory lookup failed for %s, retrying", sku)
            time.sleep(RETRY_DELAY_SECONDS)  # simple fixed backoff


def reserve(sku, quantity):
    """Reserve stock and return the reservation id."""
    response = requests.post(
        f"{inventory_url()}/reserve",
        json={"sku": sku, "quantity": quantity},
        timeout=2,
    )
    response.raise_for_status()
    return response.json()["reservation_id"]
