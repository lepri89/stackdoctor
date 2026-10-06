"""Demo Celery app: processes orders in Postgres."""

import os
import time

import psycopg
from celery import Celery

REDIS = os.environ.get("REDIS_URL", "redis://localhost:56379")
DATABASE_URL = os.environ.get("APP_DATABASE_URL", "postgresql://shop:shop@localhost:55432/shop")

app = Celery("demo", broker=f"{REDIS}/0", backend=f"{REDIS}/1")
app.conf.update(
    task_soft_time_limit=30,
    task_time_limit=40,
    result_expires=3600,
    result_extended=True,  # store task name/args with results
    worker_prefetch_multiplier=1,
    broker_transport_options={"priority_steps": [0, 3, 6, 9], "queue_order_strategy": "priority"},
)


@app.task
def process_order(order_id: int) -> str:
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute("SET lock_timeout = '20s'")
        conn.execute("UPDATE orders SET status = 'processed' WHERE id = %s", (order_id,))
    time.sleep(0.2)
    return f"order {order_id} processed"
