"""Run the atlas skill on tiny precalculated profiles and the DE tables the platform's tools leave.

The template reads tables off disk, so these tests write the exact files run_pseudobulk_de and
run_de leave behind — including the label beside them that says whether a table is a condition
contrast and whether its p-values are inferential — and one test drives the real run_de.
"""
from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest

pd = pytest.importorskip("pandas")
np = pytest.importorskip("numpy")
pytest.importorskip("matplotlib")

from aiscientist.agents.skills import SKILLS, _skills_dir  # noqa: E402


SCRIPT = _skills_dir() / "atlas_disease_genes" / "reference.py"
RETNET = Path(__file__).resolve().parents[1] / "src/aiscientist/tools/gene_panels/ird_retnet.txt"

DE_ROWS = [
    {"group": "rod", "gene": "RHO", "log2fc": 1.5, "pval": 0.0001, "pval_adj": 0.001, "score": 5.0},
    {"group": "rod", "gene": "GENE2", "log2fc": 0.8, "pval": 0.005, "pval_adj": 0.02, "score": 3.0},
    {"group": "rod", "gene": "GENE3", "log2fc": 0.1, "pval": 0.4, "pval_adj": 0.6, "score": 0.2},
    {"group": "unmatched", "gene": "RHO", "log2fc": 0.9, "pval": 0.01, "pval_adj": 0.03, "score": 2.0},
]


def _inputs(tmp_path: Path, monkeypatch, *, source: str = "pseudobulk",
            rows: list[dict] | None = None) -> tuple[Path, Path]:
    """Atlas + an upstream DE result under key ``cell_type``, labelled as ``source`` labels it."""
    work, art = tmp_path / "work", tmp_path / "artifacts"
    (work / "atlas").mkdir(parents=True)
    tables = art / "tables"
    tables.mkdir(parents=True)
    pd.DataFrame([
        {"cell_type": ct, "gene": gene, "mean_expression": mean,
         "disease_log2fc": atlas_fc, "disease_pval_adj": atlas_q}
        for ct, vals in {
            "rod": [("RHO", 50, 1.1, 0.01), ("GENE2", 2, -0.6, 0.03), ("GENE3", 1, 0.0, 1.0)],
            "cone": [("RHO", 1, 1.1, 0.01), ("GENE2", 12, -0.6, 0.03), ("GENE3", 2, 0.0, 1.0)],
            "bipolar": [("RHO", 1, 1.1, 0.01), ("GENE2", 1, -0.6, 0.03), ("GENE3", 8, 0.0, 1.0)],
        }.items() for gene, mean, atlas_fc, atlas_q in vals
    ]).to_csv(work / "atlas" / "precalculated_atlas.csv", index=False)
    pd.DataFrame(rows or DE_ROWS).to_csv(tables / "de_cell_type_all.csv", index=False)
    (tables / "de_cell_type_universe.txt").write_text("RHO\nGENE2\nGENE3\n", encoding="utf-8")
    if source == "pseudobulk":
        # run_pseudobulk_de writes the same rows under both names.
        (tables / "pseudobulk_all.csv").write_bytes((tables / "de_cell_type_all.csv").read_bytes())
    elif source in ("run_de", "run_de_exploratory", "run_de_markers"):
        (tables / "de_cell_type_inference.json").write_text(json.dumps({
            "tool": "run_de", "mode": "markers" if source == "run_de_markers" else "contrast",
            "inference": "exploratory_ranking" if source == "run_de_exploratory" else None,
            "groupby": "sampleid", "stratify_by": "cell_type",
            "reference": "rest" if source == "run_de_markers" else "WT"}))
    monkeypatch.setenv("AISCIENTIST_WORK", str(work))
    monkeypatch.setenv("AISCIENTIST_ARTIFACTS", str(art))
    return work, art


def _run(capsys=None, **overrides) -> dict:
    module = runpy.run_path(str(SCRIPT), run_name="skill_module")
    cfg = dict(module["CONFIG"], de_key="cell_type", atlas_path="atlas/precalculated_atlas.csv")
    cfg.update(overrides)
    summary = module["main"](cfg)
    if capsys is not None:
        assert json.loads(capsys.readouterr().out.strip().splitlines()[-1]) == json.loads(
            json.dumps(summary, default=str))
    return summary


def test_skill_discovery_and_outputs(tmp_path, monkeypatch, capsys):
    assert SKILLS["atlas_disease_genes"].summary.startswith("Reference template —")
    _, art = _inputs(tmp_path, monkeypatch)
    de_tables_before = sorted(p.name for p in (art / "tables").glob("de_*"))
    summary = _run(capsys, positive_control_path=str(RETNET))
    assert summary["status"] == "ok"
    assert summary["de_source"]["tool"] == "run_pseudobulk_de"
    assert summary["p_values_inferential"] is True
    assert summary["overlap_genes"] == 3
    assert summary["n_selected"] == 2
    assert "unmatched" in summary["skipped_groups"]
    assert summary["positive_control"]["available"]
    assert summary["positive_control"]["top_k_hits"] >= 1
    candidates = pd.read_csv(art / "tables" / "atlas" / "candidates.csv")
    rho = candidates.loc[candidates.gene == "RHO"].iloc[0]
    assert rho["selected"]
    assert "DE FDR/effect gate passed" in rho["why_selected"]
    assert "atlas disease direction agrees" in rho["why_selected"]
    assert rho["candidate_score"] > candidates.loc[candidates.gene == "GENE3", "candidate_score"].iloc[0]
    assert (art / "figures" / "atlas" / "top_candidates.png").stat().st_size > 0
    assert (art / "tables" / "atlas" / "positive_control.csv").exists()
    assert (art / "tables" / "atlas" / "skipped_groups.csv").exists()
    # No de_* table of its own: run_enrichment's discovery must keep finding the upstream contrast.
    assert sorted(p.name for p in (art / "tables").glob("de_*")) == de_tables_before
    assert summary["enrichment_groupby"] == "cell_type"
    assert _run()["n_selected"] == 2       # a repeat run sees the same inputs


def test_de_key_is_required_and_lists_what_exists(tmp_path, monkeypatch):
    _inputs(tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match=r"de_key.*\['cell_type'\]"):
        _run(de_key=None)


def test_a_marker_table_is_refused(tmp_path, monkeypatch):
    """One-vs-rest markers are cell-type specific by construction; scored against an atlas they
    would top the list as 'disease genes'."""
    _inputs(tmp_path, monkeypatch, source="run_de_markers")
    with pytest.raises(SystemExit, match="MARKER table"):
        _run()


def test_an_unlabelled_table_needs_an_explicit_declaration(tmp_path, monkeypatch):
    _inputs(tmp_path, monkeypatch, source="none")
    with pytest.raises(SystemExit, match="de_is_condition_contrast"):
        _run()
    summary = _run(de_is_condition_contrast=True)
    assert summary["status"] == "ok"
    assert summary["de_source"]["tool"] == "unlabelled"
    assert any("de_is_condition_contrast" in w for w in summary["warnings"])


def test_exploratory_p_values_never_gate_or_score(tmp_path, monkeypatch):
    """run_de's stratified condition contrast is labelled exploratory_ranking: its p-values must
    not decide selection, nor carry weight in the score (the label run_enrichment honours too)."""
    rows = [dict(r) for r in DE_ROWS]
    rows[1].update(pval=0.4, pval_adj=0.5)           # GENE2: large effect, no significance
    _, art = _inputs(tmp_path, monkeypatch, source="run_de_exploratory", rows=rows)
    summary = _run()
    assert summary["p_values_inferential"] is False
    assert summary["score_weights"]["fdr"] == 0
    assert pytest.approx(sum(summary["score_weights"].values())) == 1
    assert any("exploratory_ranking" in w for w in summary["warnings"])
    candidates = pd.read_csv(art / "tables" / "atlas" / "candidates.csv").set_index("gene")
    assert candidates.loc["GENE2", "selected"]       # selected on effect size alone
    assert "effect-size gate passed" in candidates.loc["GENE2", "why_selected"]
    assert "FDR" not in candidates.loc["GENE2", "why_selected"]


def test_run_de_rows_come_from_the_full_per_group_table(tmp_path, monkeypatch):
    """run_de's combined table keeps only its capped significant rows; the per-group file is the
    full tested table, and a group with no significant row is listed only in its index."""
    _, art = _inputs(tmp_path, monkeypatch, source="run_de", rows=DE_ROWS[:1])
    tables = art / "tables"
    pd.DataFrame(DE_ROWS[:3]).to_csv(tables / "de_cell_type_rod.csv", index=False)
    pd.DataFrame([{"group": "cone", "gene": "GENE2", "log2fc": 1.2, "pval": 0.001,
                   "pval_adj": 0.2, "score": 2.0}]).to_csv(tables / "de_cell_type_cone.csv", index=False)
    pd.DataFrame([{"slug": "rod", "group": "rod"}, {"slug": "cone", "group": "cone"}]).to_csv(
        tables / "rank_cell_type_index.csv", index=False)
    summary = _run()
    assert summary["de_source"]["rows_from"] == {"rod": "per_group_full", "cone": "per_group_full"}
    candidates = pd.read_csv(tables / "atlas" / "candidates.csv")
    assert set(candidates.loc[candidates.group == "rod", "gene"]) == {"RHO", "GENE2", "GENE3"}
    assert set(candidates.loc[candidates.group == "cone", "gene"]) == {"GENE2"}


def test_a_stale_run_de_label_does_not_outlive_its_table(tmp_path, monkeypatch):
    """run_pseudobulk_de reusing run_de's key rewrites the table but not run_de's label."""
    _, art = _inputs(tmp_path, monkeypatch, source="pseudobulk")
    (art / "tables" / "de_cell_type_inference.json").write_text(
        json.dumps({"tool": "run_de", "mode": "markers", "reference": "rest"}))
    summary = _run()
    assert summary["de_source"]["tool"] == "run_pseudobulk_de"
    assert summary["p_values_inferential"] is True


def test_positive_control_is_off_unless_configured(tmp_path, monkeypatch):
    _inputs(tmp_path, monkeypatch)
    summary = _run()
    assert summary["positive_control"]["configured"] is False
    assert summary["positive_control"]["hypergeometric_p"] is None
    assert not any("Positive-control" in w for w in summary["warnings"])


def test_missing_atlas_is_clear(tmp_path, monkeypatch):
    work, _ = _inputs(tmp_path, monkeypatch)
    (work / "atlas" / "precalculated_atlas.csv").unlink()
    with pytest.raises(SystemExit, match="Atlas not found.*reference"):
        _run()


def test_mouse_ensembl_mapping_to_human_atlas(tmp_path, monkeypatch, capsys):
    work, art = _inputs(tmp_path, monkeypatch, rows=[
        dict(r, gene={"RHO": "ENSMUSG1", "GENE2": "ENSMUSG2", "GENE3": "ENSMUSG3"}[r["gene"]])
        for r in DE_ROWS])
    (art / "tables" / "de_cell_type_universe.txt").write_text(
        "ENSMUSG1\nENSMUSG2\nENSMUSG3\n", encoding="utf-8")
    pd.DataFrame([{"ensembl_id": "ENSMUSG1", "symbol": "Rho"},
                  {"ensembl_id": "ENSMUSG2", "symbol": "Gene2"},
                  {"ensembl_id": "ENSMUSG3", "symbol": "Gene3"}]).to_csv(work / "mouse_ids.csv", index=False)
    pd.DataFrame([{"mouse_symbol": "Rho", "human_symbol": "RHO"},
                  {"mouse_symbol": "Gene2", "human_symbol": "GENE2"},
                  {"mouse_symbol": "Gene3", "human_symbol": "GENE3"}]).to_csv(
                      work / "orthologs.csv", index=False)
    summary = _run(capsys, query_species="mouse", query_gene_namespace="ensembl",
                   query_id_map_path="mouse_ids.csv", ortholog_map_path="orthologs.csv",
                   positive_control_path=str(RETNET))
    assert summary["overlap_genes"] == 3
    assert summary["de_unmapped_rows"] == 0
    assert summary["positive_control"]["panel_in_universe"] >= 1
    assert "RHO" in set(pd.read_csv(art / "tables" / "atlas" / "candidates.csv")["gene"])


def test_no_shared_genes_reports_empty_result(tmp_path, monkeypatch):
    work, art = _inputs(tmp_path, monkeypatch)
    atlas = work / "atlas" / "precalculated_atlas.csv"
    frame = pd.read_csv(atlas)
    frame["gene"] = "UNRELATED"
    frame.to_csv(atlas, index=False)
    summary = _run()
    assert summary["status"] == "no_candidates"
    assert summary["overlap_genes"] == 0
    assert summary["warnings"]
    assert (art / "tables" / "atlas" / "candidates.csv").exists()
    assert (art / "figures" / "atlas" / "top_candidates.png").exists()


def test_legacy_bioagent_environment_aliases(tmp_path, monkeypatch):
    work, art = _inputs(tmp_path, monkeypatch)
    monkeypatch.delenv("AISCIENTIST_WORK")
    monkeypatch.delenv("AISCIENTIST_ARTIFACTS")
    monkeypatch.setenv("BIOAGENT_WORK", str(work))
    monkeypatch.setenv("BIOAGENT_ARTIFACTS", str(art))
    assert _run()["n_selected"] == 2


def test_synthetic_anndata_query(tmp_path, monkeypatch):
    ad = pytest.importorskip("anndata")
    work, _ = _inputs(tmp_path, monkeypatch)
    obj = ad.AnnData(X=np.array([[1, 2, 0], [4, 0, 1]], dtype=float),
                     obs=pd.DataFrame({"cell_type": ["rod", "cone"]}, index=["c1", "c2"]),
                     var=pd.DataFrame(index=["RHO", "GENE2", "GENE3"]))
    obj.write_h5ad(work / "adata_qc.h5ad")
    summary = _run()
    assert summary["query"]["checked"]
    assert summary["query"]["n_cells"] == 2
    assert summary["query"]["cell_type_column_present"]


def test_the_attached_reference_is_the_default_atlas(tmp_path, monkeypatch):
    """A researcher attaches the atlas to the run as reference data; run_code receives its path as
    AISCIENTIST_REFERENCE and the template needs no atlas_path at all."""
    work, _ = _inputs(tmp_path, monkeypatch)
    monkeypatch.setenv("AISCIENTIST_REFERENCE", str(work / "atlas" / "precalculated_atlas.csv"))
    summary = _run(atlas_path=None)
    assert summary["status"] == "ok" and summary["n_selected"] == 2
    monkeypatch.delenv("AISCIENTIST_REFERENCE")
    with pytest.raises(SystemExit, match="AISCIENTIST_REFERENCE"):
        _run(atlas_path=None)


def _cell_level_atlas(path, *, log1p: bool = False, sparse_x: bool = False):
    """A cell-level object as a researcher would upload it: raw counts (or log1p-normalised) per
    cell, rods expressing RHO, cones GENE2, bipolar cells GENE3, plus 3 stray cells."""
    ad = pytest.importorskip("anndata")
    rng = np.random.default_rng(0)
    rows, labels = [], []
    for ct, hot in (("rod", 0), ("cone", 1), ("bipolar", 2)):
        for _ in range(40):
            x = rng.poisson(2.0, 3).astype(float)
            x[hot] += 40
            rows.append(x)
            labels.append(ct)
    rows += [np.ones(3)] * 3
    labels += ["stray"] * 3
    x = np.vstack(rows)
    if log1p:
        x = np.log1p(x / x.sum(axis=1, keepdims=True) * 1e4)
    if sparse_x:
        from scipy import sparse
        x = sparse.csr_matrix(x)
    ad.AnnData(X=x, obs=pd.DataFrame({"cell_type": labels}, index=[f"c{i}" for i in range(len(labels))]),
               var=pd.DataFrame(index=["RHO", "GENE2", "GENE3"])).write_h5ad(path)


@pytest.mark.parametrize("log1p", [False, True])
@pytest.mark.parametrize("sparse_x", [False, True])
def test_a_cell_level_upload_is_averaged_per_cell_type(tmp_path, monkeypatch, log1p, sparse_x):
    work, art = _inputs(tmp_path, monkeypatch)
    _cell_level_atlas(work / "atlas" / "cells.h5ad", log1p=log1p, sparse_x=sparse_x)
    summary = _run(atlas_path="atlas/cells.h5ad", max_atlas_profiles=50)
    info = summary["atlas"]
    assert info["kind"] == "cell_level" and info["n_obs"] == 123
    assert info["scale_used"] == ("log1p" if log1p else "counts")       # detected, and reported
    assert info["cells_per_type"] == {"bipolar": 40, "cone": 40, "rod": 40}
    assert any("stray" in w for w in summary["warnings"])              # < min_atlas_cells
    candidates = pd.read_csv(art / "tables" / "atlas" / "candidates.csv").set_index("gene")
    assert candidates.loc["RHO", "atlas_tau"] > 0.8                     # rod-specific in the atlas
    assert candidates.loc["RHO", "selected"]


def test_a_profile_table_must_be_linear(tmp_path, monkeypatch):
    _inputs(tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="must be linear"):
        _run(atlas_expression_scale="log1p")


def test_against_the_real_run_de(tmp_path, monkeypatch):
    """The labels this template trusts are the ones run_de actually writes."""
    pytest.importorskip("scanpy")
    import anndata as ad
    from types import SimpleNamespace

    from aiscientist.tools.run_de import tool as run_de_tool
    from aiscientist.tools.run_scanpy_qc import tool as run_scanpy_qc_tool

    rng = np.random.default_rng(0)
    n = 300
    x = rng.poisson(4.0, (n, 40)).astype(np.float32) + 1.0
    cond = np.array(["DDX41" if i % 2 else "WT" for i in range(n)])
    ctype = np.array([["Rod", "Cone", "MG"][i % 3] for i in range(n)])
    x[(cond == "DDX41") & (ctype == "Rod"), 0] *= 6          # Gene0 up in DDX41 rods
    adata = ad.AnnData(x)
    adata.var_names = [f"Gene{i}" for i in range(40)]
    adata.obs_names = [f"c{i}" for i in range(n)]
    adata.obs["sampleid"] = pd.Categorical(cond)
    adata.obs["majorclass"] = pd.Categorical(ctype)
    adata.obs["orig.ident"] = pd.Categorical(["s1"] * n)
    adata.write(tmp_path / "retina.h5ad")
    ctx = SimpleNamespace(workspace=tmp_path, decisions={"dataset_path": str(tmp_path / "retina.h5ad")})
    assert run_scanpy_qc_tool.run_scanpy_qc(
        {"min_genes": 1, "min_cells": 1, "max_pct_mt": 100.0}, ctx)["status"] == "ok"
    work, art = tmp_path / "work", tmp_path / "artifacts"
    (work / "atlas").mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{"cell_type": ct, "gene": f"Gene{i}", "mean_expression": 40.0 if (ct, i) == ("Rod", 0) else 4.0}
                  for ct in ("Rod", "Cone", "MG") for i in range(40)]).to_csv(
                      work / "atlas" / "precalculated_atlas.csv", index=False)
    monkeypatch.setenv("AISCIENTIST_WORK", str(work))
    monkeypatch.setenv("AISCIENTIST_ARTIFACTS", str(art))

    assert run_de_tool.run_de({"groupby": "majorclass"}, ctx)["status"] == "ok"
    with pytest.raises(SystemExit, match="MARKER table"):
        _run(de_key="majorclass")

    out = run_de_tool.run_de({"groupby": "sampleid", "reference": "WT", "stratify_by": "majorclass"}, ctx)
    assert out["status"] == "ok" and out["inference"] == "exploratory_ranking"
    summary = _run(de_key="majorclass", query_path=None)
    assert summary["p_values_inferential"] is False
    assert set(summary["de_source"]["rows_from"].values()) == {"per_group_full"}
    assert set(summary["de_source"]["rows_from"]) == {"Rod", "Cone", "MG"}
    candidates = pd.read_csv(art / "tables" / "atlas" / "candidates.csv")
    top = candidates[candidates.selected].iloc[0]
    assert (top["gene"], top["group"]) == ("GENE0", "Rod")
