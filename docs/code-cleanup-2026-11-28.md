# Code cleanup review — 28 November 2026

Baseline: upstream synchronization commit `de19c1e`.
Branch: `refactor/ion-code-cleanup-2026-11-28`.

## Structure and removal of redundant code

The cleanup retains NetSatBench's standalone commands, `argparse` CLI,
logging conventions, JSON dictionaries, directory layout and satellite agent.
It introduces no runtime dependency or replacement orchestration framework.

| Area | Change |
| --- | --- |
| ION preparation | Separate parser construction, input validation, generator arguments, transport selection and batch preparation. Share the stop script across local and SSH preparation. Reuse the local container-existence probe. |
| ION config generation | Separate parser construction, registry/contact validation, rendering and file output. Replace five repeated per-node write/log blocks with an ordered renderer table. |
| Image distribution | Separate parser construction and the parallel load operation from the CLI driver. Preserve failure propagation and per-worker results. |
| Epoch replay | Separate parser construction from runtime operations. Preserve queue policies, synchronization and existing error boundaries. |
| Multi-node execution | Separate parser construction and simplify selection of the first failing node's exit code. Preserve complete failure summaries and streamed output. |
| Other runtime scripts | Remove unused command-result assignments and an unused exception binding; consolidate typing imports; replace placeholder-free f-strings with ordinary strings. Calls and rendered messages are retained. |
| Extension style | Add concise module/helper descriptions and normalize formatting in the maintained ION additions and edited tests. Broad upstream reformatting is avoided. |

The ION preparation entry point is now 44 lines instead of 316, and the
configuration generator entry point is 10 lines instead of 145. The work
moves the workflow into named operations; function length alone is not a
correctness or performance metric. The renderer table preserves file-write
order and does not pre-render all files before the first write.

No existing command or public helper was removed. Validation and rollback
logic remain. The old oracle-routing CLI alias remains available. Scenario
files, registries, the StarPerf gitlink, ION C patches, Dockerfile, shell
scripts and recorded experimental data are unchanged. The original working
copy and its uncommitted MATLAB edits remain separate from this worktree.

## Verification against the baseline

- 106 unit tests pass; the pre-cleanup baseline had 99.
- All 128 canonical epoch files pass their existing generation check.
- 35 baseline-versus-cleanup CLI/output comparisons pass. They cover ten CLI
  help outputs, every demonstration scenario in relative and fractional
  absolute-time modes, default-guard and registry-only generation, scenario
  listing and selected preparation argument errors.
- All 1,020 generated configuration files in those comparisons have identical
  SHA-256 hashes, including empty security configuration files.
- The production scripts and top-level unit tests pass Ruff's `F` and `E9`
  checks. All 46 tracked Python modules parse successfully; each top-level
  container shell script passes `bash -n`; `git diff --check` is clean.
- New tests cover batch rollback after a partial failure, rollback error
  reporting, rejection of unavailable local containers, matching local/remote
  stop scripts, fractional generator arguments, complete image-worker result
  counting and propagation of worker exceptions.

The byte comparisons used a temporary checkout of `de19c1e` and separate
output directories. No generated comparison output is checked into the
repository. Validation logs and the comparison manifest are retained locally
under `/tmp/netsatbench-code-cleanup-20261128/`.

No live deployment, experiment, ION rebuild or performance measurement was
run for this cleanup. Behavioral equivalence is supported by the checks
above; it is not a claim of exhaustive coverage or faster execution.
