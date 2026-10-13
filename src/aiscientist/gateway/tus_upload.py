"""Resumable browser-to-gateway uploads (tus 1.0 core + creation/termination/checksum).

Only this module owns bytes before a file is complete. The existing staging queue owns a
completed file afterwards. Metadata is kept beside the part file so HEAD survives a gateway
restart; every operation checks the connection's account ownership.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import importlib
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import Response

from . import upload_log

router = APIRouter(prefix="/api/tus")
_VERSION = "1.0.0"
_MAX_SIZE = 100 * 1024**3
_HEADERS = {"Tus-Resumable": _VERSION, "Cache-Control": "no-store"}
_locks_guard = threading.Lock()
_locks: dict[str, asyncio.Lock] = {}
_last_cleanup: dict[str, float] = {}


def _gw():
    # app includes this router after defining its upload/staging helpers.
    return importlib.import_module("aiscientist.gateway.app")


def _answer(status: int, *, headers: dict | None = None, body: dict | None = None) -> Response:
    merged = {**_HEADERS, **(headers or {})}
    if body is None:
        return Response(status_code=status, headers=merged)
    return Response(json.dumps(body), status_code=status, headers=merged, media_type="application/json")


def _require_version(request: Request) -> Response | None:
    if request.headers.get("Tus-Resumable") != _VERSION:
        return _answer(412, headers={"Tus-Version": _VERSION}, body={"error": "Tus-Resumable: 1.0.0 required"})
    return None


def _metadata(raw: str) -> dict[str, str]:
    result = {}
    if len(raw) > 8192:
        raise ValueError("Upload-Metadata is too large")
    for item in filter(None, (x.strip() for x in raw.split(","))):
        key, sep, encoded = item.partition(" ")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", key) or not sep or key in result:
            raise ValueError("invalid Upload-Metadata")
        try:
            result[key] = base64.b64decode(encoded, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeError) as exc:
            raise ValueError("invalid Upload-Metadata encoding") from exc
    return result


def _paths(conn, upload_id: str) -> tuple[Path, Path]:
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
        raise ValueError("invalid upload id")
    root = conn.workspace / "uploads" / ".parts"
    return root / f"{upload_id}.json", root / f"{upload_id}.part"


def _lock(upload_id: str) -> asyncio.Lock:
    with _locks_guard:
        return _locks.setdefault(upload_id, asyncio.Lock())


def _save(path: Path, meta: dict) -> None:
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(meta), encoding="utf-8")
    os.replace(temp, path)


def _cleanup_parts(root: Path) -> None:
    """Remove expired sidecars at most once a day per workspace."""
    now = time.time()
    key = str(root)
    with _locks_guard:
        if now - _last_cleanup.get(key, 0) < 86400:
            return
        _last_cleanup[key] = now
    if not root.is_dir():
        return
    for path in root.glob("*.json"):
        if now - path.stat().st_mtime < 7 * 86400:
            continue
        upload_id = path.stem
        if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
            continue
        for suffix in (".json", ".part", ".incoming"):
            (root / (upload_id + suffix)).unlink(missing_ok=True)


def _load(request: Request, upload_id: str):
    gw = _gw()
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
        return None, None, None, _answer(404)
    # The connection id changes after a gateway restart. The upload belongs to the account's
    # workspace; a currently owned connection to that workspace can resume it.
    preferred = request.headers.get("X-Connection-ID", "")
    connections = list(gw.CONNECTIONS.values())[::-1]
    connections.sort(key=lambda conn: conn.id != preferred)
    for conn in connections:
        path, part = _paths(conn, upload_id)
        if not path.is_file():
            continue
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None, None, None, _answer(500, body={"error": "upload metadata is damaged"})
        if conn.owner != meta.get("owner") or (meta.get("app_user_id") is not None
                                               and conn.app_user_id != meta["app_user_id"]):
            continue
        gw._owned_conn(conn.id, request)
        return conn, meta, (path, part), None
    return None, None, None, _answer(404, body={"error": "unknown upload"})


@router.options("")
@router.options("/")
async def options() -> Response:
    return _answer(204, headers={"Tus-Version": _VERSION,
                                 "Tus-Extension": "creation,termination,checksum",
                                 "Tus-Max-Size": str(_MAX_SIZE),
                                 "Tus-Checksum-Algorithm": "sha256",
                                 "Access-Control-Allow-Methods": "OPTIONS,POST,HEAD,PATCH,DELETE",
                                 "Access-Control-Allow-Headers": "Tus-Resumable,Upload-Length,Upload-Metadata,Upload-Offset,Upload-Checksum,Upload-Final-SHA256,X-Connection-ID,Content-Type"})


@router.post("")
@router.post("/")
async def create(request: Request) -> Response:
    if wrong := _require_version(request):
        return wrong
    gw = _gw()
    try:
        fields = _metadata(request.headers.get("Upload-Metadata", ""))
        length = int(request.headers.get("Upload-Length", ""))
    except (ValueError, TypeError):
        return _answer(400, body={"error": "valid Upload-Length and Upload-Metadata required"})
    if length < 0 or length > _MAX_SIZE:
        return _answer(413, body={"error": "upload exceeds Tus-Max-Size"})
    conn = gw._owned_conn(fields.get("connection_id"), request)
    conn.workspace.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(conn.workspace).free < length + 64 * 1024**2:
        upload_log.record(conn.workspace, "refused", name=fields.get("filename"), size=length,
                          error="not enough eyeserver disk space")
        return _answer(507, body={"error": "not enough eyeserver disk space"})
    name = gw.safe_filename(fields.get("filename") or "dataset")
    rel = gw.safe_relpath(fields.get("rel_path") or "")
    folder = gw.safe_relpath(fields.get("folder") or "").split("/")[0]
    if rel and (not folder or rel.split("/")[0] != folder or len(rel.split("/")) < 2):
        return _answer(400, body={"error": "rel_path must be inside the reserved folder"})
    if folder and not (conn.workspace / "uploads" / folder).is_dir():
        upload_log.record(conn.workspace, "refused", name=rel or name, folder=folder, size=length,
                          error="the folder is not reserved on the server")
        return _answer(404, body={"error": "reserve the folder before uploading its files"})
    unpack = fields.get("unpack", "0") == "1"
    client_sha256 = fields.get("client_sha256", "").lower()
    if client_sha256 and not re.fullmatch(r"[0-9a-f]{64}", client_sha256):
        return _answer(400, body={"error": "client_sha256 must be a SHA-256 hex digest"})
    if unpack and not name.lower().endswith((".zip", ".tar", ".tar.gz", ".tgz")):
        return _answer(400, body={"error": "unpack requires a supported archive"})
    upload_id = uuid4().hex
    path, part = _paths(conn, upload_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(_cleanup_parts, path.parent)
    part.touch(exist_ok=False)
    meta = {"connection_id": conn.id, "owner": conn.owner, "app_user_id": conn.app_user_id,
            "length": length,
                 "name": name, "rel_path": rel, "folder": folder, "unpack": unpack,
                 "client_sha256": client_sha256,
                 "offset": 0, "job_id": "", "status": "uploading", "created_at": time.time()}
    _save(path, meta)
    upload_log.record(conn.workspace, "created", upload_id=upload_id, name=rel or name, folder=folder,
                      size=length, unpack=unpack)
    if length == 0:
        await _finish(conn, meta, path, part)
    return _answer(201, headers={"Location": f"/api/tus/{upload_id}", "Upload-Offset": "0"})


@router.get("/{upload_id}")
async def result(upload_id: str, request: Request) -> Response:
    """Authenticated completion details for the console (outside the tus protocol)."""
    conn, meta, _paths_, error = _load(request, upload_id)
    if error:
        return error
    gw = _gw()
    rel = meta.get("rel_path", "")
    path = f"{gw._hpc_uploads_dir(conn)}/{rel}" if gw._uploads_on_hpc(conn) else str(conn.workspace / "uploads" / rel)
    return _answer(200, body={"status": meta["status"], "name": meta["name"],
                              "path": path, "size": meta["length"], "job_id": meta.get("job_id", ""),
                              "peek": meta.get("peek"), "error": meta.get("error", "")})


@router.head("/{upload_id}")
async def head(upload_id: str, request: Request) -> Response:
    if wrong := _require_version(request):
        return wrong
    conn, _meta, paths, error = _load(request, upload_id)
    if error:
        return error
    async with _lock(upload_id):
        if not paths[0].is_file():
            return _answer(404)
        meta = json.loads(paths[0].read_text(encoding="utf-8"))
        if meta["status"] == "failed":
            return _answer(410, body={"error": meta.get("error", "upload failed")})
        if meta["offset"] == meta["length"] and meta["status"] in ("uploading", "finalizing"):
            try:
                await _finish(conn, meta, *paths)
            except (OSError, ValueError) as exc:
                _log_finish_error(conn, upload_id, meta, exc)
                if isinstance(exc, ValueError):
                    _fail_finalization(paths[0], paths[1], meta, exc)
                return _answer(422 if isinstance(exc, ValueError) else 500,
                               body={"error": f"upload finalization failed: {exc}"})
        if 0 < meta["offset"] < meta["length"]:
            upload_log.record(conn.workspace, "resume_asked", upload_id=upload_id,
                              name=meta.get("rel_path") or meta.get("name"), offset=meta["offset"],
                              size=meta["length"])
        headers = {"Upload-Offset": str(meta["offset"]), "Upload-Length": str(meta["length"])}
        if meta.get("job_id"):
            headers["Upload-Staging-Job"] = meta["job_id"]
        return _answer(200, headers=headers)


def _fail_finalization(path: Path, part: Path, meta: dict, exc: ValueError) -> None:
    meta["status"] = "failed"
    meta["error"] = str(exc)
    _save(path, meta)
    part.unlink(missing_ok=True)


def _log_finish_error(conn, upload_id: str, meta: dict, exc: Exception) -> None:
    upload_log.record(conn.workspace, "finalize_failed", upload_id=upload_id,
                      name=meta.get("rel_path") or meta.get("name"), size=meta.get("length"),
                      error=f"{type(exc).__name__}: {exc}",
                      kept=not isinstance(exc, ValueError))   # an OSError keeps the bytes for a retry


async def _finish(conn, meta: dict, path: Path, part: Path) -> None:
    gw = _gw()
    uploads = conn.workspace / "uploads"
    if meta["status"] == "finalizing":
        rel = meta["rel_path"]
        dest = uploads / rel
    else:
        if meta["rel_path"]:
            rel = meta["rel_path"]
            dest = uploads / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                raise ValueError(f"folder entry already exists: {rel}")
        else:
            rel = await asyncio.to_thread(gw._unique_upload_name, conn, uploads, meta["name"])
            dest = uploads / rel
        meta["rel_path"] = rel
        meta["status"] = "finalizing"
        _save(path, meta)
    source = part if part.exists() else dest
    if not source.is_file():
        raise ValueError("completed upload bytes are missing")
    # The HPC3 staging worker checks this digest before copying. Re-reading a 25 GiB file here
    # would hold the final PATCH past the gateway proxy's 15-second response window.
    if meta["client_sha256"] and not gw._uploads_on_hpc(conn):
        def full_digest() -> str:
            digest = hashlib.sha256()
            with source.open("rb") as stream:
                for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                    digest.update(block)
            return digest.hexdigest()
        if await asyncio.to_thread(full_digest) != meta["client_sha256"]:
            raise ValueError("whole-file SHA-256 mismatch")
    if part.exists():
        os.replace(part, dest)
    meta["status"] = "complete"
    meta["name"] = dest.name if not meta["folder"] else rel
    # Skim only regular single files, matching /api/upload. Staging itself never blocks this request.
    peek = await asyncio.to_thread(gw._peek_local_upload, dest) if not meta["folder"] else None
    meta["peek"] = peek
    if gw._uploads_on_hpc(conn):
        job = gw._submit_staging(conn, dest, rel, name=meta["name"],
                                 kind=gw._file_kind(meta["name"]), folder=meta["folder"],
                                 peek=peek, upload_id=path.stem,
                                 client_sha256=meta["client_sha256"], unpack=meta["unpack"])
        meta["job_id"] = job.id
        meta["status"] = "staging"
    elif not meta["folder"]:
        if gw._AUTH_ENABLED and conn.app_user_id:
            gw.auth_routes.record_dataset(conn.app_user_id, dest.name, str(dest),
                                          dest.stat().st_size, gw._file_kind(dest.name))
        conn.emit("success", "upload", f"Uploaded {dest.name} to the server.")
    _save(path, meta)
    seconds = max(0.0, time.time() - float(meta.get("created_at") or time.time()))
    upload_log.record(conn.workspace, "received", upload_id=path.stem, name=rel, folder=meta["folder"],
                      size=meta["length"], seconds=seconds,
                      mb_s=meta["length"] / 1e6 / seconds if seconds >= 1 else None,
                      sha256="browser" if meta["client_sha256"] else "",
                      next="copy to HPC3" if meta["job_id"] else "kept on the server",
                      job_id=meta["job_id"])


@router.patch("/{upload_id}")
async def patch(upload_id: str, request: Request) -> Response:
    if wrong := _require_version(request):
        return wrong
    if request.headers.get("Content-Type", "").split(";")[0].strip() != "application/offset+octet-stream":
        return _answer(415, body={"error": "Content-Type must be application/offset+octet-stream"})
    conn, meta, paths, error = _load(request, upload_id)
    if error:
        return error
    path, part = paths
    try:
        wanted = int(request.headers.get("Upload-Offset", ""))
    except ValueError:
        return _answer(400, body={"error": "Upload-Offset required"})
    async with _lock(upload_id):
        if not path.is_file():
            return _answer(404)
        meta = json.loads(path.read_text(encoding="utf-8"))
        offset = int(meta["offset"])
        if wanted != offset or meta["status"] != "uploading":
            upload_log.record(conn.workspace, "chunk_refused", upload_id=upload_id,
                              name=meta.get("rel_path") or meta.get("name"), offset=offset,
                              error=(f"the browser sent offset {wanted}, the server has {offset}"
                                     if wanted != offset else f"the upload is {meta['status']}"))
            return _answer(409, headers={"Upload-Offset": str(offset)})
        checksum = request.headers.get("Upload-Checksum", "")
        if checksum:
            algorithm, _, encoded = checksum.partition(" ")
            if algorithm != "sha256" or not encoded:
                return _answer(400, body={"error": "only sha256 Upload-Checksum is supported"})
            try:
                expected = base64.b64decode(encoded, validate=True)
            except binascii.Error:
                return _answer(400, body={"error": "invalid Upload-Checksum"})
            if len(expected) != 32:
                return _answer(400, body={"error": "invalid sha256 digest"})
        else:
            expected = None
        # Stage one PATCH in a sidecar. A bad checksum or interrupted request never advances
        # the persisted offset or modifies the assembled file.
        incoming = part.with_suffix(".incoming")
        digest = hashlib.sha256()
        count = 0
        try:
            with incoming.open("wb") as out:
                async for block in request.stream():
                    count += len(block)
                    if offset + count > meta["length"]:
                        return _answer(413, headers={"Upload-Offset": str(offset)})
                    await asyncio.to_thread(_write_chunk, out, digest, block)
            if expected is not None and digest.digest() != expected:
                upload_log.record(conn.workspace, "chunk_refused", upload_id=upload_id,
                                  name=meta.get("rel_path") or meta.get("name"), offset=offset,
                                  bytes=count, error="chunk checksum mismatch (SHA-256)")
                return _answer(460, headers={"Upload-Offset": str(offset)},
                               body={"error": "chunk checksum mismatch"})
            final_sha = request.headers.get("Upload-Final-SHA256", "").lower()
            if final_sha:
                if not re.fullmatch(r"[0-9a-f]{64}", final_sha) or offset + count != meta["length"]:
                    return _answer(400, body={"error": "Upload-Final-SHA256 requires the final PATCH and a hex digest"})
                if meta["client_sha256"] and meta["client_sha256"] != final_sha:
                    return _answer(409, body={"error": "whole-file digest changed"})
                meta["client_sha256"] = final_sha
            await asyncio.to_thread(_append_chunk, part, incoming)
            meta["offset"] = offset + count
            _save(path, meta)
            if meta["offset"] == meta["length"]:
                try:
                    await _finish(conn, meta, path, part)
                except (OSError, ValueError) as exc:
                    _log_finish_error(conn, upload_id, meta, exc)
                    if isinstance(exc, ValueError):
                        _fail_finalization(path, part, meta, exc)
                    return _answer(422 if isinstance(exc, ValueError) else 500,
                                   headers={"Upload-Offset": str(meta["offset"])},
                                   body={"error": f"upload received but staging failed: {exc}"})
            headers = {"Upload-Offset": str(meta["offset"])}
            if meta.get("job_id"):
                headers["Upload-Staging-Job"] = meta["job_id"]
            return _answer(204, headers=headers)
        finally:
            incoming.unlink(missing_ok=True)


def _write_chunk(out, digest, block: bytes) -> None:
    out.write(block)
    digest.update(block)


def _append_chunk(part: Path, incoming: Path) -> None:
    with part.open("ab") as out, incoming.open("rb") as source:
        shutil.copyfileobj(source, out, 1024 * 1024)


@router.delete("/{upload_id}")
async def delete(upload_id: str, request: Request) -> Response:
    if wrong := _require_version(request):
        return wrong
    conn, _meta, paths, error = _load(request, upload_id)
    if error:
        return error
    path, part = paths
    async with _lock(upload_id):
        if not path.is_file():
            return _answer(404)
        meta = json.loads(path.read_text(encoding="utf-8"))
        if meta.get("job_id"):
            _gw()._staging_queue(conn).cancel(meta["job_id"])
        else:
            part.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        upload_log.record(conn.workspace, "discarded", upload_id=upload_id,
                          name=meta.get("rel_path") or meta.get("name"), offset=meta.get("offset"),
                          size=meta.get("length"), job_id=meta.get("job_id"))
    with _locks_guard:
        _locks.pop(upload_id, None)
    return _answer(204)
