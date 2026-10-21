"""Pinned registry releases and per-run policy, entirely offline."""
import hashlib
import json
import sqlite3
import zipfile
from dataclasses import replace

import pytest

from aiscientist.agents import skills as sk
from aiscientist.agents import skills_registry as registry
from aiscientist.agents.research_harness import HarnessContext


@pytest.fixture
def release(tmp_path, monkeypatch):
    root = tmp_path / "releases" / "0.1.0"
    root.mkdir(parents=True)
    monkeypatch.setenv("AISCIENTIST_SKILLS_REGISTRY_RELEASES_ROOT", str(root.parent))
    monkeypatch.setenv("AISCIENTIST_SKILLS_REGISTRY_STATS", str(tmp_path / "counts.sqlite3"))
    rows = []
    for tier in ("core", "verified", "community"):
        name = "skill-" + tier
        with zipfile.ZipFile(root / (name + ".zip"), "w") as zf:
            zf.writestr(name + "/SKILL.md", f"---\nname: {name}\ndescription: old description\n---\nGuidance")
            zf.writestr(name + "/scripts/run.sh", "echo hello")
        rows.append({"name": name, "description": "catalog description", "category": "test",
                     "path": "skills/test/" + name, "version": "1.2.3", "status": tier,
                     "tags": ["biology"], "requirements": {"compute": "16 CPUs, 80 GB, 20 min",
                     "gpu": False, "scheduler": "optional", "network": "none", "secrets": []}})
    (root / "catalog.json").write_text(json.dumps({"schema_version": 1,
        "registry": "aiscientist-skills", "registry_version": "0.1.0", "skills": rows}))
    checksums(root)
    yield root
    monkeypatch.undo()
    sk.refresh_skills()


def checksums(root):
    (root / "SHA256SUMS").write_text("\n".join(
        hashlib.sha256(p.read_bytes()).hexdigest() + "  " + p.name
        for p in root.iterdir() if p.is_file() and p.name != "SHA256SUMS"))


def test_checksum_ok_catalog_metadata_and_tiers(release, monkeypatch):
    lib = registry.load(sk._load_from)
    assert set(lib) == {"skill-core", "skill-verified"}
    skill = lib["skill-core"]
    assert skill.summary == "catalog description"
    assert skill.version == "1.2.3" and skill.registry_release == "v0.1.0"
    assert skill.metadata["compute"] == "16 CPUs, 80 GB, 20 min"
    assert skill.files["scripts/run.sh"] == "echo hello"
    monkeypatch.setenv("AISCIENTIST_SKILLS_REGISTRY_ALLOWED_TIERS", "core,verified,community")
    assert len(registry.load(sk._load_from)) == 3


@pytest.mark.parametrize("missing", [False, True])
def test_checksum_mismatch_or_missing_never_falls_back(release, missing):
    path = release / "skill-core.zip"
    if missing:
        path.unlink()
    else:
        path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        sk._load_skills()


@pytest.mark.parametrize("tag", ["main", "develop", "v0.1", "v0.1.0/branch", "../v0.1.0"])
def test_tag_pinning(release, monkeypatch, tag):
    monkeypatch.setenv("AISCIENTIST_SKILLS_REGISTRY_RELEASE_TAG", tag)
    with pytest.raises(ValueError, match="pinned"):
        registry.config()


def test_fallback_is_explicit_and_logged(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("AISCIENTIST_SKILLS_REGISTRY_RELEASES_ROOT", str(tmp_path / "absent"))
    monkeypatch.setenv("AISCIENTIST_SKILLS_DIR", str(tmp_path / "local"))
    local = tmp_path / "local" / "example"
    local.mkdir(parents=True)
    (local / "SKILL.md").write_text("---\nname: example\n---\nLocal guidance")
    assert "example" in sk._load_skills()
    assert "transitional local skills fallback" in caplog.text
    monkeypatch.setenv("AISCIENTIST_SKILLS_REGISTRY_TRANSITIONAL_FALLBACK", "false")
    with pytest.raises(FileNotFoundError):
        sk._load_skills()


def test_unpacked_files_must_all_be_verified(release):
    catalog = json.loads((release / "catalog.json").read_text())
    row = catalog["skills"][0]
    folder = release / row["path"]
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: skill-core\n---\nGuidance")
    checksums(release)  # Nested files deliberately absent.
    with pytest.raises(ValueError, match="unverified"):
        registry.load(sk._load_from)
    with (release / "SHA256SUMS").open("a") as f:
        path = folder / "SKILL.md"
        f.write("\n" + hashlib.sha256(path.read_bytes()).hexdigest() + "  " + row["path"] + "/SKILL.md")
    assert registry.load(sk._load_from)["skill-core"].doc == "Guidance"


def test_zip_traversal_refused(release):
    with zipfile.ZipFile(release / "skill-core.zip", "a") as zf:
        zf.writestr("../outside", "bad")
    checksums(release)
    with pytest.raises(ValueError, match="unsafe"):
        registry.load(sk._load_from)


def test_manifest_and_version_counters_and_secrets(release, tmp_path, monkeypatch):
    skill = registry.load(sk._load_from)["skill-core"]
    skill = replace(skill, requirements={**skill.requirements, "secrets": ["TOKEN"]})
    ctx = HarnessContext(workspace=tmp_path / "run")
    class Executor:
        supports_skill_policy = True
    def run():
        assert registry.POLICY.get()["secret_values"] == {"TOKEN": "private-value"}
        return {"status": "ok", "stdout": "private-value"}
    monkeypatch.setenv("AISCIENTIST_SKILL_SECRET_TOKEN", "private-value")
    out = registry.execute(skill, ctx, Executor(), run, "run_code")
    assert out["stdout"] == "[REDACTED]"
    monkeypatch.delenv("AISCIENTIST_SKILL_SECRET_TOKEN")
    assert registry.execute(skill, ctx, Executor(), run, "run_code")["status"] == "error"
    assert registry.POLICY.get() is None
    manifest = json.loads((ctx.workspace / "manifest.json").read_text())
    assert manifest == {"registry_release": "v0.1.0", "skills": [{"name": skill.name,
        "version": "1.2.3", "registry_release": "v0.1.0"}]}
    assert "private-value" not in json.dumps(manifest)
    with sqlite3.connect(tmp_path / "counts.sqlite3") as db:
        assert db.execute("SELECT success,error FROM skill_versions").fetchone() == (1, 1)


def test_run_code_policy_and_local_isolation_gate(release, tmp_path):
    from aiscientist.agents.research_lab import make_run_code_tool
    from aiscientist.agents.sandbox import CodeSandbox
    skill = registry.load(sk._load_from)["skill-core"]
    ctx = HarnessContext(workspace=tmp_path / "run")
    registry.record_use(ctx, skill)
    sandbox = CodeSandbox()
    result = make_run_code_tool(sandbox).executor({"code": "print(1)"}, ctx)
    assert result["status"] == "error" and "isolation" in result["error"]
    sandbox.container_image = "test.sif"
    token = registry.POLICY.set({**skill.requirements, "secret_values": {}})
    try:
        cmd, _ = sandbox._command("/tmp/code.py", "/tmp")
        assert "--cleanenv" in cmd and "--network" in cmd and "none" in cmd
    finally:
        registry.POLICY.reset(token)


def test_slurm_scheduler_gate_before_remote_execution(release):
    from aiscientist.gateway.slurm_sandbox import SlurmCodeExecutor
    skill = registry.load(sk._load_from)["skill-core"]
    token = registry.POLICY.set({**skill.requirements, "scheduler": "none", "secret_values": {}})
    try:
        executor = SlurmCodeExecutor(remote=object(), container_image="test.sif", scratch_dir="/tmp/test")
        result = executor("print(1)")
        assert "does not declare Slurm" in result["error"]
        result = executor.run_in_image("echo hello", "test.sif", workdir="test")
        assert "does not declare Slurm" in result["error"]
    finally:
        registry.POLICY.reset(token)


def test_read_reference_records_version(release, tmp_path):
    lib = registry.load(sk._load_from)
    ctx = HarnessContext(workspace=tmp_path / "run")
    result = sk.make_skill_reference_tool(lambda: lib).executor({"name": "skill-core"}, ctx)
    assert result["version"] == "1.2.3" and result["requirements"]["gpu"] is False
    assert registry.active_skill(ctx).name == "skill-core"
    assert json.loads((ctx.workspace / "manifest.json").read_text())["skills"][0]["version"] == "1.2.3"


@pytest.mark.parametrize("gpu,network", [(False, "none"), (True, "runtime"), (False, "install")])
def test_slurm_container_permissions_and_private_credentials(release, tmp_path, monkeypatch, gpu, network):
    from aiscientist.gateway import slurm_sandbox as slurm
    from aiscientist.gateway.executor import ExecResult
    skill = registry.load(sk._load_from)["skill-core"]
    skill = replace(skill, requirements={**skill.requirements, "gpu": gpu,
                    "network": network, "secrets": ["TOKEN"]})
    monkeypatch.setenv("AISCIENTIST_SKILL_SECRET_TOKEN", "private-value")
    class Remote:
        commands = []
        uploads = []
        def exec(self, command, **kwargs):
            self.commands.append(command)
            return ExecResult(command, 0, "", "")
        def put_file(self, local, remote):
            from pathlib import Path
            self.uploads.append((remote, Path(local).read_text()))
    remote = Remote()
    executor = slurm.SlurmCodeExecutor(remote=remote, container_image="test.sif", scratch_dir="/scratch")
    scripts = []
    def batch(remote, spec, **kwargs):
        scripts.append(spec.script)
        return object()
    monkeypatch.setattr(slurm, "run_batch_job", batch)
    monkeypatch.setattr(executor, "_artifact_snapshot", lambda: {})
    monkeypatch.setattr(executor, "_written_since", lambda _: [])
    monkeypatch.setattr(executor, "_sync_artifacts_back", lambda **kw: None)
    monkeypatch.setattr(executor, "_collect", lambda *a, **kw: {"status": "ok", "stdout": "private-value"})
    ctx = HarnessContext(workspace=tmp_path / "run")
    result = registry.execute(skill, ctx, executor, lambda: executor("print(1)"), "run_code")
    assert result["status"] == "ok" and result["stdout"] == "[REDACTED]"
    script = scripts[0]
    assert "--cleanenv" in script
    assert ("--nv" in script) == gpu and ("--gres=gpu:1" in script) == gpu
    assert ("--network none" in script) == (network != "runtime")
    assert "private-value" not in script
    assert remote.uploads[0][1] == "export TOKEN=private-value"
    assert remote.uploads[0][0] in remote.commands[-1] and "rm -f" in remote.commands[-1]
    assert any("umask 077" in c for c in remote.commands)


def test_restricted_tools_and_executor_capability(release, tmp_path):
    skill = registry.load(sk._load_from)["skill-core"]
    ctx = HarnessContext(workspace=tmp_path / "run")
    def forbidden():
        pytest.fail("execution must be denied")
    assert "cannot enforce" in registry.execute(skill, ctx, object(), forbidden, "run_code")["error"]
    class Executor:
        supports_skill_policy = True
    skill = replace(skill, requirements={**skill.requirements, "allowed_tools": "run_in_environment"})
    assert "does not declare tool" in registry.execute(skill, ctx, Executor(), forbidden, "run_code")["error"]


def test_registry_policy_does_not_use_uncontained_dependency_preflight(monkeypatch):
    from aiscientist.gateway.code_preflight import PreflightingExecutor
    wrapper = PreflightingExecutor(inner=lambda code: {"status": "ok"})
    def forbidden(code):
        pytest.fail("uncontained dependency preflight must not run")
    monkeypatch.setattr(wrapper, "_resolve", forbidden)
    token = registry.POLICY.set({"network": "none"})
    try:
        assert wrapper("import missing")["status"] == "ok"
    finally:
        registry.POLICY.reset(token)
