# Upstream synchronization review — 28 November 2026

## Scope and baseline

This branch merges `mSvcBench/NetSatBench:main` at
`8ddc9c4` into the ION-enabled fork at `80bc341`.
The common ancestor is `4f19dc5`. Upstream contributes 13 commits; the fork
contributes 11 commits since that ancestor. This synchronizes the fork; it does
not change the separately maintained ION-DTN source or binary image.

The work was performed in a separate Git worktree on
`sync/upstream-2026-11-28`. The original `main` checkout and the uncommitted
StarPerf MATLAB visualization changes remain untouched. The StarPerf gitlink
remains `70a63f2f1b23aecb77cfd51dc0803e2a27d3532a` in both parents.

## Incoming changes retained

- Worker-local Docker bind mounts through the node `volume` field.
- Soft restarts, selection by node type or all nodes, and bounded restart
  concurrency.
- Quoted remote execution, with a single shell-command string executed by
  `bash -c` inside the target container.
- Per-node output and complete failure summaries for `exectype`.
- Oracle route batching into separate `run` entries, including batches with
  zero sleep between them.
- Startup handling of a non-empty epoch queue, expanded setup instructions,
  and the upstream removal of 478 generated Iridium example files. These
  removals originate upstream; campaign results and evidence are outside
  their scope.

## Conflict resolutions and integration fixes

`README.md` and `control/nsb-deploy.py` required manual conflict resolution.
The README retains the upstream organization and setup instructions alongside
ION documentation, the actual `worker-config.json` path, reachable node-side
Etcd addressing, and the registered `system-clean-docker` command.
Deployment now accepts both the upstream `volume` parameter and the fork's
node-scoped `env` settings.

Container recreation during node restart also carries `env` through to
`docker run`, preserving settings such as `ION_RFXCLOCK_POLL_MSEC`. A soft
restart uses the existing container and does not apply changed image,
resource, volume, or environment settings.

`run --queue-policy {ask,clear,keep,abort}` makes queue handling explicit.
The default preserves the terminal prompt; a non-interactive process returns
an error when the queue is non-empty instead of blocking. `clear` only unlinks
files and symlinks and stops replay if deletion fails. It does not recursively
remove directories or follow symlinks. The Etcd readiness check now validates
the first node too; testing `any()` against its iterator previously consumed
that entry before validation.

Oracle routing uses the new `--max-routes-per-command` option. The former
`--max-routes-per-epoch` spelling remains a CLI alias. The subsecond regression
test follows the upstream internal parameter name and still checks a
millisecond-precision route-epoch shift.

## Verification

- Baseline: 86 unit tests pass with the host Python environment.
- Integrated branch: 99 unit tests pass, including 13 new compatibility tests.
- The scenario checker verifies all 128 checked-in epoch files.
- All Python sources in `control/`, `utils/`, `sat-container/`, and `test/`
  parse successfully. Each top-level container shell script passes `bash -n`.
- Ten CLI help checks pass with real dependencies, covering initialization,
  deployment, restart, replay, execution, ION preparation/generation, and
  oracle routing. Both oracle batching option names are exposed.
- Git reports no unresolved merge entries or whitespace errors.

The new checks exercise volume/environment preservation, soft restart,
queue cleanup and refusal paths, first-node readiness, remote command argument
boundaries, reporting all failed nodes, and route batching. SSH, Docker and
Etcd operations in the unit tests are mocked; no workers are changed.

Reproduce the test suite from this branch:

```bash
python3 -m unittest discover -s test -p 'test_*.py' -v
```

The existing `.venv-live` has a pre-existing NumPy/SciPy binary incompatibility:
the baseline itself cannot import `scipy.sparse` there. It was not modified.
The integrated suite and CLI checks also passed in a separate validation venv,
using the host NumPy/SciPy and installing `etcd3==0.12.0`,
`protobuf==3.20.3`, `watchdog==6.0.0`, `networkx`, `pymetis`, and `prettytable`.

## Remaining validation boundary

No live satellite experiment, image rebuild, MATLAB execution, or physical
multi-worker deployment was performed for this synchronization. The tests
establish source-level compatibility and mocked orchestration behavior;
they do not establish daemon readiness or measured end-to-end performance
of a newly deployed image. The ION source, runtime staging and recorded
experimental results are unchanged by this work.
