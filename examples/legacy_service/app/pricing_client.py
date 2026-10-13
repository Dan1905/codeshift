"""Client for the upstream pricing service."""

import requests

from app.settings import pricing_url


class PricingClient:
    """Thin wrapper around the pricing API."""

    def __init__(self, currency="USD"):
        self.currency = currency

    def quote_price(self, sku):
        response = requests.get(
            f"{pricing_url()}/price/{sku}", params={"currency": self.currency}, timeout=2
        )
        response.raise_for_status()
        return response.json()["unit_price"]

    def quote_total(self, sku, quantity):
        # Unit price times quantity, rounded to cents.
        return round(self.quote_price(sku) * quantity, 2)
