# Working on NetSatBench

NetSatBench uses standalone Python commands, JSON scenario files and a
per-container satellite agent. Keep changes consistent with that structure.
The ION extension follows the same command dispatch and logging conventions.

## Code organization

- `nsb.py` maps subcommands to scripts. Keep command names, flags and script
  paths stable unless a change explicitly requires a migration.
- `control/` contains scenario initialization, deployment, replay and runtime
  preparation. Keep CLI argument definitions in `create_parser()` when a
  command needs to separate parsing from its workflow.
- `utils/` contains configuration generators, inspection and execution tools.
  Keep rendering functions separate from file writes and remote operations.
- `sat-container/` contains the container runtime, the link agent and image
  staging scripts. Keep ION source patches separate from Python orchestration.
- `test/test_*.py` contains the unit and orchestration regression tests.
  `test/handover/` contains the original experiment programs, rather than
  standalone unit tests.
- `generators/StarPerf_Simulator` is a Git submodule. Do not include unrelated
  local generator or MATLAB changes in an orchestration cleanup.

Use small functions for validation, command construction and execution when
those responsibilities make a command difficult to follow. A helper should
name a useful operation; avoid wrappers that merely rename one expression.
Retain the existing `argparse`, logging and dictionary-based configuration
style instead of introducing another command framework or configuration model.

## Readability and compatibility

Use descriptive `snake_case` names and short docstrings that explain a
function's purpose or a non-obvious constraint. Follow adjacent upstream code
when editing existing scripts. Keep automatic formatting limited to the
extension files being maintained; the ION additions use a 100-character line
length. Do not reformat the whole upstream codebase to make a small change.

Remove unused bindings while preserving calls that perform work. Keep
rollback, argument validation, command quoting, time precision checks and
runtime verification even when they add lines. They enforce operational
contracts and are not redundant boilerplate.

Treat generated config contents, CLI defaults, accepted input formats, exit
codes, and cleanup behavior as compatibility boundaries. A behavior-preserving
refactor should compare those outputs against its baseline, in addition to
passing unit tests. Keep shell-command construction separate from execution
so tests can verify where arguments and quoting boundaries land.

## Validation

Run from the repository root with an environment containing the project
requirements:

```bash
python3 -m unittest discover -s test -p 'test_*.py' -v
```

The tests check the canonical generated epochs as well as the Python helpers.
They mock worker, Docker and Etcd operations; successful unit tests do not
prove daemon readiness or end-to-end delivery on a live cluster.

For edited shell scripts, run `bash -n` on each script. Use `git diff --check`
before committing. If Ruff is available in a development environment, check
syntax, undefined names and unused bindings with:

```bash
ruff check --select F,E9 control utils sat-container nsb.py test/test_*.py
```

Ruff is optional development tooling, not a runtime dependency. Historical
handover programs are not included in this lint command.
