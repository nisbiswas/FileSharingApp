import asyncio
import logging
import os
import re
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool
from starlette.formparsers import MultiPartException, MultiPartParser

from store import CHUNK, MAX_BYTES, ShareError, Store

logger = logging.getLogger("uvicorn.error")
BODY_LIMIT = MAX_BYTES + 64 * 1024
TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}")


class RequestLog:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = time.monotonic()
        request_id = secrets.token_hex(12)
        status = 500

        async def send_with_id(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", []).append((b"x-request-id", request_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            route = getattr(scope.get("route"), "path", "unmatched")
            logger.info("request_id=%s method=%s route=%s status=%d duration_ms=%.1f",
                        request_id, scope["method"], route, status, 1000 * (time.monotonic() - started))


class BodyTooLarge(MultiPartException):
    pass


async def bounded_body(request):
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > BODY_LIMIT:
            raise BodyTooLarge("Upload exceeds the request limit.")
        yield chunk


def owner_token(header):
    scheme, _, token = (header or "").partition(" ")
    if scheme.lower() != "bearer" or not TOKEN.fullmatch(token):
        raise ShareError(401, "A valid sender token is required.")
    return token


def clean_filename(name):
    base = re.split(r"[/\\]", name or "")[-1]
    return "".join(char for char in base if char.isprintable()).strip()[:200] or "download"


def byte_range(value, size):
    # A server may return the full representation for unsupported/multipart ranges.
    match = re.fullmatch(r"bytes=(\d{0,20})-(\d{0,20})", value or "")
    if not match or not any(match.groups()):
        return None
    first, last = match.groups()
    start = int(first) if first else max(0, size - int(last))
    end = min(int(last), size - 1) if first and last else size - 1
    if start > end:
        return False
    return start, end


def create_app(store):
    async def sweep():
        while True:
            await asyncio.sleep(60)
            try:
                await run_in_threadpool(store.collect)
            except sqlite3.Error:
                logger.exception("Expired share cleanup failed")

    @asynccontextmanager
    async def lifespan(_app):
        await run_in_threadpool(store.initialize)
        await run_in_threadpool(store.collect)
        task = asyncio.create_task(sweep())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    api = FastAPI(title="FileSharingApp", version="2.0", lifespan=lifespan)
    api.add_middleware(RequestLog)

    @api.exception_handler(ShareError)
    async def share_error(_request, exc):
        headers = {"Cache-Control": "no-store"}
        if exc.retry_after:
            headers["Retry-After"] = str(exc.retry_after)
        return JSONResponse({"detail": exc.message}, status_code=exc.status, headers=headers)

    @api.exception_handler(sqlite3.OperationalError)
    async def storage_error(_request, exc):
        logger.error("Storage operation failed (%s)", getattr(exc, "sqlite_errorname", "unknown"))
        return JSONResponse({"detail": "Storage is temporarily unavailable."}, status_code=503,
                            headers={"Retry-After": "5"})

    @api.get("/status")
    def status():
        return {"status": "It is running"}

    @api.get("/ready")
    def ready():
        with store.connection() as db:
            db.execute("SELECT id FROM shares LIMIT 1").fetchone()
        return {"status": "ready"}

    @api.post("/api/v1/upload", openapi_extra={"requestBody": {"required": True, "content": {
        "multipart/form-data": {"schema": {"type": "object", "required": ["file"],
            "properties": {"file": {"type": "string", "format": "binary"}}}}}}})
    async def upload(request: Request, ttl_hours: Literal["1", "6", "24"] = "24",
                     authorization: str | None = Header(None)):
        token = owner_token(authorization) if authorization is not None else secrets.token_urlsafe(32)
        if request.headers.get("content-type", "").split(";")[0].lower() != "multipart/form-data":
            raise ShareError(415, "Send the file as multipart form data.")
        length = request.headers.get("content-length")
        if length and (not length.isdecimal() or len(length) > 20 or int(length) > BODY_LIMIT):
            raise ShareError(413, "Choose a file of 10 MiB or less.")
        try:
            form = await MultiPartParser(request.headers, bounded_body(request), max_files=1, max_fields=0).parse()
        except BodyTooLarge:
            raise ShareError(413, "Choose a file of 10 MiB or less.")
        except MultiPartException:
            raise ShareError(422, "Send exactly one file in the file field.")
        try:
            file = form.get("file")
            if not hasattr(file, "file"):
                raise ShareError(422, "Choose a file to upload.")
            receipt = await run_in_threadpool(store.create, file.file, clean_filename(file.filename),
                                              int(ttl_hours) * 3600, token, request.client.host)
        finally:
            await form.close()
        return JSONResponse({**receipt, "owner_token": token}, headers={"Cache-Control": "no-store"})

    def opened(uuid: UUID):
        with store.read(str(uuid)) as data:
            yield data

    @api.get("/api/v1/shares/{uuid}")
    def metadata(data=Depends(opened)):
        return JSONResponse(data[0], headers={"Cache-Control": "no-store"})

    @api.get("/api/v1/download/{uuid}")
    def download(request: Request, data=Depends(opened)):
        info, blob = data
        size = info["size_bytes"]
        tag = '"' + info["sha256"] + '"'
        headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
                   "Accept-Ranges": "bytes", "ETag": tag,
                   "Content-Disposition": "attachment; filename*=UTF-8''" + quote(info["filename"], safe="")}
        requested = None
        if request.headers.get("if-range", tag) == tag:
            requested = byte_range(request.headers.get("range"), size)
        if requested is False:
            return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
        start, end = requested or (0, size - 1)
        if requested:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        length = end - start + 1
        headers["Content-Length"] = str(length)

        def chunks():
            remaining = length
            blob.seek(start)
            while remaining:
                chunk = blob.read(min(remaining, CHUNK))
                remaining -= len(chunk)
                yield chunk

        return StreamingResponse(chunks(), media_type="application/octet-stream",
                                 status_code=206 if requested else 200, headers=headers)

    @api.delete("/api/v1/shares/{uuid}", status_code=204)
    def revoke(uuid: UUID, authorization: str | None = Header(None)):
        store.revoke(str(uuid), owner_token(authorization))
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    return api


app = create_app(Store(os.getenv("UPLOAD_DIR", Path(__file__).parent.parent / "uploads")))
