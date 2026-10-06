"""Enqueue N demo tasks: python enqueue.py 50"""

import sys

from tasks import process_order

n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
for i in range(1, n + 1):
    process_order.apply_async((i,), priority=6 if i % 2 else 0)  # exercise priority queues
print(f"enqueued {n} tasks")
