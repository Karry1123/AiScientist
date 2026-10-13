"""The short report: asked for, or automatic when QC is all a run did (gateway/app.py).

2026-10-11, testing CellQC alone: the manuscript writer must fill an Abstract, Study question,
Results, Discussion, Limitations, Conclusion and Methods — several pages around one QC table."""
from __future__ import annotations

import json
from pathlib import Path

from aiscientist.agents.scope import RunScope
from aiscientist.gateway.app import (
    _build_brief_report, _insert_verification_note, _tools_that_ran, _wants_brief_report)

_QC = {"status": "ok", "cells_before": 18681, "cells_after": 16750, "genes_after": 33696,
       "thresholds": {"min_counts": 500, "min_features": 300, "max_pct_mt": 10},
       "warnings": ["Sample1_WT: the two doublet callers agree poorly (kappa 0.07)."],
       "counts_layer": "ambient-corrected integer UMI counts from CellQC (soupx)",
       "libraries": [{"sample": "Sample1_WT", "cells_cellranger": 18681, "cells_after_filter": 18011,
                      "cells_after_doublets": 16750, "frac_retained": 0.8966, "doublets_removed": 1261,
                      "doublet_kappa": 0.0731, "ambient_soupx_contamination_mean": 0.025,
                      "median_pct_mt": 0.7509,
                      "removed_only_by": {"min_counts": 4, "min_features": 27, "max_pct_mt": 541,
                                          "several_thresholds": 98}}]}


class _Result:
    def __init__(self, steps):
        self.rounds = [{"step": "**QC** — `run_cellqc`", "verdict": {"verdict": "accept", "score": 0.9},
                        "scientist_result": {"steps": steps}}]
        self.agenda = ["**QC** — `run_cellqc`"]


def _art(tmp_path: Path) -> Path:
    art = tmp_path / "artifacts"
    for sub in ("data", "tables", "figures", "report"):
        (art / sub).mkdir(parents=True)
    (art / "data" / "dataset_results.json").write_text(json.dumps({
        "dataset_path": "/x/Sample1_WT/filtered_feature_bc_matrix.h5", "cells": 18681, "genes": 33696,
        "cellranger_layout": {"n_libraries": 1, "all_have_bam": True,
                              "libraries": [{"sample": "Sample1_WT", "bam": True}]}}))
    (art / "data" / "result_dataset.json").write_text(json.dumps({
        "name": "Sample1_WT.processed.20261011-1200.abc.h5ad", "n_cells": 16750, "n_genes": 33696,
        "counts_layer": "ambient-corrected integer UMI counts from CellQC (soupx)",
        "obs_columns": ["sampleid"], "obsm": [], "location": "hpc3", "kept_days": 3}))
    (art / "tables" / "cells.csv").write_text("barcode\n")
    (art / "figures" / "violin_cellqc_after_qc.png").write_bytes(b"png")
    return art


_QC_STEPS = [{"tool": "inspect_dataset", "ok": True, "result": {"status": "ok"}},
             {"tool": "run_cellqc", "ok": True, "args": {"species": "mouse"}, "result": _QC},
             {"tool": "run_code", "ok": True, "result": {"status": "ok", "stdout": "x"}},
             {"tool": "finish", "ok": True, "result": {}}]


def test_a_run_that_only_did_qc_gets_the_short_report_unasked():
    assert _tools_that_ran(_Result(_QC_STEPS))[:2] == ["inspect_dataset", "run_cellqc"]
    assert _wants_brief_report(None, _Result(_QC_STEPS))
    assert _wants_brief_report(RunScope(), _Result(_QC_STEPS))
    more = _QC_STEPS + [{"tool": "run_clustering", "ok": True, "result": {"status": "ok"}}]
    assert not _wants_brief_report(RunScope(), _Result(more))
    assert _wants_brief_report(RunScope(brief_report=True), _Result(more))     # asked for
    failed = _QC_STEPS + [{"tool": "run_de", "ok": False, "result": {"status": "error"}}]
    assert _wants_brief_report(None, _Result(failed))          # nothing beyond QC succeeded
    assert not _wants_brief_report(None, _Result([]))          # nothing ran at all: not "QC only"


def test_the_short_report_is_the_tools_own_numbers_and_nothing_else(tmp_path):
    art = _art(tmp_path)
    md = _build_brief_report(art, "只做 QC，报告尽量简短", _Result(_QC_STEPS),
                             RunScope(keep=("run_cellqc",), brief_report=True))
    assert md.startswith("# Quality-control summary: Sample1_WT")
    assert "As requested (only run_cellqc; brief report), this run did quality control only" in md
    assert "| Sample1_WT | 18,681 | 18,011 | 16,750 | 89.7% | 2.5% | 0.75 |" in md
    assert "4 too few counts, 27 too few genes, 541 too much mitochondrial RNA, 98 several of these" in md
    assert "doublets removed: 1,261 (the two doublet callers agree at kappa 0.07)" in md
    assert "agree poorly" in md                                  # the tool's warning is kept
    assert "Sample1_WT.processed.20261011-1200.abc.h5ad" in md   # the result matrix is named
    assert "figures/violin_cellqc_after_qc.png" in md and "## What was run" in md
    for heading in ("## Abstract", "## Discussion", "## Methods", "## References", "## Limitations"):
        assert heading not in md
    assert len(md.split()) < 700
    # The verification summary still goes on the end.
    noted = _insert_verification_note(md, {"numbers_checked": 9, "numbers_exact": 9})
    assert noted.rstrip().endswith("could not be traced.") and "## Verification" in noted


def test_a_short_report_of_a_longer_run_shows_its_cell_types(tmp_path):
    art = _art(tmp_path)
    (art / "tables" / "celltype_composition.csv").write_text(
        "cell_type,n_cells,pct\nRod photoreceptor,13520,80.72\nUnassigned,741,4.42\n")
    steps = _QC_STEPS + [{"tool": "run_marker_annotation", "ok": True, "result": {"status": "ok"}}]
    md = _build_brief_report(art, "QC and annotate, brief report", _Result(steps),
                             RunScope(brief_report=True))
    assert md.startswith("# Analysis summary: Sample1_WT")
    assert "| Rod photoreceptor | 13,520 | 80.72 |" in md and "this is the short report" in md
