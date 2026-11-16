"""Run against an idle local API using the test dependencies."""
import argparse
import hashlib
import math
import time
from concurrent.futures import ThreadPoolExecutor

import httpx


def run(client, port):
    data = bytes(range(256)) * 4096
    base = f"http://127.0.0.1:{port}/api/v1"
    uploaded = client.post(base + "/upload", files={"file": ("benchmark.bin", data)})
    uploaded.raise_for_status()
    receipt = uploaded.json()
    file_id = receipt["file_uuid"]
    digest = hashlib.sha256(data).hexdigest()

    def read(route):
        start = time.perf_counter()
        response = client.get(f"{base}/{route}/{file_id}")
        response.raise_for_status()
        elapsed = time.perf_counter() - start
        if route == "download" and hashlib.sha256(response.content).hexdigest() != digest:
            raise RuntimeError("Downloaded bytes differ from the fixture")
        return elapsed * 1000

    try:
        for route in ("shares", "download"):
            read(route)
            with ThreadPoolExecutor(max_workers=4) as pool:
                start = time.perf_counter()
                samples = sorted(pool.map(read, [route] * 80))
                duration = time.perf_counter() - start
            print(f"{route:8} n=80 clients=4 rate={80 / duration:.1f}/s "
                  f"p50={samples[39]:.1f}ms p95={samples[math.ceil(0.95 * 80)-1]:.1f}ms")
    finally:
        client.delete(f"{base}/shares/{file_id}", headers={
            "Authorization": "Bearer " + receipt["owner_token"],
        }).raise_for_status()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    with httpx.Client(timeout=30, trust_env=False) as client:
        run(client, parser.parse_args().port)
