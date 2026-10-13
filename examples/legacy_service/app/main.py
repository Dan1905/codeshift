"""Order service: synchronous FastAPI endpoints that call two upstream services."""

from datetime import datetime

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app import inventory_client
from app.pricing_client import PricingClient

app = FastAPI(title="Order Service")
pricing = PricingClient()


class OrderRequest(BaseModel):
    sku: str
    quantity: int


def check_availability(sku, quantity):
    available = inventory_client.fetch_stock(sku)
    return available >= quantity


@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


@app.get("/stock/{sku}")
def stock(sku: str):
    return {"sku": sku, "quantity": inventory_client.fetch_stock(sku)}


@app.post("/orders")
def create_order(order: OrderRequest):
    if not check_availability(order.sku, order.quantity):
        raise HTTPException(status_code=409, detail="insufficient stock")
    reservation_id = inventory_client.reserve(order.sku, order.quantity)
    total = pricing.quote_total(order.sku, order.quantity)
    return {
        "reservation_id": reservation_id,
        "total": total,
        "created_at": datetime.utcnow().isoformat(),
    }
