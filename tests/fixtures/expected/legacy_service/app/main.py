"""Order service: synchronous FastAPI endpoints that call two upstream services."""

from datetime import timezone, datetime

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app import inventory_client
from app.pricing_client import PricingClient

app = FastAPI(title="Order Service")
pricing = PricingClient()


class OrderRequest(BaseModel):
    sku: str
    quantity: int


async def check_availability(sku, quantity):
    available = await inventory_client.fetch_stock(sku)
    return available >= quantity


@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


@app.get("/stock/{sku}")
async def stock(sku: str):
    return {"sku": sku, "quantity": await inventory_client.fetch_stock(sku)}


@app.post("/orders")
async def create_order(order: OrderRequest):
    if not await check_availability(order.sku, order.quantity):
        raise HTTPException(status_code=409, detail="insufficient stock")
    reservation_id = await inventory_client.reserve(order.sku, order.quantity)
    total = await pricing.quote_total(order.sku, order.quantity)
    return {
        "reservation_id": reservation_id,
        "total": total,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
