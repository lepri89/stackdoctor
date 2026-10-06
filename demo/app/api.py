"""Demo API: enqueue orders and read order stats."""

import psycopg
from fastapi import FastAPI

from tasks import DATABASE_URL, process_order

api = FastAPI(title="stackdoctor demo shop")


@api.get("/health")
def health():
    return {"ok": True}


@api.post("/orders/enqueue")
def enqueue(n: int = 10):
    ids = [process_order.apply_async((i,), priority=6 if i % 2 else 0).id for i in range(1, n + 1)]
    return {"enqueued": len(ids)}


@api.get("/orders/stats")
def stats():
    with psycopg.connect(DATABASE_URL) as conn:
        rows = conn.execute("SELECT status, count(*) FROM orders GROUP BY status").fetchall()
    return dict(rows)
