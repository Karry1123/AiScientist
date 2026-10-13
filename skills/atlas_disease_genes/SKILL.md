---
name: atlas_disease_genes
description: Reference template — prioritize disease-associated genes by combining condition differential expression with a precalculated cell-type atlas.
---

## When to use

Use this after a condition-versus-control DE analysis when a precalculated atlas can provide
independent cell-type expression or disease-contrast evidence. First run `run_pseudobulk_de` when
independent samples or donors exist; it tests at the sample level. Otherwise run
`run_de(reference=..., stratify_by=...)`: its p-values are labelled `exploratory_ranking`, and the
template then gates and scores on effect size and atlas evidence only, never on FDR, and says so in
its summary. It refuses a one-vs-rest marker table (`run_de` without `reference`): markers are
cell-type specific by construction and would rank as "disease genes". This template integrates an
existing DE result; it does not calculate DE from cells. Do not interpret atlas expression
specificity as a disease-association p-value.

## Details & adaptation

Set `CONFIG["de_key"]` to the upstream contrast: `run_pseudobulk_de`'s `group_key` or `run_de`'s
`stratify_by` (the `<key>` of `tables/de_<key>_all.csv`). It is never discovered. The label each
DE tool leaves beside its table decides how the table is read; a hand-written contrast table that
no tool labelled needs `CONFIG["de_is_condition_contrast"] = True`. For `run_de`, rows come from the
full per-group tables, not the capped combined table.

The atlas is the file the user attached to the run as reference data: `run_code` receives it as
`AISCIENTIST_REFERENCE`, and `CONFIG["atlas_path"] = None` reads it. Any other atlas, and every mapping
file, must sit under `AISCIENTIST_WORK`: on HPC3 the container sees nothing else. Never download a
mapping or atlas during `run_code`.

Adapt the rest of `CONFIG` to the inspected atlas. A table atlas is long-form CSV, TSV or Parquet with
`cell_type`, `gene`, and **linear** `mean_expression`; optional `disease_log2fc` and
`disease_pval_adj` columns add independent directional evidence. An h5ad atlas is averaged per
`cell_type` on the linear scale: either precalculated profiles (one observation each, linear), or a
cell-level object as a researcher uploads it (more than `max_atlas_profiles` cells; raw counts are
CP10k-normalised, log1p is undone; `atlas_expression_scale="auto"` detects which and the summary
reports it under `atlas`). Cell types with fewer than `min_atlas_cells` cells are dropped and named in
the warnings. A cell-level reference contributes expression context only; it carries no disease
contrast unless a table supplies one.

Specify gene namespace, species, offline Ensembl-to-symbol and mouse-to-human ortholog maps,
cell-type labels, and thresholds. Ambiguous mappings are excluded and counted. A missing atlas
cell type or zero shared genes is reported in the JSON summary. If the atlas has no disease
contrast, rank by query DE plus atlas expression context and label that limit. The positive control
is off by default; set `positive_control_path` to a one-symbol-per-line panel only when the studied
phenotype makes it relevant (RetNet, `src/aiscientist/tools/gene_panels/ird_retnet.txt`, for
inherited retinal disease). Its overlap is validation, not an input to the score.

The candidate score is a ranking heuristic, not a probability or an adjusted p-value. The template
writes no `de_*` table. For pathway enrichment call `run_enrichment(groupby=<de_key>)` on the
upstream contrast, so the enrichment reads the same table and its inference label.

## Run

Call `read_skill_reference("atlas_disease_genes")`, then
`read_skill_reference("atlas_disease_genes", file="reference.py")`. Inspect the dataset and
atlas metadata, adapt `CONFIG`, and execute the code with `run_code`. The current runtime uses
`AISCIENTIST_WORK` and `AISCIENTIST_ARTIFACTS`; the template also accepts legacy `BIOAGENT_*`
aliases. Inspect `tables/atlas/`, `figures/atlas/`, and the printed JSON summary
(`p_values_inferential`, `de_source`, `warnings`) before making biological claims.
