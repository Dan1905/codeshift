def test_health(api):
    response = api.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_stock_lookup(api):
    assert api.get("/stock/widget").json() == {"sku": "widget", "quantity": 10}


def test_stock_lookup_retries_transient_failure(api, upstream):
    upstream.failures_left = 2
    assert api.get("/stock/widget").json()["quantity"] == 10


def test_create_order(api):
    response = api.post("/orders", json={"sku": "widget", "quantity": 4})
    assert response.status_code == 200
    body = response.json()
    assert body["reservation_id"] == "res-widget-4"
    assert body["total"] == 10.0


def test_create_order_rejects_when_out_of_stock(api):
    response = api.post("/orders", json={"sku": "gadget", "quantity": 1})
    assert response.status_code == 409
