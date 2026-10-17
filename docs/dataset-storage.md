# Dataset storage and recovery

Set `DATA_ROOT` (or `AISCIENTIST_DATA_ROOT`) to a persistent volume outside the Git
checkout. The default is `~/.local/share/aiscientist`. Back up this volume together
with the registry database configured by `AISCIENTIST_DATABASE_URL` (or
`AISCIENTIST_STATE_DIR`). Changing DATA_ROOT does not move existing files.

```
DATA_ROOT/
  datasets/<uuid>/original/   # verified uploaded files
  datasets/<uuid>/processed/  # derived dataset files
  analysis_results/
  temp/                      # resumable, unpublished transfers
  exports/
```

Dataset display names remain metadata; UUIDs identify storage, so repeated names do
not create `(1)` folders. Registry responses include UUID, owner, original name,
storage path, byte size, status, creation and update timestamps. Existing numeric
registry IDs remain valid. Startup adds and backfills the new metadata without
rewriting legacy paths. Set DATA_ROOT explicitly when using the mock console; explicitly
supplied legacy workspaces keep their old layout until migration. Production sessions
use the external default. New HPC uploads use `temp/<uuid>/original` below the account's
configured HPC upload root, then move to `datasets/<uuid>/{original,processed}` after
verification. The registry points at the final HPC path; legacy remote paths remain valid.

Uploads remain in temp until the complete manifest is verified. Explicit cancellation
removes temporary files; interruptions preserve them for resumption until the upload
TTL expires (`AISCIENTIST_UPLOAD_TTL_SECONDS`, default `86400`, or 24 hours).
The gateway sweeps disconnected sessions at startup and at most every five minutes;
a resume request also checks expiry. Failed HPC transfers use this TTL unless
`AISCIENTIST_UPLOAD_KEEP_SETTLED_DAYS` overrides it. If SSH is disconnected at expiry,
local bytes are removed and a small cleanup record defers removal of the remote temp
folder until that HPC account reconnects. Dataset deletion removes the filesystem content
before committing the row deletion; a disk failure leaves the row available for
retry and reports an error. Overlapping registry paths cannot be deleted together.

## Reconciliation

Authenticated `POST /api/datasets/reconcile` accepts:

```json
{"cleanup": false, "all_owners": false}
```

Dry-run is the default. A user's scan reports their registry rows whose paths are
missing inside managed storage or known local legacy upload roots. Remote HPC paths
and external bound paths are excluded. Administrators can set `all_owners: true` to also find unregistered UUID
folders under DATA_ROOT/datasets and legacy uploads under
`AISCIENTIST_RESULTS_DIR/<owner>/uploads` and
`AISCIENTIST_LEGACY_RESULTS_DIR/<owner>/uploads` (the latter defaults to the old
checkout `runs/console` directory). Legacy roots are limited to registered owners. `cleanup: true` removes the reported missing rows
and orphan UUID directories. Owner-scoped calls never disclose or remove unowned
folders. Symlinks and hidden staging files are excluded from automatic orphan deletion.
Legacy upload folders retain their original names in the report; cleanup checks all
registry references before removing one. Non-UUID names in managed datasets require
manual inspection. The response includes
`missing`, `orphans`, and filesystem `errors`.

## Non-destructive legacy migration

First back up the database and data, then stop uploads and analyses involving the
dataset. Preview an authenticated `POST /api/datasets/migrate` request:

```json
{"dataset_id": 123, "apply": false}
```

The response shows source and destination. Set `apply: true` to copy the owned
legacy dataset into its UUID directory (symlinks are rejected) and update its registry path. The source
remains untouched. Existing destination files are never overwritten; an interrupted
copy requires inspection of its temp migration folder before retrying. Keep legacy
sources until their migrated datasets and backups have been validated. Restore the
registry and data together so recorded UUIDs continue to match their directories.
