"""Client for the upstream pricing service."""

from app.settings import pricing_url
import httpx


class PricingClient:
    """Thin wrapper around the pricing API."""

    def __init__(self, currency="USD"):
        self.currency = currency

    async def quote_price(self, sku):
        async with httpx.AsyncClient(follow_redirects=True, timeout=None) as client:
            response = await client.get(
                f"{pricing_url()}/price/{sku}", params={"currency": self.currency}, timeout=2
            )
            response.raise_for_status()
            return response.json()["unit_price"]

    async def quote_total(self, sku, quantity):
        # Unit price times quantity, rounded to cents.
        return round(await self.quote_price(sku) * quantity, 2)
