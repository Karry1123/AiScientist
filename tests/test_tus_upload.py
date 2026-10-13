"""End-to-end tus creation, offset recovery, checksum and staging handoff."""
import asyncio
import base64
import hashlib
import json
import os
from types import SimpleNamespace

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    pytest.importorskip("httpx")
    monkeypatch.setenv("AISCIENTIST_DATABASE_URL", f"sqlite:///{(tmp_path / 'users.db').as_posix()}")
    from fastapi.testclient import TestClient
    from aiscientist.gateway import app as gw
    from aiscientist.gateway.mock_host import MockExecutor
    from aiscientist.gateway.settings import HPCSettings

    monkeypatch.setattr(gw, "_AUTH_ENABLED", False)
    loop = asyncio.new_event_loop()
    conn = gw.Connection(HPCSettings(), mock=True, loop=loop, username="tester")
    conn.workspace = tmp_path / "workspace"
    conn.settings.uploads_on_hpc = False
    conn.executor = MockExecutor(username="tester")
    gw.CONNECTIONS[conn.id] = conn
    try:
        yield TestClient(gw.app), conn, gw
    finally:
        queue = gw._STAGING.pop(conn.owner, None)
        if queue:
            for job in queue.jobs():     # a worker left running would use the next test's connection
                queue.wait(job.id, 5.0)
            queue.shutdown()
        gw.CONNECTIONS.pop(conn.id, None)
        loop.close()


def _metadata(**values):
    return ",".join(f"{key} {base64.b64encode(str(value).encode()).decode()}"
                    for key, value in values.items())


def _create(client, conn, size, **fields):
    return client.post("/api/tus", headers={"Tus-Resumable": "1.0.0", "Upload-Length": str(size),
                                             "Upload-Metadata": _metadata(connection_id=conn.id,
                                                                          filename="data.csv", **fields)})


def _patch(client, url, offset, data, digest=None, final_sha=None):
    headers = {"Tus-Resumable": "1.0.0", "Content-Type": "application/offset+octet-stream",
               "Upload-Offset": str(offset)}
    if digest is not None:
        headers["Upload-Checksum"] = "sha256 " + base64.b64encode(digest).decode()
    if final_sha is not None:
        headers["Upload-Final-SHA256"] = final_sha
    return client.patch(url, headers=headers, content=data)


def test_resume_checksum_and_local_result(env):
    client, conn, _gw = env
    payload = b"gene,count\nRHO,42\n"
    created = _create(client, conn, len(payload), client_sha256=hashlib.sha256(payload).hexdigest())
    assert created.status_code == 201
    url = created.headers["location"]
    assert url.startswith("/api/tus/")
    assert _patch(client, url, 0, payload[:5], b"wrong".ljust(32, b"!")).status_code == 460
    assert client.head(url, headers={"Tus-Resumable": "1.0.0"}).headers["upload-offset"] == "0"
    first = payload[:5]
    assert _patch(client, url, 0, first, hashlib.sha256(first).digest()).status_code == 204
    assert _patch(client, url, 0, first).status_code == 409
    assert client.head(url, headers={"Tus-Resumable": "1.0.0"}).headers["upload-offset"] == "5"
    rest = payload[5:]
    assert _patch(client, url, 5, rest, hashlib.sha256(rest).digest()).status_code == 204
    result = client.get(url).json()
    assert result["status"] == "complete" and result["size"] == len(payload)
    assert (conn.workspace / "uploads" / result["name"]).read_bytes() == payload
    assert client.head(url, headers={"Tus-Resumable": "1.0.0"}).headers["upload-offset"] == str(len(payload))


def test_folder_path_and_termination(env):
    client, conn, _gw = env
    folder = client.post("/api/upload/reserve-folder", json={"connection_id": conn.id,
                                                               "name": "study"}).json()["folder"]
    bad = _create(client, conn, 3, folder=folder, rel_path="wrong/a.csv")
    assert bad.status_code == 400
    made = _create(client, conn, 3, folder=folder, rel_path=f"{folder}/nested/a.csv")
    url = made.headers["location"]
    assert _patch(client, url, 0, b"abc", hashlib.sha256(b"abc").digest()).status_code == 204
    assert (conn.workspace / "uploads" / folder / "nested" / "a.csv").read_bytes() == b"abc"
    unfinished = _create(client, conn, 9).headers["location"]
    assert client.delete(unfinished, headers={"Tus-Resumable": "1.0.0"}).status_code == 204
    assert client.head(unfinished, headers={"Tus-Resumable": "1.0.0"}).status_code == 404


def test_head_recovers_a_finalization_interrupted_after_rename(env):
    client, conn, _gw = env
    made = _create(client, conn, 3)
    upload_id = made.headers["location"].rsplit("/", 1)[-1]
    meta_path = conn.workspace / "uploads" / ".parts" / f"{upload_id}.json"
    part_path = meta_path.with_suffix(".part")
    dest = conn.workspace / "uploads" / "recovered.csv"
    part_path.write_bytes(b"abc")
    meta = json.loads(meta_path.read_text())
    meta.update(offset=3, status="finalizing", rel_path=dest.name)
    meta_path.write_text(json.dumps(meta))
    os.replace(part_path, dest)
    response = client.head(made.headers["location"], headers={"Tus-Resumable": "1.0.0"})
    assert response.status_code == 200 and response.headers["upload-offset"] == "3"
    assert client.get(made.headers["location"]).json()["status"] == "complete"
    assert dest.read_bytes() == b"abc"


def test_staging_handoff(env):
    client, conn, gw = env
    conn.settings.uploads_on_hpc = True
    made = _create(client, conn, 3)
    url = made.headers["location"]
    full_sha = hashlib.sha256(b"abc").hexdigest()
    response = _patch(client, url, 0, b"abc", hashlib.sha256(b"abc").digest(), final_sha=full_sha)
    assert response.status_code == 204 and response.headers["upload-staging-job"]
    result = client.get(url).json()
    assert result["job_id"] == response.headers["upload-staging-job"]
    assert result["status"] == "staging"
    assert gw._staging_queue(conn).get(result["job_id"]).client_sha256 == full_sha
    assert gw._staging_queue(conn).wait(result["job_id"], 5).state == "done"


def test_hpc_final_patch_does_not_hash_the_whole_file_in_request(env, monkeypatch):
    client, conn, gw = env
    from aiscientist.gateway import tus_upload

    conn.settings.uploads_on_hpc = True
    monkeypatch.setattr(gw, "_submit_staging", lambda *args, **kwargs: SimpleNamespace(id="queued"))
    monkeypatch.setattr(gw, "_peek_local_upload", lambda path: None)
    real_sha256 = hashlib.sha256
    calls = []

    def counted_sha256(*args, **kwargs):
        calls.append(1)
        return real_sha256(*args, **kwargs)

    monkeypatch.setattr(tus_upload.hashlib, "sha256", counted_sha256)
    url = _create(client, conn, 3).headers["location"]
    response = _patch(client, url, 0, b"abc", final_sha=real_sha256(b"abc").hexdigest())
    assert response.status_code == 204 and response.headers["upload-staging-job"] == "queued"
    assert len(calls) == 1  # PATCH chunk; no second pass over the complete file


def test_new_connection_to_same_workspace_resumes_upload(env):
    client, conn, gw = env
    url = _create(client, conn, 6).headers["location"]
    assert _patch(client, url, 0, b"abc", hashlib.sha256(b"abc").digest()).status_code == 204
    gw.CONNECTIONS.pop(conn.id)
    next_conn = gw.Connection(conn.settings, mock=True, loop=conn.loop, username="tester")
    next_conn.workspace = conn.workspace
    next_conn.executor = conn.executor
    gw.CONNECTIONS[next_conn.id] = next_conn
    try:
        assert client.head(url, headers={"Tus-Resumable": "1.0.0"}).headers["upload-offset"] == "3"
        assert _patch(client, url, 3, b"def", hashlib.sha256(b"def").digest()).status_code == 204
        assert (conn.workspace / "uploads" / "data.csv").read_bytes() == b"abcdef"
    finally:
        gw.CONNECTIONS.pop(next_conn.id, None)


def test_head_reloads_metadata_inside_lock(env, monkeypatch):
    client, conn, _gw = env
    from aiscientist.gateway import tus_upload

    url = _create(client, conn, 3).headers["location"]
    assert _patch(client, url, 0, b"abc").status_code == 204
    real_load = tus_upload._load

    def stale_load(request, upload_id):
        found_conn, meta, paths, error = real_load(request, upload_id)
        if meta:
            meta = {**meta, "status": "uploading"}
        return found_conn, meta, paths, error

    monkeypatch.setattr(tus_upload, "_load", stale_load)
    assert client.head(url, headers={"Tus-Resumable": "1.0.0"}).status_code == 200
    assert (conn.workspace / "uploads" / "data.csv").read_bytes() == b"abc"


def test_delete_reloads_staging_job_inside_lock(env, monkeypatch):
    client, conn, gw = env
    from aiscientist.gateway import tus_upload

    conn.settings.uploads_on_hpc = True
    url = _create(client, conn, 3).headers["location"]
    response = _patch(client, url, 0, b"abc")
    job_id = response.headers["upload-staging-job"]
    queue = gw._staging_queue(conn)
    real_cancel = queue.cancel
    cancelled = []

    def record_cancel(value):
        cancelled.append(value)
        return real_cancel(value)

    monkeypatch.setattr(queue, "cancel", record_cancel)
    real_load = tus_upload._load

    def stale_load(request, upload_id):
        found_conn, meta, paths, error = real_load(request, upload_id)
        if meta:
            meta = {**meta, "job_id": "", "status": "uploading"}
        return found_conn, meta, paths, error

    monkeypatch.setattr(tus_upload, "_load", stale_load)
    assert client.delete(url, headers={"Tus-Resumable": "1.0.0"}).status_code == 204
    assert cancelled == [job_id]


def test_terminal_finalization_error_is_not_left_pending(env):
    client, conn, _gw = env
    folder = client.post("/api/upload/reserve-folder", json={"connection_id": conn.id,
                                                               "name": "study"}).json()["folder"]
    dest = conn.workspace / "uploads" / folder / "a.csv"
    dest.write_bytes(b"existing")
    url = _create(client, conn, 3, folder=folder, rel_path=f"{folder}/a.csv").headers["location"]
    assert _patch(client, url, 0, b"abc").status_code == 422
    assert client.get(url).json()["status"] == "failed"
    assert client.head(url, headers={"Tus-Resumable": "1.0.0"}).status_code == 410
    assert not (conn.workspace / "uploads" / ".parts" / f"{url.rsplit('/', 1)[-1]}.part").exists()


def test_bad_version_and_unknown_connection(env):
    client, conn, _gw = env
    assert client.options("/api/tus").headers["tus-extension"] == "creation,termination,checksum"
    assert client.post("/api/tus", headers={"Upload-Length": "3"}).status_code == 412
    bad = client.post("/api/tus", headers={"Tus-Resumable": "1.0.0", "Upload-Length": "3",
                                           "Upload-Metadata": _metadata(connection_id="not-a-connection")})
    assert bad.status_code == 404


def test_a_second_folder_of_the_same_name_gets_its_own_folder(env):
    client, conn, _gw = env
    first = client.post("/api/upload/reserve-folder", json={"connection_id": conn.id, "name": "study"}).json()["folder"]
    second = client.post("/api/upload/reserve-folder", json={"connection_id": conn.id, "name": "study"}).json()["folder"]
    assert first == "study" and second == "study_2"
    made = _create(client, conn, 3, folder=second, rel_path=f"{second}/nested/a.csv")
    assert made.status_code == 201, made.text
    assert _patch(client, made.headers["location"], 0, b"abc", hashlib.sha256(b"abc").digest()).status_code == 204
    assert (conn.workspace / "uploads" / second / "nested" / "a.csv").read_bytes() == b"abc"
    assert not any((conn.workspace / "uploads" / first).rglob("*.csv"))      # nothing merged into the first
