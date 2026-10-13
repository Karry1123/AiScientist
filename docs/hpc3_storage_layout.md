# HPC3 storage layout, the 3-day Temp sweep and the session cleanup

**Status:** implemented; the HPC3 side is **created and live-verified**. Needs an eyeserver
restart (and a `.env` review — see [Deploy](#deploy)) to take effect in prod.

The asset inventory of everything we own on HPC3 is a separate doc: `docs/hpc3_assets.md`.

## The problem this fixes

Results land on the **eyeserver** — `<AISCIENTIST_RESULTS_DIR>/<ucinetid>/<run_id>/artifacts/`
(prod: `/data/aiscientist`). Every HPC3 executor mirrors its `artifacts/` back there when a step
finishes, so the deliverables are never stranded on the cluster.

The **process files** are a different story. Until this change, every HPC3-offloaded step wrote
into the member's **personal** lab dir:

| what | where it used to go |
|---|---|
| scanpy work/ checkpoints + artifacts | `/dfs3b/ruic20_lab/<user>/analysis/<run_id>/` |
| offline-VEP variant workspace | `/dfs3b/ruic20_lab/<user>/variant/<run_id>/` |
| LIRICAL phenotype workspace | `/dfs3b/ruic20_lab/<user>/phenotype/<run_id>/` |
| scGPT staged query + predictions | `/dfs3b/ruic20_lab/<user>/scgpt/<run_id>/` |
| pandoc/xelatex render bundle | `/dfs3b/ruic20_lab/<user>/reports/` |
| `run_code` snippets, sbatch scripts, args/result JSON, Slurm logs | `/dfs3b/ruic20_lab/<user>/.bioagent/{runcode,analysis,variant,phenotype,paperqa}/` |

and **nothing ever cleaned any of it.** The product's only GC (`app._expire_old_checkpoints`,
`AISCIENTIST_CHECKPOINT_TTL_DAYS`, default 7d) sweeps the eyeserver's local run bundles and never
touches HPC3. The only HPC3-side deletion was a human clicking an item in the storage panel.

That was not fixable in place: you cannot safely automate `rm -rf` against a directory people
also keep hand-curated data in. So the machine-generated files moved out.

Measured 2026-08-07: `ruic20_hpc` on dfs3b was at **595.26 TiB of 600 TiB (99.2 %)** — about
4.7 TiB free for the whole lab. Our own leftovers were 11 G (`analysis`) + 17 G (`variant`) for
one member alone.

## The layout

```
/dfs3b/ruic20_lab/software/AiScientist/  <- AISCIENTIST_HPC_SHARED_ROOT (was software/bioagent)
├── containers/ hf/ envs/ ollama/       <- 350+ GB of assets, NEVER swept (docs/hpc3_assets.md)
│   scgpt_model/ vlreview_model/
├── Temp/<ucinetid>/                    <- process files: removed when their session disconnects,
│   │                                      and SWEPT after 3 days whatever happened to the session
│   ├── analysis/<run_id>/{work,artifacts,result}/
│   ├── variant/<run_id>/
│   ├── phenotype/<run_id>/
│   ├── scgpt/<run_id>/
│   ├── reports/<run_id>/               <- the render bundle (was one shared reports/ until 2026-10-13)
│   ├── server-data/<session>/          <- this session's copies of datasets bound from the server
│   ├── paperqa/
│   └── scratch/{analysis,runcode,variant,phenotype,paperqa,report,worker}/
│                                       <- each job's sbatch script, args/result JSON, Slurm log
│                                          and tool log (see "Job files" below)
├── uploads/<ucinetid>/                 <- raw research data, NEVER swept
├── pysrc/<ucinetid>/                   <- synced AiScientist source, rewritten each connect
└── bin/<ucinetid>/                     <- staged from deploy/hpc3/ at each connect:
    ├── temp_gc.sh                         the sweeper
    └── session_clean.sh                   the session cleanup
```

Temp sits in the *same* root as the containers and model weights, so the sweeper's confinement to
`<root>/Temp` is load-bearing rather than cosmetic. It is enforced in the script and proven live:
a decoy asset dir with a 10-day-old file, placed next to the real `containers/`, survived a real
sweep untouched.

Deliberately **not** in scope of any automatic deletion:

* `uploads/` — raw uploaded data. Manual deletion only, from the storage panel or the dataset
  list. (Long-standing rule: 误删研究数据的风险 > 存储成本.) With `AISCIENTIST_UPLOADS_ON_HPC` a
  file uploaded through the browser is moved here and removed from the server, so this is its only
  copy: no session end and no sweep may take it. The one exception is not an upload: the old
  `uploads/<ucinetid>/server-data/<root>/` mirror of server-side data (see "When a session
  disconnects").
* `pysrc/` — bind-mounted read-only by running jobs for their whole lifetime.
* `<shared_root>/{containers,hf,envs,ollama,scgpt_model,vlreview_model}/` — the built assets.
* `/dfs3b/ruic20_lab/<ucinetid>/` — **every member's personal folder stays untouched.** Old runs
  that predate this change are still there, still browsable, and are only ever removed by hand.
* `$HOME/.bioagent/` — the vLLM serve job's own files only: `serve.<partition>.sbatch`,
  `vllm.<jobid>.port` and `vllm.<jobid>.spec` (how the gateway finds and steers a running serve
  job), plus that job's log `$HOME/bioagent-vllm-<jobid>.log`. No analysis, report or worker job
  writes there any more (see "Job files" below).

## Job files

Every job a run starts on HPC3 has a few small files of its own: the sbatch script, the args JSON
it is given, the result JSON it prints, the Slurm log (`#SBATCH --output`) and the tool's stderr
log. They all go to `Temp/<ucinetid>/scratch/<line>/`, one directory per job line:

| job line | directory under `Temp/<ucinetid>/` |
|---|---|
| scanpy analysis tools (QC, clustering, DE, CellQC ...) and image pulls | `scratch/analysis/` |
| `run_code` snippets | `scratch/runcode/` |
| offline VEP variant annotation | `scratch/variant/` |
| LIRICAL phenotype | `scratch/phenotype/` |
| PaperQA literature | `scratch/paperqa/` |
| report rendering (pandoc / xelatex), including the staged `render_<n>.tgz` | `scratch/report/` |
| the session's CPU worker (holder script + log) | `scratch/worker/` |
| scGPT annotation, render review | beside their output: `scgpt/<run_id>/out/`, `vlreview/<run_id>/pass<n>/out/` |

The gateway takes each of these from one function (`app._scratch_dir`), and the job classes have
no default directory: a job line that names none fails when it is built instead of writing
somewhere else. The paths are absolute on purpose. Slurm does not expand `$HOME` in
`#SBATCH --output`, so a path with a shell variable in it is created as written.

Until 2026-10-12 some of these lines did not follow the layout above: the scanpy analysis line and
the report renderer wrote to `$HOME/.bioagent/{analysis,report}/`, the scGPT and review scripts went
to `$HOME/.bioagent/jobs/`, and the CPU worker's log to a directory literally named `$HOME` inside
the home directory (RCHENLAB/AiScientist issue 2). Nothing in the home directory is swept, so those
files only accumulated; in `Temp` they are removed with the rest once they go cold.

## The sweep

`deploy/hpc3/aiscientist_temp_gc.sh`, staged to `<shared_root>/bin/<ucinetid>/temp_gc.sh`.

**It runs as a Slurm batch job, never on a login node.** The sweep walks directory trees and calls
`rm -rf` — real filesystem work, and RCIC's rule is that login nodes are for logging in and
*submitting* jobs. So the gateway's login-node session only ever runs one `sbatch --wrap`; the
`find`/`rm` happen on a compute node in the free `standard` partition (1 CPU, 2 G, 30 min cap).

A **unit** is one `Temp/<user>/<kind>/<entry>` directory (a run dir, or a flat scratch dir with
no per-run subdirs). A unit is deleted only when **its entire subtree** has gone untouched for
`--ttl-days`. That all-or-nothing rule is the safety property: a long-running job keeps writing,
so it can never be half-deleted out from under itself, and a run with mixed-age files is either
fully cold or fully kept.

Guards, all enforced by the script itself and covered by `tests/test_hpc_temp_gc.py`:

* `--root` must be absolute, contain no `..`, and not be `/`; only `<root>/Temp` is walked.
* `--ttl-days` must be a non-negative integer; `0` disables the sweep and exits 0.
* `--user` must be a bare account name — the gateway always passes the logged-in user, so a
  session only ever sweeps its own subtree.
* `--dry-run` prints and removes nothing.

Verified live on HPC3 in the real location (2026-08-08), end to end through Slurm: jobs 55150830 /
55150832 ran on compute node `hpc3-l18-05`, state COMPLETED, and left `GC_RESULT removed=1 kept=1`
in the log, which the next submit read back. Cold units removed; a warm run kept *including* its
10-day-old files; a decoy dir beside the 103 GB of real assets untouched; another user's Temp,
`uploads/` and `pysrc/` untouched; all guards reject (relative root, `..`, `/`, non-integer TTL,
an injected `--user`).

Because the sweep is asynchronous, what the console can report *now* is what the PREVIOUS sweep
deleted — read from its job log (`<shared_root>/bin/<ucinetid>/temp_gc.log`, one small `tail`).
Housekeeping does not need a synchronous answer, and this keeps the login node idle.

### When the sweep runs

1. **On every connect** — `app._prepare_shared_storage` creates this user's dirs and submits.
2. **Every 6 h per live session** — `app._hpc_temp_gc_loop`.
3. **Cron backstop** (optional, per member). Note the cron line *submits* rather than sweeping in
   place, for the same reason:

   ```
   0 3 * * * sbatch --job-name=aiscientist-tempgc-$USER --partition=standard --account=ruic20_lab --cpus-per-task=1 --mem=2G --time=00:30:00 --wrap "bash /dfs3b/ruic20_lab/software/AiScientist/bin/$USER/temp_gc.sh --root /dfs3b/ruic20_lab/software/AiScientist --ttl-days 3 --user $USER --quiet"
   ```

   `scrontab` is **disabled** on HPC3 (`scrontab: fatal: scrontab is disabled on this cluster`),
   so a Slurm-native cron is not an option; `/etc/cron.deny` is empty, so ordinary `crontab` is.

## When a session disconnects

The sweep removes by **age**, and only when the same user connects again. So until 2026-10-13 a
session's files outlived it by at least three days, and its copy of a dataset bound from the server
sat under `uploads/`, which nothing sweeps (RCHENLAB/AiScientist issue 4: 14 GB of BAM left three
times over in one member's uploads). A disconnect now removes by **owner**:

| removed at disconnect | why it is safe |
|---|---|
| `Temp/<ucinetid>/<kind>/<run_id>/` for every run this session started or resumed (analysis, variant, phenotype, reports, scgpt, vlreview, corpus) | the deliverables are already on the server (`artifacts/`); what goes is the working copy |
| `Temp/<ucinetid>/server-data/<session>/` | a copy of data that lives on the server; the next session copies it again |
| `uploads/<ucinetid>/server-data/<root>/` | the same copies, from before they were per session; nothing reads them any more |

`deploy/hpc3/aiscientist_session_clean.sh`, staged to `<shared_root>/bin/<ucinetid>/session_clean.sh`
at connect. `/api/disconnect` submits it as one `sbatch --wrap` on the free CPU partition (job
`aiscientist-sessionclean-<ucinetid>`, log `bin/<ucinetid>/session_clean.log`) and then closes the
SSH session; the job needs neither. The gateway decides the targets (`app._session_cleanup_targets`)
and the script removes exactly those: it never lists a directory to choose what to delete.

What a disconnect does **not** remove:

* **A run that is still executing**, and the earlier run a continuing run reads its dataset from.
  Their folders, and with them the session's `server-data/` copies, are left to the sweep.
* **A run another live session also knows** (the same run resumed from a second device).
* **Anything of another session.** Server data is copied per session for this reason: two sessions
  bound to the same folder each have their own copy, so one ending cannot take the other's files.
* **Uploads and saved results.** A dataset uploaded through the browser, and anything "Save to my
  data" moved into the user's folder, is never a target.
* **`scratch/` and `paperqa/`**: per-job scripts and logs, shared by the user's sessions. The sweep
  removes them once cold.

What it costs: after a disconnect a run can no longer be continued from a **middle** step, and its
processed dataset or CellQC result directory is gone unless it was saved to the user's data first
(the console says "kept on HPC3 until you disconnect"). The report, figures and tables are on the
server and stay.

Why a middle step and not the rest: continuing from step *k* reads the AnnData checkpoint step
*k-1* wrote (`analysis/<run_id>/work/adata_*.h5ad`, hundreds of MB to several GB). Those exist only
on HPC3: the server receives a run's `artifacts/` and removes its own `work/` when the report is
written, so there is nothing on the server to copy back. What the server does hold is the *input*:
a run resumed in a later session (from its first step, or an interrupted run) has its server-side
datasets copied into that session again and its stored paths repointed
(`app._restage_server_data`). To carry a finished analysis across sessions, save the processed
dataset ("Save to my data", or the run option that saves it at completion): a later message then
starts a new run from it and skips the steps already done.

**Not covered: a session that ends without a disconnect.** Closing the browser tab, a gateway
restart, or a dropped connection run no cleanup: the run may still be executing, and an
interrupted run is resumed from these very files. Those sessions fall back to the sweep, which
still runs only when that user connects again.

Verified on HPC3 (2026-10-13, compute node `hpc3-l18-04`, bash 5.1.8, GNU find 4.8.0), in a
throwaway tree: the named run's folders under four kinds, the session's copies and the old mirror
removed; another session's run and copies, another member's run of the same name, an upload and
`containers/` untouched; `..`, a name with `/`, and a call without `--user` all exit 2 with nothing
removed. `tests/test_session_cleanup.py` runs the same script against a real tree.

### What still touches a login node

`mkdir`/`chmod`/`test -d` at connect (you cannot submit a job before your dirs exist), the `tail`
of the last job log, the `sbatch` itself, and the storage panel's `du -sh`. All are metadata-only
and user-initiated — the class of thing a login node is for. No `find`, no `rm -rf`, no bulk
transfer: staging goes over `access-hpc3` (the DTN) via `put_file`.

## Config

| env var | default | meaning |
|---|---|---|
| `AISCIENTIST_HPC_SHARED_ROOT` | `/dfs3b/ruic20_lab/software/AiScientist` | shared project root |
| `AISCIENTIST_TEMP_TTL_DAYS` | `3` | age after which a cold Temp unit is removed; `0` disables |
| `AISCIENTIST_SESSION_CLEANUP` | `1` | remove a session's own Temp files when it disconnects; `0` leaves them to the sweep |

Both also accept the `AISCIENTIST_*` prefix (`core.config.apply_brand_env_aliases`).

## Deploy

The HPC3 side is **done** (2026-08-08): the root exists, is `drwxrwsr-x` (2775) with setgid, and
`Temp/ uploads/ pysrc/ bin/` are created group-writable so any member's session can make its own
per-user subdir. `ensure_shared_dirs` was run against the real cluster and both a dry-run and a
real sweep came back clean.

What is left is the eyeserver:

1. Restart the gateway so the new defaults load.
2. **Check `/data/aiscientist/app/.env` for pinned `software/bioagent` paths.** They still resolve
   through the compat symlink, so nothing breaks either way — but update them so the symlink can
   eventually go. `AISCIENTIST_HPC_SHARED_ROOT` itself needs no entry unless you want a non-default.
3. Optional per-member cron backstop (above).

`/dfs3b/ruic20_lab` (top level) is `drwxr-s--- ruic20 ruic20_hpc` — group members cannot mkdir
there, and `newgrp`/`sg` do not change that (supplementary groups already count for the access
check; the group simply has no `w`). If you ever want the root at `/dfs3b/ruic20_lab/AiScientist`
instead, someone with `ruic20` rights has to create it, and `AISCIENTIST_HPC_SHARED_ROOT` moves there
with no code change.

## Migrating the old files

Nothing is moved or deleted automatically. Existing
`/dfs3b/ruic20_lab/<ucinetid>/{analysis,variant,phenotype,reports,scgpt,.bioagent}/` stay exactly
where they are; a member can review and delete them from the storage panel, which now lists all
three areas (Temp, Uploads, personal lab folder) with their sizes.

The same holds for the job files left in the home directory before 2026-10-12 (see "Job files"):
they are not written any more and nothing deletes them. The storage panel does not list the home
directory, so remove them by hand once no run is in progress:

```
rm -rf ~/.bioagent/analysis ~/.bioagent/report ~/.bioagent/runcode ~/.bioagent/variant ~/.bioagent/jobs
rm -rf ~/'$HOME'        # the directory literally named $HOME — keep the single quotes
```

Leave `~/.bioagent/` itself and the `serve.*`, `vllm.*` files in it: a running serve job reads them.
