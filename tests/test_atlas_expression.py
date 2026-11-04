"""Offline bridge contracts; no containers, network, scanpy or real h5ad."""
import json
import shlex
from pathlib import Path

import pytest

from aiscientist.agents import atlas_expression as atlas, skills, skills_registry
from aiscientist.agents.research_harness import HarnessContext
from aiscientist.agents.registry import build_scientist_catalog

FIXTURE = Path(__file__).parent / "fixtures" / "atlas_expression"
IMAGE = ("docker://ghcr.io/lijinbio/mcp-atlas-expression-exploration@sha256:"
         "5e9b13da5d180e7983945a7f1f4182e4b12876d3ec8f59ed0a0aa7b43d041d8a")
GENES = "LRAT, CRALBP, rho, ABCR"
RESOLUTION = [
    {"query": "LRAT", "atlas_gene": "LRAT", "method": "exact"},
    {"query": "CRALBP", "atlas_gene": "RLBP1", "method": "hgnc_alias"},
    {"query": "rho", "atlas_gene": "RHO", "method": "case"},
    {"query": "ABCR", "atlas_gene": "ABCA4", "method": "hgnc_previous"},
]


@pytest.fixture
def release(skill_release, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_ATLAS_VERSION", "heca/v2026.11")
    return skill_release(FIXTURE, tag="v0.2.0")


class Runner:
    supports_skill_policy = True
    dataset_path = "/shared/heca/v2026.11/HRCA_snRNA_allcells.h5ad"

    def __init__(self):
        self.calls = []
        self.result = {"resolution": RESOLUTION}

    def run_in_image(self, command, image, **kwargs):
        self.calls.append((command, image, kwargs))
        return {"status": "ok", "returncode": 0,
                "stdout": json.dumps({"result": self.result, "atlas": {
                    "path": self.dataset_path, "version": "heca/v2026.11",
                    "size_bytes": 123, "mtime_ns": 456}}),
                "artifacts": ["gene_expression/q/gene_resolution.tsv"]}


def tools(runner):
    return {t.name: t for t in build_scientist_catalog(code_executor=runner)}


def test_release_metadata_checksum_tier_and_exposure(release, monkeypatch):
    skill = skills.get_skill(atlas.SKILL)
    assert (skill.status, skill.registry_release) == ("community", "v0.2.0")
    assert (skill.image, skill.cpus, skill.mem_gb, skill.time_limit) == (IMAGE, 4, 300, "04:00:00")
    assert set(atlas.TOOLS) <= tools(Runner()).keys()
    assert not set(atlas.TOOLS) & tools(None).keys()
    monkeypatch.setenv("AISCIENTIST_SKILLS_REGISTRY_ALLOWED_TIERS", "core,verified")
    assert not set(atlas.TOOLS) & tools(Runner()).keys()
    monkeypatch.setenv("AISCIENTIST_SKILLS_REGISTRY_ALLOWED_TIERS", "core,verified,community")
    bundle = release / "aiscientist-skills-0.2.0.zip"
    bundle.write_bytes(bundle.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        skills_registry.check_release(release, "v0.2.0")
    assert not set(atlas.TOOLS) & tools(Runner()).keys()


def test_resolution_flow_preserves_all_user_names_and_provenance(release, tmp_path):
    runner = Runner()
    catalog = tools(runner)
    ctx = HarnessContext(workspace=tmp_path)
    result = catalog["resolve_genes"].executor({"genes": GENES}, ctx)
    assert result["resolution"] == RESOLUTION
    runner.result = {"query_id": "HRCA__LRAT_RLBP1_RHO_ABCA4", "state": "COMPLETED", "resolution": RESOLUTION}
    submitted = catalog["submit_query"].executor({"genes": GENES}, ctx)
    assert submitted["resolution"] == RESOLUTION
    assert submitted["artifacts"] == ["gene_expression/q/gene_resolution.tsv"]
    for command, image, kwargs in runner.calls:
        assert image == IMAGE
        assert (kwargs["cpus"], kwargs["mem_gb"], kwargs["time_limit"]) == (4, 300, "04:00:00")
        cli = next(line for line in command.splitlines() if line.startswith("aiscientist-mcp "))
        assert json.loads(shlex.split(cli)[7]) == {"genes": GENES}
        assert 'ATLAS_H5AD="$AISCIENTIST_DATASET"' in command
        assert 'AISCI_DATA_DIR="$AISCIENTIST_ENV_DIR" AISCI_EXECUTOR=inline' in command
        assert "--timeout 15000" in command
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["atlas"]["version"] == "heca/v2026.11"
    assert manifest["atlas"]["path"] == runner.dataset_path
    assert manifest["skills"][0]["registry_release"] == "v0.2.0"
    runner.result = {"state": "FAILED", "query_id": "q"}
    assert catalog["query_status"].executor({"query_id": "q"}, ctx)["status"] == "error"


def test_all_nine_exact_signatures(release):
    catalog = tools(Runner())
    expected = {
        "resolve_genes": {"genes", "atlas"}, "submit_query": {"genes", "atlas", "rerun"},
        "query_status": {"query_id"}, "get_summary": {"query_id", "level", "gene", "top"},
        "plot_dotplot": {"query_id"}, "plot_violin": {"query_id", "groupby"},
        "plot_featureplot": {"query_id"}, "write_report": {"query_id"}, "list_queries": set(),
    }
    for name, fields in expected.items():
        assert set(catalog[name].parameters["properties"]) == fields
    from aiscientist.agents.research_lab import _primary_tool
    assert _primary_tool("Resolve with `resolve_genes`.") == "resolve_genes"
    assert _primary_tool("Extract with `submit_query`.") == "submit_query"


def test_invalid_binding_version_and_failed_container(release, monkeypatch):
    runner = Runner()
    tool = tools(runner)["resolve_genes"]
    for args in ({}, {"genes": GENES, "extra": 1}, {"genes": GENES, "atlas": "/other.h5ad"}):
        assert tool.executor(args, None)["status"] == "error"
    runner.dataset_path = "/shared/not-an-atlas.txt"
    assert tool.executor({"genes": GENES}, None)["status"] == "error"
    runner.dataset_path = Runner.dataset_path
    monkeypatch.delenv("AISCIENTIST_ATLAS_VERSION")
    assert tool.executor({"genes": GENES}, None)["status"] == "error"
    assert not runner.calls
    monkeypatch.setenv("AISCIENTIST_ATLAS_VERSION", "heca/v2026.11")
    monkeypatch.setattr(runner, "run_in_image", lambda *a, **kw: {"status": "error", "stderr": "OOM"})
    assert tools(runner)["submit_query"].executor({"genes": GENES}, None)["stderr"] == "OOM"
    monkeypatch.setattr(runner, "run_in_image", lambda *a, **kw: {"status": "ok", "stdout": "truncated"})
    assert tools(runner)["submit_query"].executor({"genes": GENES}, None)["status"] == "error"


def test_deliverables_are_collected_without_checkpoint_or_atlas(release, tmp_path, monkeypatch, capsys):
    data, artifacts = tmp_path / "work", tmp_path / "artifacts"
    query = data / "atlas-expression-exploration" / "q"
    query.mkdir(parents=True)
    names = ["gene_resolution.tsv", "summary_majorclass.tsv", "summary_celltype.tsv",
             "summary_region_class.tsv", "dotplot_majorclass.pdf", "dotplot_majorclass.png",
             "violin.pdf", "violin.png", "featureplot.pdf", "featureplot.png", "report.md"]
    for name in [*names, "query.h5ad", "job.log", "state.json"]:
        (query / name).write_text(name)
    source = tmp_path / "atlas.h5ad"
    source.write_text("mock atlas; never parsed")
    (tmp_path / ".atlas-result.json").write_text(json.dumps({"resolution": RESOLUTION}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AISCI_DATA_DIR", str(data))
    monkeypatch.setenv("AISCIENTIST_ARTIFACTS", str(artifacts))
    monkeypatch.setenv("ATLAS_H5AD", str(source))
    monkeypatch.setenv("AISCI_ATLAS_RESULT", str(tmp_path / ".atlas-result.json"))
    exec(atlas.COLLECT, {})
    response = json.loads(capsys.readouterr().out)
    assert response["result"]["resolution"] == RESOLUTION
    assert response["atlas"]["size_bytes"] == source.stat().st_size
    assert sorted(p.name for p in artifacts.rglob("*") if p.is_file()) == sorted(names)
    assert source.read_text() == "mock atlas; never parsed"


def test_submit_uses_real_slurm_image_path_with_metadata(release):
    from test_run_in_environment import _HPC, _executor
    hpc = _HPC()
    runner = _executor(hpc)
    runner.dataset_path = Runner.dataset_path
    tools(runner)["submit_query"].executor({"genes": GENES}, None)
    script = "\n".join(hpc.commands)
    for value in ("--cpus-per-task=4", "--mem=300G", "--time=04:00:00",
                  "AISCI_EXECUTOR=inline", "call submit_query", "apptainer"):
        assert value in script
