"""Each species result has its own Save to my data control."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "frontend" / "console" / "app.js"


def _render(row: dict) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = APP_JS.read_text(encoding="utf-8")
    block = source[source.index("function resultHandoffHtml("):source.index("async function showResultHandoff(")]
    script = r"""
const vm = require("vm");
const src = require("fs").readFileSync(0, "utf8");
const api = vm.runInNewContext(src + "\n;({ resultHandoffHtml })", {
  escapeHtml: (s) => String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;"),
  icon: () => "",
});
console.log(api.resultHandoffHtml("run", __ROW__));
""".replace("__ROW__", json.dumps(row))
    done = subprocess.run([node, "-e", script], input=block, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_each_species_result_has_a_separate_save_control():
    html = _render({"name": "mouse.h5ad", "datasets": [
        {"name": "mouse.h5ad", "what": "dataset:mouse", "n_cells": 12, "n_genes": 5},
        {"name": "human.h5ad", "what": "dataset:human", "n_cells": 8, "n_genes": 7},
    ]})
    assert html.count("mouse.h5ad") == 1
    assert html.count("human.h5ad") == 1
    assert 'data-what="dataset:mouse"' in html
    assert 'data-what="dataset:human"' in html
    assert html.count("data-save-result=") == 2


def test_legacy_single_dataset_keeps_its_save_control():
    html = _render({"name": "legacy.h5ad", "n_cells": 12, "n_genes": 5})
    assert "legacy.h5ad" in html
    assert 'data-what="dataset"' in html
    assert html.count("data-save-result=") == 1
