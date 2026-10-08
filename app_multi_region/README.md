# Distributed source generator

Run setup once, then launch bounded workers. The existing products, users,
orders, and line_items schemas remain unchanged. Only AU, UK, and US receive
traffic. All commands use `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, and
`DB_PASSWORD` for the source PostgreSQL connection.

```bash
uv sync --frozen
uv run python main.py setup --run-id normal-001 --profile normal --workers 3 --seed 42
uv run python main.py run --run-id normal-001
uv run python main.py report --run-id normal-001
```

`run` launches the configured number of local worker processes and waits for
all of them. Each worker has one database connection. The local coordinator
has one additional connection. It exits nonzero if workers fail or miss rows.
The report contains configuration, start time, actual committed source-row
counts by region/operation/table, and missed rows per worker. It contains no
credentials and excludes setup rows.

For separate containers or hosts, set the common start time once, then launch
one process for each worker ID from zero through `workers - 1`:

```bash
uv run python main.py start --run-id normal-001 --delay 30
uv run python main.py worker --run-id normal-001 --worker-id 0
# Launch IDs 1 and 2 on the other workers before the start time.
```

PostgreSQL supplies the shared clock. Calling `start` again preserves the
original window. Worker IDs are fixed for the run. A session advisory lock
rejects two live processes claiming the same worker ID.

## Workload controls

| Option | Default | Meaning |
| --- | --- | --- |
| `--profile` | `normal` | Normal or load profile |
| `--seed` | `1` | Deterministic payload choices |
| `--workers` | `1` | Total workers sharing the global row budget |
| `--baseline` | Profile value | Sustained source row changes per minute |
| `--peak` | Profile value | Burst source row changes per minute |
| `--duration` | `900` | Measured seconds, excluding setup and start delay |
| `--bursts` | `300,600` | Burst start seconds; an empty string disables bursts |
| `--burst-duration` | `60` | Seconds per burst |
| `--regions` | `au,uk,us` | Ordered list containing exactly these regions |
| `--region-weights` | `1,1,1` | Positive integer weights in region order |
| `--operation-mix` | `50,45,5` | Positive integer insert/update/delete weights |
| `--bad-data-rate` | `0.02` | Deliberate payload data-quality defects |

Normal uses 5,000 sustained and 100,000 peak row changes per minute. Load uses
20,000 and 400,000. Both last fifteen minutes, with sixty-second bursts at
minutes five and ten. Their scheduled totals are 265,000 and 1,060,000 source
row changes. Setup is outside these totals. Downstream copies and tombstones
never count toward input load.

Scheduling uses one-second budgets and carries fractional rows across seconds
before allocating work. Workers receive complete operation/region cycles in
rotation, so adding workers does not multiply the rate. Individual workers may
be idle during a short slot. Rounding can leave a partial cycle at the end of
a custom workload; the default profiles have exact operation totals and region
counts differing by at most one row.

Use a short run to verify connectivity without running the full profile:

```bash
uv run python main.py setup --run-id smoke-001 --workers 3 --duration 8 \
  --baseline 7200 --peak 14400 --bursts 2,5 --burst-duration 1
uv run python main.py run --run-id smoke-001
```

## Setup, ownership, and restarts

Setup creates a `generator_control` schema for run configuration and worker
checkpoints. Keep it outside the CDC publication/table include list. The
existing connector's explicit regional table selection already excludes it.
Setup needs schema/table creation privileges; workers need source DML and
control-table access. No cloud resources are created by these commands.

Setup serializes concurrent callers and commits seed rows with the run record.
Repeating the same configuration is a no-op. Reusing a run ID with different
settings fails. A new ID seeds a separate owned set of rows, retaining prior
runs. Setup takes place before the measured window.

Each worker owns bounded mutation pools and stable reference rows in every
region. Orders refer to stable users; line items refer to stable products and
orders. Deleting a mutable row therefore needs no cascade. Each operation
changes exactly one source row. New inserts enter the mutation pool; older
rows remain in the source when they leave the pool. No padding or ownership
columns are added to application tables.

Workers commit at most 100 row changes with their checkpoint in one transaction.
A restart reloads committed progress and ownership. An interrupted transaction
rolls back together with its checkpoint. Payload choices are seeded per event;
serial IDs and database timestamps still depend on the database and execution
time. Reproducibility requires the same configuration and locked dependencies.

A late worker skips expired slots, records missed rows, and fails the run.
It never catches up by moving old traffic into a later burst or extending the
window. Statement timeouts and a transaction deadline check bound writes at
the end of the run. Setup reruns and completed worker restarts do not generate
new traffic. If the database is unreachable, the command fails rather than
promising a clean completion. Use a new run ID for another measured attempt.

The local smoke test proves behavior, not capacity at the peak load profile.
Full-rate distributed execution and ECS lifecycle checks belong to deployment
validation.

## Verification

```bash
uv run python -m unittest discover -s tests -v
```

The PostgreSQL checks are opt-in and require an isolated disposable database.
They create regional seed data, control tables, and temporary audit triggers.
Set the database connection variables, then run:

```bash
GENERATOR_TEST_POSTGRES=1 uv run python -m unittest discover -s tests -v
```

These checks compare committed source operations with the run report, exercise
all four entity tables and all three regions, reject duplicate workers, and
verify setup reruns, transaction rollback, checkpoint recovery, and completed
run restarts. Controlled-clock checks cover both full fifteen-minute profiles.
