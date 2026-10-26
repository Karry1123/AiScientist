"""Server-path binding feedback and chat ownership, run under node."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "frontend" / "console" / "app.js"

HARNESS = r"""
const vm = require("vm");
const src = require("fs").readFileSync(0, "utf8");
const messages = [], posts = [];
const chats = new Map([ ["A", { serverBoundTyped: new Set() }], ["B", { serverBoundTyped: new Set() }] ]);
const state = { activeId: "A", connectionId: "conn" };
const sandbox = {
  state, JSON, String, Error,
  chatOf: (sid) => chats.get(sid),
  toast: (text) => messages.push(text),
  setUploadProgress() {}, renderServerPathOffer() {},
  isFolderKind: () => false, addDataset() {},
  confirm: () => __CONFIRM__,
  fetch: async (url) => {
    posts.push(url);
    if (url.endsWith("/check")) {
      state.activeId = "B"; // leaving the chat while its check is in flight
      return { ok: true, json: async () => ({ confirm: true, message: "Copy 50 GB" }) };
    }
    return { ok: true, json: async () => ({ name: "sample", path: "/resolved/sample", kind: "file" }) };
  },
};
const api = vm.runInNewContext(src + "\n;({ bindServerPath })", sandbox);
(async () => {
  const result = await api.bindServerPath("/server/sample");
  console.log(JSON.stringify({ result, messages, posts,
    a: [...chats.get("A").serverBoundTyped], b: [...chats.get("B").serverBoundTyped] }));
})().catch((err) => { console.error(err.stack); process.exit(1); });
"""


def _run(confirmed: bool) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = APP_JS.read_text(encoding="utf-8")
    block = source[source.index("async function bindServerPath("):source.index("// Mark/unmark one bound file")]
    done = subprocess.run(
        [node, "-e", HARNESS.replace("__CONFIRM__", str(confirmed).lower())],
        input=block, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_large_copy_cancel_is_visible_and_does_not_bind():
    out = _run(False)
    assert out["result"] is False
    assert out["messages"] == ["Bind cancelled."]
    assert out["posts"] == ["/api/server-data/check"]
    assert out["a"] == out["b"] == []


def test_binding_after_chat_switch_remembers_the_originating_chat():
    out = _run(True)
    assert out["result"] is True
    assert out["a"] == ["/server/sample"]
    assert out["b"] == []
