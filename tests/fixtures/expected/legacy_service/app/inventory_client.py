"""Client for the upstream inventory service."""

import logging

from app.settings import inventory_url
import asyncio
import httpx

logger = logging.getLogger(__name__)

RETRY_DELAY_SECONDS = 0.05


async def fetch_stock(sku, retries=2):
    """Return the quantity in stock for a SKU, retrying transient failures."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=None) as client:
        for attempt in range(retries + 1):
            try:
                response = await client.get(f"{inventory_url()}/stock/{sku}", timeout=2)
                response.raise_for_status()
                return response.json()["quantity"]
            except httpx.HTTPError:
                if attempt == retries:
                    raise
                logger.warning("inventory lookup failed for %s, retrying", sku)
                await asyncio.sleep(RETRY_DELAY_SECONDS)  # simple fixed backoff


async def reserve(sku, quantity):
    """Reserve stock and return the reservation id."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=None) as client:
        response = await client.post(
            f"{inventory_url()}/reserve",
            json={"sku": sku, "quantity": quantity},
            timeout=2,
        )
        response.raise_for_status()
        return response.json()["reservation_id"]
