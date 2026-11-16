import hashlib
import io
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from main import BODY_LIMIT, create_app
from store import CHUNK, DAY, MAX_BYTES, Store


class ShareTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.now = 1700000000
        self.store = Store(self.folder, clock=lambda: self.now)
        self.client = self.enterContext(TestClient(create_app(self.store)))
        self.owner = {"Authorization": "Bearer " + "a" * 48}

    def post(self, data=b"hello", name="brief.pdf", headers=None, ttl=24):
        return self.client.post(f"/api/v1/upload?ttl_hours={ttl}", files={"file": (name, data)},
                                headers=headers)

    def test_original_routes_and_filename(self):
        result = self.post(name="folder/brief.pdf")
        self.assertEqual(result.status_code, 200, result.text)
        share = result.json()
        url = "/api/v1/download/" + share["file_uuid"]
        res = self.client.get(url)
        self.assertEqual(res.content, b"hello")
        self.assertIn("brief.pdf", res.headers["content-disposition"])
        self.assertEqual(share["sha256"], hashlib.sha256(b"hello").hexdigest())
        info = self.client.get("/api/v1/shares/" + share["file_uuid"])
        self.assertNotIn("owner_token", info.json())
        self.assertEqual(info.json()["expires_at"], self.now + DAY)
        self.assertEqual(self.client.get(url[:-1]).status_code, 422)
        self.assertEqual(self.client.get("/api/v1/download/" + str(uuid4())).status_code, 404)
        self.assertIn("x-request-id", res.headers)

    def test_range_and_empty_file(self):
        url = "/api/v1/download/" + self.post().json()["file_uuid"]
        for value, body in [("bytes=0-0", b"h"), ("bytes=1-", b"ello"), ("bytes=-2", b"lo")]:
            res = self.client.get(url, headers={"Range": value})
            self.assertEqual((res.status_code, res.content), (206, body))
        self.assertEqual(self.client.get(url, headers={"Range": "bytes=9-"}).status_code, 416)
        self.now += 5
        empty = self.post(b"").json()
        res = self.client.get("/api/v1/download/" + empty["file_uuid"])
        self.assertEqual((res.status_code, res.content), (200, b""))

    def test_concurrent_retries_and_mismatched_reuse(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.post(headers=self.owner), range(4)))
        self.assertEqual([r.status_code for r in results], [200] * 4)
        self.assertEqual(len({r.json()["file_uuid"] for r in results}), 1)
        for kwargs in [{"data": b"changed"}, {"name": "other"}, {"ttl": 6}]:
            self.assertEqual(self.post(headers=self.owner, **kwargs).status_code, 409)
        res = self.post()
        self.assertEqual(res.status_code, 429)
        self.assertEqual(res.headers["retry-after"], "5")

    def test_revoke_preserves_open_snapshot_and_rejects_new_reads(self):
        share = self.post(headers=self.owner).json()["file_uuid"]
        url = "/api/v1/shares/" + share
        self.assertEqual(self.client.delete(url).status_code, 401)
        self.assertEqual(self.client.delete(url, headers={"Authorization": "Bearer " + "b"*48}).status_code, 404)
        with self.store.read(share) as (_, blob):
            self.assertEqual(self.client.delete(url, headers=self.owner).status_code, 204)
            self.assertEqual(self.client.get(url).status_code, 404)
            self.assertEqual(blob.read(), b"hello")
        self.assertEqual(self.client.delete(url, headers=self.owner).status_code, 204)
        self.assertEqual(self.post(headers=self.owner).status_code, 410)
        self.assertEqual(self.client.get("/api/v1/download/" + share).status_code, 404)

    def test_expiry_on_boundary_before_sweep(self):
        share = self.post(ttl=1).json()["file_uuid"]
        self.now += 3599
        url = "/api/v1/download/" + share
        self.assertEqual(self.client.get(url).status_code, 200)
        self.now += 1
        self.assertEqual(self.client.get(url).status_code, 404)
        self.store.collect()
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM shares").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT bytes FROM storage_usage").fetchone()[0], 0)

    def test_quota_and_failure_rollback(self):
        self.store.quota = 5
        def interrupted(db, *args):
            Store.insert(db, *args)
            raise sqlite3.OperationalError("IO failure")
        with patch.object(self.store, "insert", interrupted):
            self.assertEqual(self.post(headers=self.owner).status_code, 503)
        share = self.post(headers=self.owner).json()["file_uuid"]
        self.now += 5
        self.assertEqual(self.post().status_code, 507)
        self.client.delete("/api/v1/shares/" + share, headers=self.owner)
        self.assertEqual(self.post().status_code, 200)

    def test_concurrent_quota_admission(self):
        self.store.quota = 5
        def submit(number):
            from store import ShareError
            try:
                self.store.create(io.BytesIO(b"hello"), "x", DAY, str(number), str(number))
                return 200
            except ShareError as exc:
                return exc.status
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sorted(pool.map(submit, range(4))), [200, 507, 507, 507])

    def test_bounded_upload_and_validation(self):
        self.assertEqual(self.post(data=b"x" * (MAX_BYTES+1)).status_code, 413)
        self.assertEqual(self.post(data=b"x" * MAX_BYTES).status_code, 200)
        self.assertEqual(self.post(ttl=25).status_code, 422)
        self.assertEqual(self.client.post("/api/v1/upload", content="test").status_code, 415)
        res = self.client.post("/api/v1/upload", files=[("file", ("a", b"a")), ("file", ("b", b"b"))])
        self.assertEqual(res.status_code, 422)
        def body():
            yield b'--x\r\nContent-Disposition: form-data; name="file"; filename="x"\r\n\r\n'
            for _ in range(BODY_LIMIT // CHUNK + 1):
                yield b"x" * CHUNK
            yield b"\r\n--x--\r\n"
        res = self.client.post("/api/v1/upload", content=body(),
                               headers={"Content-Type": "multipart/form-data; boundary=x"})
        self.assertEqual(res.status_code, 413)

    def test_restart_keeps_links_cooldown_and_replay(self):
        original = self.post(headers=self.owner).json()
        with TestClient(create_app(Store(self.folder, clock=lambda: self.now))) as second:
            self.assertEqual(second.get("/api/v1/download/" + original["file_uuid"]).content, b"hello")
            self.assertEqual(second.post("/api/v1/upload", files={"file": ("x", b"x")}).status_code, 429)
            self.assertEqual(second.post("/api/v1/upload", files={"file": ("brief.pdf", b"hello")},
                                         headers=self.owner).json(), original)

    def test_import_legacy_without_extending_expiration(self):
        uid = str(uuid4())
        old = self.folder / (uid + ".txt")
        old.write_bytes(b"legacy")
        os.utime(old, (self.now - 200, self.now - 200))
        expired = self.folder / str(uuid4())
        expired.write_bytes(b"old")
        os.utime(expired, (self.now - DAY, self.now - DAY))
        self.store.initialize()
        self.store.initialize()
        self.assertFalse(old.exists())
        self.assertFalse(expired.exists())
        self.assertEqual(self.client.get("/api/v1/download/" + uid).content, b"legacy")
        self.assertEqual(self.client.get("/api/v1/shares/" + uid).json()["expires_at"],
                         self.now - 200 + DAY)
