"""Per-call cache accounting, without a live model or the application's database."""

from types import SimpleNamespace

import pytest

from aiscientist.gateway.token_usage import input_token_usage


@pytest.mark.parametrize("usage,cached,uncached,writes", [
    ({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 80}}, 80, 20, 0),
    ({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 0}}, 0, 100, 0),
    ({"prompt_tokens": 100, "input_tokens": 10, "cache_read_input_tokens": 70,
      "cache_creation_input_tokens": 20}, 70, 10, 20),
    ({"prompt_tokens": 100, "prompt_cache_hit_tokens": 40}, 40, 60, 0),
    ({"prompt_tokens": 100}, 0, 100, 0),
    (None, 0, 0, 0),
])
def test_input_token_split(usage, cached, uncached, writes):
    assert input_token_usage(usage) == {"cache_read_tokens": cached, "input_tokens": uncached,
                                      "cache_write_tokens": writes}


@pytest.fixture
def usage_db(tmp_path, monkeypatch):
    pytest.importorskip("sqlalchemy")
    from aiscientist.gateway import db

    monkeypatch.setenv("AISCIENTIST_DATABASE_URL", f"sqlite:///{(tmp_path / 'usage.db').as_posix()}")
    monkeypatch.setenv("AISCIENTIST_STATE_DIR", str(tmp_path / "state"))
    db.reset()
    yield db
    db.reset()


def test_usage_record_and_public_fields(usage_db):
    from sqlalchemy import select
    from aiscientist.gateway import auth_routes, models

    usage_db.init_db()
    for model, cached in (("model-a", 80), ("model-b", 0)):
        auth_routes.record_llm_call(run_id="run", user_id=None, role="scientist", model=model,
                                    endpoint="local", remote=False, max_tokens=20, duration_ms=1,
                                    usage={"prompt_tokens": 100, "completion_tokens": 5,
                                           "prompt_tokens_details": {"cached_tokens": cached}})
    with usage_db.session_scope() as s:
        rows = s.scalars(select(models.LlmCall).order_by(models.LlmCall.id)).all()
        assert [(r.model, r.public()["cache_read_tokens"], r.public()["input_tokens"])
                for r in rows] == [("model-a", 80, 20), ("model-b", 0, 100)]
        assert [r.total_tokens for r in rows] == [105, 105]


def test_cache_creation_is_recorded_separately_from_new_input(usage_db):
    from sqlalchemy import select
    from aiscientist.gateway import auth_routes, models

    usage_db.init_db()
    usage = {"prompt_tokens": 100, "input_tokens": 10, "cache_read_input_tokens": 70,
             "cache_creation_input_tokens": 20, "cache_creation_1h_input_tokens": 5}
    auth_routes.record_llm_call(run_id="run", user_id=None, role="writer", model="claude",
                                endpoint="api", remote=True, max_tokens=20, duration_ms=1, usage=usage)
    with usage_db.session_scope() as s:
        row = s.scalars(select(models.LlmCall)).one()
        assert (row.input_tokens, row.cache_read_tokens, row.cache_write_tokens) == (10, 70, 20)


def test_run_log_records_split_even_if_database_write_fails(monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("sqlalchemy")
    from aiscientist.gateway import app, auth_routes

    events = []
    conn = SimpleNamespace(emit=lambda *args, **kwargs: events.append((args, kwargs)),
                           active_run=SimpleNamespace(run_id="run"), app_user_id=None)

    def fail(**kwargs):
        raise RuntimeError("database offline")

    monkeypatch.setattr(auth_routes, "record_llm_call", fail)
    app._record_lab_usage(conn, role="writer", model="model-a", endpoint="local", remote=False,
                          usage={"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 80}},
                          max_tokens=20, duration_ms=1)
    assert len(events) == 1
    args, kwargs = events[0]
    assert args[:2] == ("info", "llm_usage")
    assert "model-a" in args[2] and "cached input 80" in args[2] and "new input 20" in args[2]
    assert kwargs["detail"]["usage"]["input_tokens"] == 20


def test_scientist_retry_records_both_model_calls(monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("sqlalchemy")
    from aiscientist.gateway import app, auth_routes, vllm_client

    for name in ("AISCIENTIST_LLM_BASE_URL", "AISCIENTIST_LAB_LLM_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    records, events = [], []
    monkeypatch.setattr(auth_routes, "record_llm_call", lambda **kwargs: records.append(kwargs))
    replies = iter([
        {"content": "", "tool_calls": [], "finish_reason": "length",
         "usage": {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 0}}},
        {"content": "answer", "tool_calls": [], "finish_reason": "stop",
         "usage": {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 80}}},
    ])
    monkeypatch.setattr(vllm_client, "chat_tools", lambda *args, **kwargs: next(replies))
    conn = SimpleNamespace(selected_model="model-a", tunnel_port=1234, mock=True,
                           active_run=SimpleNamespace(run_id="run"),
                           emit=lambda *args, **kwargs: events.append((args, kwargs)))
    llm = app._lab_llm(conn)
    assert llm.scientist_chat([{"role": "user", "content": "question"}], [])["content"] == "answer"
    assert len(records) == 2
    assert all(r["run_id"] == "run" and r["model"] == "model-a" and r["role"] == "scientist" for r in records)
    counts = [kwargs["detail"]["usage"] for args, kwargs in events if args[1] == "llm_usage"]
    assert [(u["cache_read_tokens"], u["input_tokens"]) for u in counts] == [(0, 100), (80, 20)]


def test_tool_chat_preserves_provider_cache_usage(monkeypatch):
    import io
    import json
    from aiscientist.gateway import vllm_client

    usage = {"prompt_tokens": 100, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 80}}
    body = {"choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}], "usage": usage}
    monkeypatch.setattr(vllm_client.urllib.request, "urlopen",
                        lambda *args, **kwargs: io.BytesIO(json.dumps(body).encode()))
    assert vllm_client.chat_tools(1234, "model-a", [], [])["usage"] == dict(usage, model="model-a")


@pytest.mark.parametrize("finished", [False, True])
def test_usage_log_and_record_follow_the_billed_run(monkeypatch, finished):
    pytest.importorskip("fastapi")
    pytest.importorskip("sqlalchemy")
    from aiscientist.gateway import app, auth_routes

    events, records = [], []
    active = SimpleNamespace(run_id="other", conversation_id="other-chat")
    bound = SimpleNamespace(run_id="billed", conversation_id="billed-chat")
    conn = SimpleNamespace(active_run=active, app_user_id=None,
                           emit=lambda *args, **kwargs: events.append(kwargs))
    monkeypatch.setattr(auth_routes, "record_llm_call", lambda **kwargs: records.append(kwargs))
    token = app._USAGE_RUN.set("billed" if finished else None)
    try:
        app._record_lab_usage(conn, bound_run=None if finished else bound,
                              role="writer", model="model-a", endpoint="local", remote=False,
                              usage={"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 80}},
                              max_tokens=20, duration_ms=1)
    finally:
        app._USAGE_RUN.reset(token)
    assert records[0]["run_id"] == events[0]["run"].run_id == "billed"
