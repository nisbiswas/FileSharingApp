"""Transactional share storage. The data directory must be on a local filesystem."""
import hashlib
import math
import re
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path
from uuid import uuid4

CHUNK = 128 * 1024
MAX_BYTES = 10 * 1024**2
DAY = 86400
LEGACY = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}(?:\.[^.]*)?")


class ShareError(Exception):
    def __init__(self, status, message, retry_after=None):
        self.status = status
        self.message = message
        self.retry_after = retry_after


def fingerprint(source):
    size = 0
    digest = hashlib.sha256()
    while chunk := source.read(CHUNK):
        size += len(chunk)
        if size > MAX_BYTES:
            raise ShareError(413, "Choose a file of 10 MiB or less.")
        digest.update(chunk)
    source.seek(0)
    return size, digest.hexdigest()


class Store:
    def __init__(self, directory, *, clock=time.time, quota=256 * 1024**2, cooldown=5):
        self.directory = Path(directory)
        self.database = self.directory / "shares.sqlite3"
        self.clock = clock
        self.quota = quota
        self.cooldown = cooldown

    @contextmanager
    def connection(self, *, write=False):
        with closing(sqlite3.connect(self.database, timeout=5, check_same_thread=False)) as db:
            db.row_factory = sqlite3.Row
            with db:
                db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield db

    def initialize(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS shares (
                    id INTEGER PRIMARY KEY, uuid TEXT NOT NULL UNIQUE,
                    owner_hash TEXT UNIQUE, filename TEXT NOT NULL,
                    size INTEGER NOT NULL, sha256 TEXT NOT NULL, ttl INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
                    content BLOB NOT NULL
                );
                CREATE INDEX IF NOT EXISTS share_expiry ON shares(expires_at);
                CREATE TABLE IF NOT EXISTS rate_limits (
                    client TEXT PRIMARY KEY, next_upload REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS rate_expiry ON rate_limits(next_upload);
                CREATE TABLE IF NOT EXISTS storage_usage (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), bytes INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO storage_usage VALUES (1, 0);
                CREATE TRIGGER IF NOT EXISTS charge_share AFTER INSERT ON shares BEGIN
                    UPDATE storage_usage SET bytes=bytes+NEW.size;
                END;
                CREATE TRIGGER IF NOT EXISTS release_revoked AFTER UPDATE OF revoked ON shares
                WHEN OLD.revoked=0 AND NEW.revoked=1 BEGIN
                    UPDATE storage_usage SET bytes=bytes-OLD.size;
                END;
                CREATE TRIGGER IF NOT EXISTS release_expired AFTER DELETE ON shares
                WHEN OLD.revoked=0 BEGIN
                    UPDATE storage_usage SET bytes=bytes-OLD.size;
                END;
            """)
        self.import_legacy()

    def import_legacy(self):
        # Old deployments used <UUID><last extension> and discarded the original name.
        for path in self.directory.iterdir():
            if not LEGACY.fullmatch(path.name) or path.is_symlink():
                continue
            with self.connection(write=True) as db:
                if not path.is_file():
                    continue
                expires = int(path.stat().st_mtime) + DAY
                share_id = path.name[:36]
                if expires > self.clock() and not db.execute(
                    "SELECT 1 FROM shares WHERE uuid=?", (share_id,)
                ).fetchone():
                    with path.open("rb") as source:
                        size, digest = fingerprint(source)
                        self.insert(db, source, share_id, None, path.name, size, digest, DAY, expires)
            # Commit before unlink so a crash can safely repeat this import.
            path.unlink(missing_ok=True)

    @staticmethod
    def insert(db, source, share_id, owner, filename, size, digest, ttl, expires):
        row = db.execute("""
            INSERT INTO shares(uuid,owner_hash,filename,size,sha256,ttl,expires_at,content)
            VALUES(?,?,?,?,?,?,?,zeroblob(?))
        """, (share_id, owner, filename, size, digest, ttl, expires, size))
        with db.blobopen("shares", "content", row.lastrowid) as blob:
            while chunk := source.read(CHUNK):
                blob.write(chunk)

    @staticmethod
    def receipt(row):
        return {"file_uuid": row["uuid"], "filename": row["filename"], "size_bytes": row["size"],
                "sha256": row["sha256"], "expires_at": row["expires_at"]}

    def create(self, source, filename, ttl, token, client):
        size, digest = fingerprint(source)
        owner = hashlib.sha256(token.encode()).hexdigest()
        with self.connection(write=True) as db:
            now = self.clock()
            previous = db.execute("SELECT uuid,filename,size,sha256,expires_at,ttl,revoked FROM shares WHERE owner_hash=?",
                                  (owner,)).fetchone()
            if previous:
                if previous["revoked"] or previous["expires_at"] <= now:
                    raise ShareError(410, "This transfer has ended. Start a new transfer.")
                if (previous["sha256"], previous["filename"], previous["ttl"]) != (digest, filename, ttl):
                    raise ShareError(409, "This upload key already belongs to another transfer.")
                return self.receipt(previous)
            rate = db.execute("SELECT next_upload FROM rate_limits WHERE client=?", (client,)).fetchone()
            if rate and rate[0] > now:
                raise ShareError(429, "Please wait before creating another link.", math.ceil(rate[0] - now))
            used = db.execute("SELECT bytes FROM storage_usage WHERE singleton=1").fetchone()[0]
            if size + used > self.quota:
                raise ShareError(507, "Share storage is full.")
            share_id = str(uuid4())
            expires = int(now) + ttl
            self.insert(db, source, share_id, owner, filename, size, digest, ttl, expires)
            db.execute("INSERT OR REPLACE INTO rate_limits VALUES(?,?)", (client, now + self.cooldown))
            return {"file_uuid": share_id, "filename": filename, "size_bytes": size,
                    "sha256": digest, "expires_at": expires}

    @contextmanager
    def read(self, share_id):
        with self.connection() as db:
            row = db.execute("""
                SELECT id,uuid,filename,size,sha256,expires_at FROM shares
                WHERE uuid=? AND revoked=0 AND expires_at>?
            """, (share_id, self.clock())).fetchone()
            if row is None:
                raise ShareError(404, "This link is unavailable. It may have expired or been revoked.")
            with db.blobopen("shares", "content", row["id"], readonly=True) as blob:
                yield self.receipt(row), blob

    def revoke(self, share_id, token):
        owner = hashlib.sha256(token.encode()).hexdigest()
        with self.connection(write=True) as db:
            changed = db.execute("""
                UPDATE shares SET revoked=1, content=x'' WHERE uuid=? AND owner_hash=? AND expires_at>?
            """, (share_id, owner, self.clock()))
            if not changed.rowcount:
                raise ShareError(404, "Transfer unavailable for this sender.")

    def collect(self):
        with self.connection(write=True) as db:
            now = self.clock()
            db.execute("DELETE FROM shares WHERE expires_at<=?", (now,))
            db.execute("DELETE FROM rate_limits WHERE next_upload<=?", (now,))
