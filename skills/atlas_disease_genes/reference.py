"""Reference template: combine existing condition DE with a precalculated cell-type atlas.

The Scientist must inspect the atlas and adapt CONFIG before run_code. No network access is used.
The score ranks evidence; it is neither a new statistical test nor a causal-gene probability.
"""
from __future__ import annotations

import filecmp
import json
import math
import os
from pathlib import Path

import numpy as np
import pandas as pd


# ----- CONFIG: inspect the real files and adapt every dataset-specific value -----------------
CONFIG = {
    # Paths are absolute or relative to AISCIENTIST_WORK. None = the file the user attached as
    # REFERENCE data (AISCIENTIST_REFERENCE), which run_code can read on HPC3; any other atlas must
    # sit under AISCIENTIST_WORK, because the container sees nothing else.
    "atlas_path": None,
    "query_path": "adata_qc.h5ad",  # optional; set None when the upstream DE table suffices
    # REQUIRED: the upstream condition contrast, tables/de_<de_key>_all.csv — run_pseudobulk_de's
    # group_key or run_de's stratify_by. Never discovered: a one-vs-rest marker table has the
    # same columns, and only the label the DE tool leaves beside it tells the two apart.
    "de_key": None,
    # Only for a hand-written contrast table no DE tool labelled (e.g. condition_by_celltype).
    "de_is_condition_contrast": False,
    # Off unless the studied phenotype makes the panel relevant: one gene symbol per line.
    "positive_control_path": None,
    # Long-form atlas columns; optional disease contrast columns may be absent.
    "atlas_columns": {"cell_type": "cell_type", "gene": "gene",
                      "mean_expression": "mean_expression",
                      "disease_log2fc": "disease_log2fc",
                      "disease_pval_adj": "disease_pval_adj"},
    # h5ad X scale: "auto" (detected and reported), "counts", "log1p" or "linear". A table's
    # mean_expression must be linear. Means are taken on the linear scale (counts -> CP10k).
    "atlas_expression_scale": "auto",
    "max_atlas_profiles": 5000,  # an h5ad with more observations is a cell-level object
    "min_atlas_cells": 20,       # cell-level h5ad: a cell type with fewer cells is dropped
    "query_species": "human", "atlas_species": "human",  # human or mouse
    "query_gene_namespace": "symbol", "atlas_gene_namespace": "symbol",
    # CSV columns: ensembl_id,symbol; mouse_symbol,human_symbol. Required for relevant conversions.
    "query_id_map_path": None, "atlas_id_map_path": None,
    "ortholog_map_path": None,
    "cell_type_map": {},  # query DE group -> atlas cell_type; identity is the default
    "query_cell_type_column": "cell_type",  # optional h5ad observation coverage check
    "fdr_max": 0.05, "min_abs_log2fc": 0.25,
    "min_atlas_mean": 0.0, "min_gene_overlap": 1,
    # Components are normalized to [0, 1]. Missing atlas disease DE contributes zero. The FDR
    # weight is dropped when the upstream p-values are not inferential (see NON_INFERENTIAL).
    "weights": {"fdr": 0.35, "effect": 0.25, "specificity": 0.25,
                "atlas_concordance": 0.15},
    "top_n_plot": 20,
}
# ---------------------------------------------------------------------------------------------

# run_de's labels for cell-level p-values across a condition; run_enrichment honours the same two.
NON_INFERENTIAL = ("exploratory_ranking", "pseudoreplicated")


def _env_path(current: str, legacy: str) -> Path:
    value = os.environ.get(current) or os.environ.get(legacy)
    if not value:
        raise SystemExit(f"Set {current} (or legacy {legacy}) before running this skill")
    return Path(value)


def _path(value: str | None, base: Path) -> Path | None:
    if not value:
        return None
    p = Path(value)
    return p if p.is_absolute() else base / p


def _slug(name: object) -> str:
    # The platform's per-group filename rule (tools/_lib/scrna.py::_slug).
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(name).strip())
    return safe.strip("_") or "group"


def _load_unique_map(path: Path | None, source: str, target: str, warnings: list[str]) -> dict[str, str]:
    if path is None:
        return {}
    if not path.is_file():
        warnings.append(f"mapping file missing: {path}")
        return {}
    try:
        frame = pd.read_csv(path, dtype=str, usecols=[source, target]).dropna()
    except ValueError:
        warnings.append(f"{path}: expected mapping columns {source!r} and {target!r}")
        return {}
    frame[source] = frame[source].str.split(".", regex=False).str[0].str.upper()
    frame[target] = frame[target].str.upper()
    distinct = frame.groupby(source)[target].nunique()
    ambiguous = set(distinct[distinct != 1].index)
    if ambiguous:
        warnings.append(f"{path.name}: excluded {len(ambiguous)} ambiguous source IDs")
    return {a: b for a, b in frame[[source, target]].drop_duplicates().itertuples(index=False, name=None)
            if a not in ambiguous}


def _mapper(species: str, namespace: str, id_path: Path | None, ortholog: dict[str, str],
            warnings: list[str]):
    if species not in {"human", "mouse"} or namespace not in {"symbol", "ensembl"}:
        raise SystemExit("species must be human/mouse and gene namespace must be symbol/ensembl")
    identifiers = _load_unique_map(id_path, "ensembl_id", "symbol", warnings) if namespace == "ensembl" else {}
    if namespace == "ensembl" and not identifiers:
        warnings.append(f"No usable Ensembl mapping for {species}; its genes will be unmatched")
    if species == "mouse" and not ortholog:
        warnings.append("No usable mouse-to-human ortholog map; mouse genes will be unmatched")

    def convert(raw: object) -> str | None:
        if pd.isna(raw):
            return None
        key = str(raw).strip().split(".")[0].upper() if namespace == "ensembl" else str(raw).strip().upper()
        if not key:
            return None
        symbol = identifiers.get(key) if namespace == "ensembl" else key
        return ortholog.get(symbol) if species == "mouse" and symbol else symbol

    return convert


def _load_de(tables: Path, cfg: dict, warnings: list[str]) -> tuple[pd.DataFrame, dict]:
    """The upstream contrast named by CONFIG['de_key'], with where it came from and whether its
    p-values are inferential. Refuses a table no tool labelled a condition contrast."""
    key = cfg["de_key"]
    if not key:
        keys = sorted(p.name[len("de_"):-len("_all.csv")] for p in tables.glob("de_*_all.csv"))
        raise SystemExit(f"Set CONFIG['de_key'] to the upstream condition contrast; DE keys on disk: "
                         f"{keys}. Use run_pseudobulk_de's group_key or run_de's stratify_by.")
    combined_path = tables / f"de_{key}_all.csv"
    if not combined_path.is_file():
        raise SystemExit(f"Upstream DE table missing: {combined_path}; run run_pseudobulk_de or "
                         "run_de(reference=..., stratify_by=...) first")
    sidecar = tables / f"de_{key}_inference.json"
    pseudobulk = tables / "pseudobulk_all.csv"
    if pseudobulk.is_file() and filecmp.cmp(pseudobulk, combined_path, shallow=False):
        # run_pseudobulk_de writes the same rows to both names; any sidecar left beside this key
        # belongs to an earlier run_de whose table it has since replaced.
        source = {"tool": "run_pseudobulk_de", "mode": "contrast", "inference": "sample_level"}
        warnings.append("Pseudobulk combined table keeps the 500 lowest-p genes per group; genes "
                        "beyond them are not ranked")
    elif sidecar.is_file():
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SystemExit(f"Unreadable DE label {sidecar}: {exc}") from None
        if meta.get("mode") == "markers" or str(meta.get("reference") or "rest") == "rest":
            raise SystemExit(f"{combined_path.name} is a one-vs-rest MARKER table, not a condition "
                             "contrast. Run run_pseudobulk_de, or run_de with reference= and "
                             "stratify_by=, and point CONFIG['de_key'] at that result.")
        source = {"tool": str(meta.get("tool") or "run_de"), "mode": "contrast",
                  "inference": meta.get("inference") or "cell_level",
                  "groupby": meta.get("groupby"), "reference": meta.get("reference")}
    elif cfg["de_is_condition_contrast"]:
        source = {"tool": "unlabelled", "mode": "declared_contrast", "inference": "declared"}
        warnings.append(f"No DE tool labelled {combined_path.name}; it is treated as a condition "
                        "contrast because CONFIG['de_is_condition_contrast'] is set")
    else:
        raise SystemExit(f"No DE tool labelled {combined_path.name} as a condition contrast, and a "
                         "one-vs-rest marker table has the same columns. Re-run run_pseudobulk_de "
                         "or run_de(reference=..., stratify_by=...); for a hand-written contrast "
                         "table only, set CONFIG['de_is_condition_contrast'] = True.")

    combined = pd.read_csv(combined_path)
    needed = {"group", "gene", "log2fc", "pval", "pval_adj", "score"}
    if not needed <= set(combined):
        raise SystemExit(f"DE table missing columns {sorted(needed - set(combined))}; expected canonical tool output")
    combined["group"] = combined["group"].astype(str)
    # run_de's combined table holds only its capped significant rows; the per-group file is the
    # full tested table for that contrast — the same preference run_enrichment applies. Its group
    # index also lists groups with no significant row, which the combined table omits.
    groups = list(combined["group"].unique())
    index = tables / f"rank_{key}_index.csv"
    if source["tool"] == "run_de" and index.is_file():
        groups += [g for g in pd.read_csv(index, dtype=str)["group"] if g not in groups]
    parts, rows_from = [], {}
    for group in groups:
        full = tables / f"de_{key}_{_slug(group)}.csv"
        if source["tool"] == "run_de" and full.is_file():
            part = pd.read_csv(full)
            part["group"] = group
            rows_from[group] = "per_group_full"
        else:
            part = combined[combined["group"] == group]
            rows_from[group] = "combined"
        parts.append(part[sorted(needed)])
    truncated = [g for g, how in rows_from.items() if how == "combined"]
    if source["tool"] == "run_de" and truncated:
        warnings.append(f"No full per-group table for {len(truncated)} group(s); their rows come "
                        "from the capped combined table")
    source.update(key=key, table=str(combined_path), rows_from=rows_from)
    return pd.concat(parts, ignore_index=True), source


def _detect_scale(block) -> str:
    """counts / log1p / linear, from one chunk of X: integers are counts, small non-integers log1p."""
    vals = block.data if hasattr(block, "tocsr") else np.asarray(block).ravel()   # sparse: nonzeros
    vals = vals[np.isfinite(vals)][:200_000]
    if not len(vals):
        return "linear"
    if np.allclose(vals, np.round(vals)):
        return "counts"
    return "log1p" if float(vals.max()) <= 30 else "linear"


def _h5ad_profiles(path: Path, cfg: dict, convert, wanted: set[str], warnings: list[str], info: dict):
    """Mean expression per cell type, on the linear scale, from an h5ad atlas: precalculated
    profiles (one observation per profile) or a cell-level object a researcher uploaded. Read in
    row chunks, restricted to the query's genes, so a large atlas never loads whole."""
    try:
        import anndata as ad
        from scipy import sparse
    except ImportError:
        raise SystemExit("h5ad atlas requires anndata and scipy in the HPC analysis environment") from None
    ct_col = cfg["atlas_columns"]["cell_type"]
    atlas = ad.read_h5ad(path, backed="r")
    try:
        if ct_col not in atlas.obs:
            raise SystemExit(f"atlas h5ad obs lacks {ct_col!r}; available: {list(atlas.obs)}")
        if atlas.X is None:
            raise SystemExit("atlas h5ad has no X matrix")
        labels = atlas.obs[ct_col].astype(str).str.strip().to_numpy()
        types = sorted(set(labels) - {"", "nan"})
        code = {t: i for i, t in enumerate(types)}
        var = atlas.var_names.astype(str)
        cols = np.flatnonzero([convert(g) in wanted for g in var])
        cell_level = atlas.n_obs > int(cfg["max_atlas_profiles"])
        scale = cfg["atlas_expression_scale"]
        sums = np.zeros((len(types), len(cols)))
        n = np.zeros(len(types))
        step = 20_000
        for start in range(0, atlas.n_obs, step):
            block = atlas.X[start:start + step]
            block = sparse.csr_matrix(block) if sparse.issparse(block) else np.asarray(block, dtype=float)
            if scale == "auto":     # precalculated profiles are linear by contract
                scale = _detect_scale(block) if cell_level else "linear"
            if scale == "counts":
                lib = np.asarray(block.sum(axis=1)).ravel()
                factor = np.divide(1e4, lib, out=np.zeros_like(lib, dtype=float), where=lib > 0)
                block = (sparse.diags(factor) @ block) if sparse.issparse(block) else block * factor[:, None]
            block = block[:, cols]
            if scale == "log1p":
                block = block.expm1() if sparse.issparse(block) else np.expm1(block)
            rows = [i for i, lab in enumerate(labels[start:start + step]) if lab in code]
            onehot = sparse.csr_matrix((np.ones(len(rows)), ([code[labels[start + i]] for i in rows], rows)),
                                       shape=(len(types), block.shape[0]))
            part = onehot @ block
            sums += part.toarray() if sparse.issparse(part) else np.asarray(part)
            n += np.asarray(onehot.sum(axis=1)).ravel()
        keep = n >= (int(cfg["min_atlas_cells"]) if cell_level else 1)
        dropped = [t for t, k in zip(types, keep) if not k]
        if dropped:
            warnings.append(f"Dropped {len(dropped)} atlas cell type(s) with fewer than "
                            f"{cfg['min_atlas_cells']} cells: {', '.join(dropped[:10])}")
        info.update(kind="cell_level" if cell_level else "profiles", n_obs=int(atlas.n_obs),
                    scale_used=scale, n_cell_types=int(keep.sum()),
                    cells_per_type={t: int(c) for t, c, k in zip(types, n, keep) if k})
        if cell_level:
            warnings.append(f"Cell-level h5ad atlas: {atlas.n_obs} cells averaged into "
                            f"{int(keep.sum())} cell-type profiles (X read as {scale})")
        means = sums[keep] / n[keep][:, None]
        kept_types = [t for t, k in zip(types, keep) if k]
        names = cfg["atlas_columns"]
        for t, row in zip(kept_types, means):
            yield pd.DataFrame({names["cell_type"]: t, names["gene"]: var[cols],
                                names["mean_expression"]: row})
    finally:
        atlas.file.close()


def _atlas_batches(path: Path, cfg: dict, convert, wanted: set[str], warnings: list[str], info: dict):
    names = cfg["atlas_columns"]
    name = path.name.lower()
    if name.endswith(".h5ad"):
        yield from _h5ad_profiles(path, cfg, convert, wanted, warnings, info)
        return
    if cfg["atlas_expression_scale"] not in ("auto", "linear"):
        raise SystemExit("A profile table's mean_expression must be linear; convert it, or attach "
                         "the cell-level h5ad and let the template average it")
    info.update(kind="table", scale_used="linear")
    if name.endswith((".csv", ".csv.gz", ".tsv", ".tsv.gz")):
        sep = "\t" if ".tsv" in name else ","
        for batch in pd.read_csv(path, sep=sep, chunksize=100_000,
                                 usecols=lambda c: c in set(names.values())):
            yield batch
    elif name.endswith(".parquet"):
        try:
            import pyarrow.parquet as pq
        except ImportError:
            raise SystemExit("Parquet atlas requires pyarrow in the HPC analysis environment") from None
        parquet = pq.ParquetFile(path)
        columns = [c for c in names.values() if c in parquet.schema.names]
        for batch in parquet.iter_batches(batch_size=100_000, columns=columns):
            yield batch.to_pandas()
    else:
        raise SystemExit(f"Unsupported atlas format {path.name!r}; expected csv, tsv, parquet, or h5ad")


def _read_atlas(path: Path, cfg: dict, convert, wanted_genes: set[str], warnings: list[str],
                info: dict) -> pd.DataFrame:
    names = cfg["atlas_columns"]
    required = [names[k] for k in ("cell_type", "gene", "mean_expression")]
    parts = []
    for batch in _atlas_batches(path, cfg, convert, wanted_genes, warnings, info):
        missing = [c for c in required if c not in batch]
        if missing:
            raise SystemExit(f"atlas missing columns {missing}; available: {list(batch)}")
        take = required + [names[k] for k in ("disease_log2fc", "disease_pval_adj") if names[k] in batch]
        frame = batch[take].rename(columns={v: k for k, v in names.items()}).copy()
        frame["gene"] = frame["gene"].map(convert)
        # Keep only query-tested genes while streaming a potentially large atlas.
        frame = frame[frame["gene"].isin(wanted_genes)]
        frame["cell_type"] = frame["cell_type"].astype(str).str.strip()
        frame["mean_expression"] = pd.to_numeric(frame["mean_expression"], errors="coerce")
        frame = frame.dropna(subset=["gene", "mean_expression"])
        frame = frame[(frame["cell_type"] != "") & (frame["mean_expression"] >= 0)]
        parts.append(frame)
    if not parts:
        return pd.DataFrame(columns=["cell_type", "gene", "mean_expression"])
    out = pd.concat(parts, ignore_index=True)
    # Repeated gene/cell-type rows can occur after ID mapping. Mean aggregation is explicit.
    agg = {"mean_expression": "mean"}
    for col in ("disease_log2fc", "disease_pval_adj"):
        if col in out:
            out[col] = pd.to_numeric(out[col], errors="coerce")
            agg[col] = "mean" if col == "disease_log2fc" else "min"
    return out.groupby(["cell_type", "gene"], as_index=False).agg(agg)


def _tau(values: np.ndarray) -> float:
    peak = float(np.max(values)) if len(values) else 0.0
    return float(np.sum(1 - values / peak) / (len(values) - 1)) if peak > 0 and len(values) > 1 else 0.0


def _positive_control(path: Path | None, candidates: pd.DataFrame, universe: set[str]) -> dict:
    result = {"configured": path is not None, "available": bool(path and path.is_file()),
              "panel_path": str(path) if path else None,
              "panel_size": 0, "panel_in_universe": 0, "top_k": 0, "top_k_hits": 0,
              "top_k_expected": 0.0, "hypergeometric_p": None}
    if not result["available"] or not universe or candidates.empty:
        return result
    known = {line.strip().upper() for line in path.read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.startswith("#")}
    valid = known & universe
    ranked = candidates[candidates["selected"]].sort_values("candidate_score", ascending=False)
    top = ranked.drop_duplicates("gene").head(min(20, len(ranked)))
    n, k, draws = len(universe), len(valid), len(top)
    hits = len(set(top["gene"]) & valid)
    result.update(panel_size=len(known), panel_in_universe=k, top_k=draws,
                  top_k_hits=hits, top_k_expected=round(draws * k / n, 4))
    if draws and k:
        hi = min(draws, k)
        # Exact survival probability, stable for ordinary gene universes.
        result["hypergeometric_p"] = min(1.0, sum(
            math.exp(math.lgamma(k + 1) - math.lgamma(x + 1) - math.lgamma(k - x + 1)
                     + math.lgamma(n - k + 1) - math.lgamma(draws - x + 1)
                     - math.lgamma(n - k - draws + x + 1)
                     - (math.lgamma(n + 1) - math.lgamma(draws + 1) - math.lgamma(n - draws + 1)))
            for x in range(hits, hi + 1) if 0 <= draws - x <= n - k))
    return result


def main(cfg: dict = CONFIG) -> dict:
    work = _env_path("AISCIENTIST_WORK", "BIOAGENT_WORK")
    art = _env_path("AISCIENTIST_ARTIFACTS", "BIOAGENT_ARTIFACTS")
    tables = art / "tables"
    tdir, fdir = tables / "atlas", art / "figures" / "atlas"
    tdir.mkdir(parents=True, exist_ok=True)
    fdir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    atlas_path = _path(cfg["atlas_path"] or os.environ.get("AISCIENTIST_REFERENCE"), work)
    if not atlas_path or not atlas_path.is_file():
        raise SystemExit(f"Atlas not found: {atlas_path}. Attach it to the run as reference data "
                         "(AISCIENTIST_REFERENCE), or place it under AISCIENTIST_WORK and set "
                         "CONFIG['atlas_path']")
    if cfg["atlas_expression_scale"] not in ("auto", "linear", "counts", "log1p"):
        raise SystemExit("atlas_expression_scale must be auto, linear, counts or log1p")
    atlas_info: dict = {}
    de, de_source = _load_de(tables, cfg, warnings)
    p_inferential = de_source["inference"] not in NON_INFERENTIAL
    if not p_inferential:
        warnings.append(f"Upstream p-values are labelled {de_source['inference']!r}: candidates are "
                        "gated and scored on effect size and atlas evidence only, not on FDR")

    ortholog = _load_unique_map(_path(cfg["ortholog_map_path"], work), "mouse_symbol", "human_symbol", warnings)
    query_convert = _mapper(cfg["query_species"], cfg["query_gene_namespace"],
                            _path(cfg["query_id_map_path"], work), ortholog, warnings)
    atlas_convert = _mapper(cfg["atlas_species"], cfg["atlas_gene_namespace"],
                            _path(cfg["atlas_id_map_path"], work), ortholog, warnings)
    de["gene_original"] = de["gene"].astype(str)
    de["gene"] = de["gene"].map(query_convert)
    n_de_unmapped = int(de["gene"].isna().sum())
    de = de.dropna(subset=["gene"]).copy()
    for col in ("log2fc", "pval", "pval_adj", "score"):
        de[col] = pd.to_numeric(de[col], errors="coerce")
    de = de.dropna(subset=["log2fc", "pval_adj"])
    if de.duplicated(["group", "gene"]).any():
        warnings.append("Duplicate mapped DE genes per group: retaining the lowest FDR row")
        de = de.sort_values("pval_adj").drop_duplicates(["group", "gene"])
    atlas = _read_atlas(atlas_path, cfg, atlas_convert, set(de["gene"]), warnings, atlas_info)
    query_genes, atlas_genes = set(de["gene"]), set(atlas["gene"])
    overlap = query_genes & atlas_genes
    if len(overlap) < int(cfg["min_gene_overlap"]):
        warnings.append(f"Only {len(overlap)} shared genes; minimum is {cfg['min_gene_overlap']}")
    query_path = _path(cfg["query_path"], work)
    query_info = {"path": str(query_path) if query_path else None, "checked": False}
    if query_path:
        if not query_path.is_file():
            warnings.append(f"Optional query h5ad missing: {query_path}; using upstream DE table")
        else:
            try:
                import anndata as ad
                obj = ad.read_h5ad(query_path, backed="r")
                try:
                    query_info.update(checked=True, n_cells=int(obj.n_obs), n_genes=int(obj.n_vars),
                                      cell_type_column_present=cfg["query_cell_type_column"] in obj.obs)
                finally:
                    obj.file.close()
            except ImportError:
                warnings.append("anndata unavailable; skipped optional query h5ad inspection")

    weights = dict(cfg["weights"])
    if not p_inferential:
        weights["fdr"] = 0.0
    total = sum(weights.values()) or 1.0
    weights = {k: v / total for k, v in weights.items()}
    gate = ("DE effect-size gate" if not p_inferential else "DE FDR/effect gate")

    means = atlas.pivot(index="gene", columns="cell_type", values="mean_expression").fillna(0)
    tau = pd.Series({g: _tau(row) for g, row in zip(means.index, means.to_numpy())}, name="atlas_tau")
    totals = means.sum(axis=1).replace(0, np.nan)
    available_cell_types = set(atlas["cell_type"])
    mapped = cfg["cell_type_map"]
    rows = []
    skipped_groups = {}
    for group, part in de.groupby("group", sort=True):
        cell_type = mapped.get(group, group)
        if cell_type not in available_cell_types:
            skipped_groups[group] = f"atlas cell type {cell_type!r} not found"
            continue
        target = atlas[atlas["cell_type"] == cell_type].set_index("gene")
        merged = part.merge(target.drop(columns="cell_type"), left_on="gene", right_index=True, how="inner")
        if merged.empty:
            skipped_groups[group] = "no overlapping mapped genes for this cell type"
            continue
        merged["atlas_cell_type"] = cell_type
        merged["atlas_tau"] = merged["gene"].map(tau).fillna(0)
        merged["atlas_target_fraction"] = (merged["gene"].map(means[cell_type]) /
                                           merged["gene"].map(totals)).fillna(0)
        passes = ((merged["log2fc"].abs() >= cfg["min_abs_log2fc"]) &
                  (merged["mean_expression"] > cfg["min_atlas_mean"]))
        if p_inferential:
            passes &= merged["pval_adj"] <= cfg["fdr_max"]
        merged["selected"] = passes
        fdr_component = (-np.log10(merged["pval_adj"].clip(lower=1e-300)) / 10).clip(0, 1)
        effect_component = (merged["log2fc"].abs() / 4).clip(0, 1)
        specificity = (merged["atlas_tau"] * merged["atlas_target_fraction"]).clip(0, 1)
        if "disease_log2fc" in merged:
            directional = (np.sign(merged["log2fc"]) == np.sign(merged["disease_log2fc"])) & merged["disease_log2fc"].notna()
            if "disease_pval_adj" in merged:
                directional &= merged["disease_pval_adj"].le(cfg["fdr_max"])
            merged["atlas_direction_concordant"] = directional
            concordance = directional.astype(float)
            direction = np.where(directional, "; atlas disease direction agrees",
                                 "; atlas disease direction disagrees or lacks significance")
        else:
            merged["atlas_direction_concordant"] = pd.NA
            concordance = pd.Series(0.0, index=merged.index)
            direction = "; atlas disease contrast unavailable"
        merged["candidate_score"] = (weights["fdr"] * fdr_component + weights["effect"] * effect_component
                                     + weights["specificity"] * specificity
                                     + weights["atlas_concordance"] * concordance).round(6)
        merged["why_selected"] = (
            pd.Series(np.where(passes, f"{gate} passed", f"{gate} or atlas expression gate not met"),
                      index=merged.index)
            + f"; atlas {cell_type} mean=" + merged["mean_expression"].map("{:.3g}".format)
            + ", tau=" + merged["atlas_tau"].map("{:.3g}".format) + direction)
        rows.append(merged)
    if skipped_groups:
        warnings.append(f"Skipped {len(skipped_groups)} DE groups with no usable atlas match")
    candidates = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=[
        "group", "gene", "atlas_cell_type", "log2fc", "pval_adj", "mean_expression",
        "atlas_tau", "atlas_target_fraction", "selected", "candidate_score", "why_selected"])
    candidates = candidates.sort_values("candidate_score", ascending=False)
    candidates.to_csv(tdir / "candidates.csv", index=False)
    pd.DataFrame([{"query_group": k, "atlas_cell_type": mapped.get(k, k), "reason": v}
                  for k, v in skipped_groups.items()], columns=["query_group", "atlas_cell_type", "reason"]).to_csv(
                      tdir / "skipped_groups.csv", index=False)

    # The tested universe of the upstream contrast: the positive control's hypergeometric background.
    universe_path = tables / f"de_{de_source['key']}_universe.txt"
    if universe_path.is_file():
        raw_universe = [s.strip() for s in universe_path.read_text(encoding="utf-8").splitlines() if s.strip()]
        universe = {g for g in map(query_convert, raw_universe) if g}
    else:
        universe = set(de["gene"])
        warnings.append("No full DE tested-universe file found; using genes present in the DE table")
    panel = _path(cfg["positive_control_path"], work)
    control = _positive_control(panel, candidates, universe)
    if control["configured"] and not control["available"]:
        warnings.append(f"Positive-control panel not found: {panel}")
    pd.DataFrame([control]).to_csv(tdir / "positive_control.csv", index=False)

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(7, 4))
    top = candidates[candidates["selected"]].head(int(cfg["top_n_plot"]))
    if top.empty:
        ax.text(0.5, 0.5, "No candidates passed the configured gates", ha="center", va="center")
        ax.set_axis_off()
    else:
        labels = [f"{r.gene} | {r.group}" for r in top.itertuples()]
        ax.barh(labels[::-1], top["candidate_score"].to_numpy()[::-1])
        ax.set_xlabel("Evidence ranking score (not a p-value)")
    fig.tight_layout()
    fig.savefig(fdir / "top_candidates.png", dpi=150)
    plt.close(fig)
    summary = {
        "status": "ok" if not candidates.empty else "no_candidates",
        "atlas_path": str(atlas_path), "atlas": atlas_info, "de_source": de_source, "query": query_info,
        "p_values_inferential": p_inferential, "score_weights": weights,
        "query_genes": len(query_genes), "atlas_genes_retained_for_query": len(atlas_genes),
        "overlap_genes": len(overlap), "de_unmapped_rows": n_de_unmapped,
        "n_candidate_rows": len(candidates), "n_selected": int(candidates["selected"].sum()) if len(candidates) else 0,
        "skipped_groups": skipped_groups, "positive_control": control,
        # Downstream enrichment reads the upstream contrast, never this ranking.
        "enrichment_groupby": de_source["key"],
        "tables": [str(tdir / n) for n in ("candidates.csv", "skipped_groups.csv", "positive_control.csv")],
        "figures": [str(fdir / "top_candidates.png")], "warnings": warnings,
    }
    print(json.dumps(summary, ensure_ascii=False, default=str))
    return summary


if __name__ == "__main__":
    main()
