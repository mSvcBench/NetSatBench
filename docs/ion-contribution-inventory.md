# ION-DTN contribution inventory

Scope: classic `feature/ion-dtn-guard-seconds`, integration commit `0253f0b`.
Comparison base: `47f12a2`, the parent of the first ION integration commit.
This is a historical contribution boundary, not a comparison with today's
upstream HEAD. The subsecond worktree is intentionally excluded.

## Main additions

| Component | Responsibility |
| --- | --- |
| `nsb.py` | Registers ION preparation, config/scenario generation and image distribution commands |
| `control/nsb-ion-prepare.py` | Generates plans, distributes per-node files, starts ION and rolls back failed preparation |
| `utils/nsb-ion-generate-configs.py` | Converts epoch windows and registry settings into guarded ION contact/range and node configurations |
| `utils/nsb-ion-generate-demo-scenarios.py` | Generates and checks deterministic demonstration epochs |
| `utils/nsb-load-image.py` | Distributes a local image to selected workers |
| `control/nsb-run.py` | Extends epoch replay and shared absolute-time handling |
| `control/nsb-deploy.py`, `control/nsb-node-restart.py` | Support image pull policy; deploy also warns about container-local Etcd addresses |
| `sat-container/sat-agent.py` | Link reconciliation and runtime networking changes; shared code, not ION-only |
| `sat-container/Dockerfile.ion`, staging and patch scripts | Build the separate ION runtime image without replacing the original Dockerfile |
| `sat-container/patches/` | ION fixes for repeated-file references, fragment queue ordinals and UDP receive buffer request |
| `examples/10nodes-ion-dtn/` | Topology, registry, seven scenarios and run instructions |
| `test/test_*.py` | Generator, scenario, preparation and link-reconciliation regression checks |

Documentation, diagram edits and removal of tracked bytecode are listed below
but are not runtime ION features. Nothing in this list implies every change
is suitable for an upstream PR without separate review.

## Delivery-time style pass

Preserve upstream's standalone scripts, existing argparse commands, logging,
function names and directory structure. Section separators in the four new
Python command scripts follow the existing control-script convention. No new
framework, dependencies, timing defaults, protocol behavior or public options
are introduced. No original implementation is removed merely to reduce diff size.

Validation: 44 local regression tests pass and the scenario checker verifies
118 epoch files. Python AST equality against the pre-style commit checks that
this style pass changes comments only. These checks do not replace live network
or clean-host installation validation.

## Line-by-line code review (2026-09-07)

Reviewed all added source lines and changed hunks in the 20 Python, shell,
Dockerfile and C-patch files in the comparison below. New Python tools and
the four regression test files were read in full; modified original files
were reviewed through their complete diffs and relevant surrounding functions.
This is not a line-by-line audit of every unchanged upstream file, all prose,
all diagram XML, or the separate subsecond repository.

### Style conclusion

The extension follows the original standalone-script architecture: snake_case
functions, argparse entry points, JSON dictionaries, per-command logging,
Etcd lookups, subprocess calls and thread-based parallelism. Emoji-prefixed
logs and section banners already exist upstream; they are not introduced by
this extension. Original files themselves mix type annotations, quote styles,
short and long functions, and error-reporting conventions. There is no basis
for presenting one perfectly uniform upstream style as a requirement.

The new generator is more explicit and vertically formatted than some original
scripts, with more validation and a small dataclass. Those differences are
reasonable, not grounds for removing validation or flattening the dataclass.
Long main functions, duplicated local/remote startup strings, and inconsistent
CLI exception handling are more useful cleanup targets than cosmetic banners.
The existing shell staging helper is more defensive than many upstream scripts;
retain its temporary staging and rollback design.

### Functional findings, separate from style

Follow-up fixes: findings 1–7 below are now addressed in source. Image loading
uses one quoted SSH command and Docker's direct archive input, without a shell
pipeline. Local and remote ION startup share a fail-fast script. Docker builds
require all staged core executables and resolvable dynamic libraries through
`verify-ion-runtime.sh`, without invoking ION admin commands during the build.
Absolute replay rejects incompatible fixed-wait, resume and loop modes. CA-only
Etcd settings are retained by the control and utility clients and propagated to
containers. Fractional bit rates use a mathematical ceiling. Runtime staging
only replaces directories bearing its managed marker. The original finding
descriptions remain below as the review record; line numbers refer to the
reviewed pre-fix version.

Validation after the follow-up: 85 unit tests pass, including shell argument
preservation, each failed admin stage, local/remote script consistency, and
runtime-check failure paths; 128 epoch files still match. The image was not
rebuilt or deployed in this follow-up. Dependency validation is not daemon
readiness: a live startup and bundle pilot is still required for a new image.

1. **High — remote image-load command loses shell grouping.**
   `utils/nsb-load-image.py:128` passes `sh`, `-lc`, and a command as separate
   SSH arguments without protecting the remote command string. At line 177,
   the archive path is also interpolated unquoted into a pipeline and cleanup.
   A harmless local reconstruction of the remote shell text
   `sh -lc printf %s REVIEW_OK` exits 2 rather than printing the marker.
   Quote both the remote script and path; test paths containing spaces and
   shell metacharacters without making a real SSH connection. This is a remote
   distribution problem, not proof that local preloaded-image pilots fail.

2. **High — startup can mask an earlier admin failure.**
   `control/nsb-ion-prepare.py:282` and the remote startup string at line 335
   run ionadmin, ionsecadmin and bpadmin sequentially without fail-fast chaining.
   A shell returns the final command's status; `false; true; true` returns 0.
   Also verify daemon readiness rather than interpreting a successful admin
   invocation alone as successful startup. Cover local and remote paths.

3. **High — image build checks suppress runtime failures.**
   `sat-container/Dockerfile.ion:27` combines chmod/ldconfig/admin probes with
   `|| true`. Failure can be hidden, including an earlier step in the AND/OR
   chain. A staged host binary may also have incompatible runtime libraries.
   Separate mandatory loader/executable checks from help commands with unusual
   exit codes. Rebuild verification is required; do not silently replace an
   already-tested image for a presentation.

4. **Medium — absolute scheduling does not compose safely with resume/loop.**
   `control/nsb-run.py:499` slices the file list for resume before deriving the
   first epoch timestamp at line 517. Reusing the original T0 then reanchors
   resumed offsets incorrectly. Looping at line 530 resets only TIME_OFFSET,
   not the absolute T0, so later cycles can replay past deadlines immediately.
   Define these combinations explicitly or reject them. Single-pass absolute
   replay is a distinct case; do not label all time scheduling broken.

5. **Medium — CA-only Etcd settings are dropped.**
   `control/nsb-ion-prepare.py:141` and `utils/nsb-load-image.py:56` pass ca_cert
   only when both username and password are present. Similar conditional code
   exists upstream, but copying it does not validate CA-only connections.
   Add mock-client tests; the default isolated plaintext lab is unaffected.

6. **Low for supplied scenarios — fractional bit-rate rounding is incorrect.**
   `utils/nsb-ion-generate-configs.py:302` uses an integer ceiling idiom on
   floats. Locally reproduced: 8.1 bit/s becomes 1 B/s rather than ceil(8.1/8)
   = 2 B/s. Add fractional and non-finite input cases. Existing scenario rates
   do not establish this edge case's correctness.

7. **Medium — staging output path guard is not a general safe-path guarantee.**
   `sat-container/stage-ion-runtime.sh:10` rejects root/current/prefix equality,
   but other pre-existing user directories can still be renamed and eventually
   removed. Keep the documented dedicated ion-runtime destination; require an
   explicit managed-directory marker or overwrite confirmation before accepting
   arbitrary existing output paths. Do not test this with real user directories.

Additional follow-up fixes: `apply-ion-patches.sh` now accepts linked Git
worktrees, and `sat-agent.py` compares the realized netem delay, rate, loss and
queue limit with the desired values while normalizing tc units. Oracle-routing
offsets now accept millisecond precision and preserve fractional UTC output.
Watch/reconciliation threads can overlap and deserve race-focused tests; no
race was reproduced in this review.
The current UDP-address validator accepts IPv6 although generated endpoints
and demo support are IPv4-oriented. Keep that support boundary explicit.

### Review verification and limits

- Existing unittest suite: 85 passing tests; 128 canonical epoch files checked.
- Harmless read-only reproductions: remote shell grouping, shell failure
  masking, and fractional rate conversion. No remote worker was contacted.
- The prepare tests cover node-name validation, rollback dispatch and failed
  admin sequences, but not live daemon readiness. Image loading, CA-only client
  setup, absolute-mode rejection and managed staging have regression coverage.
- The test named `test_udp_outduct_payload_stays_below_overlay_mtu` asserts the
  generated `udpclo 1200` line, not measured serialized packet size.
- C patch hunks follow the adjacent ION C style. Reading them and passing the
  Python suite do not prove C binary compatibility or live queue correctness.
- No runtime code changed during this review. Original source changes and
  previous comment-only style edits remain intact. No live campaign or image
  deployment was performed as part of the review.

## Complete tracked change list

Reproduce with `git diff --name-status 47f12a2 0253f0b`.
A = added, M = modified, D = deleted. This inventory does not duplicate source files.

| Status | File |
| --- | --- |
| A | [.gitignore](../.gitignore) |
| M | [README.md](../README.md) |
| M | [control/nsb-deploy.py](../control/nsb-deploy.py) |
| A | [control/nsb-ion-prepare.py](../control/nsb-ion-prepare.py) |
| M | [control/nsb-node-restart.py](../control/nsb-node-restart.py) |
| M | [control/nsb-run.py](../control/nsb-run.py) |
| M | [docs/control-commands.md](../docs/control-commands.md) |
| M | [docs/images/netsatbench.drawio](../docs/images/netsatbench.drawio) |
| A | [docs/ion-dtn-integration-guide.md](../docs/ion-dtn-integration-guide.md) |
| M | [docs/routing-interface.md](../docs/routing-interface.md) |
| M | [docs/starperf-integration.md](../docs/starperf-integration.md) |
| A | [examples/10nodes-ion-dtn/README.md](../examples/10nodes-ion-dtn/README.md) |
| A | [examples/10nodes-ion-dtn/epochs/00-all-open/NetSatBench-epoch0.json](../examples/10nodes-ion-dtn/epochs/00-all-open/NetSatBench-epoch0.json) |
| A | [examples/10nodes-ion-dtn/epochs/00-all-open/README.md](../examples/10nodes-ion-dtn/epochs/00-all-open/README.md) |
| A | [examples/10nodes-ion-dtn/epochs/01-store-carry-forward/NetSatBench-epoch0.json](../examples/10nodes-ion-dtn/epochs/01-store-carry-forward/NetSatBench-epoch0.json) |
| A | [examples/10nodes-ion-dtn/epochs/01-store-carry-forward/NetSatBench-epoch1.json](../examples/10nodes-ion-dtn/epochs/01-store-carry-forward/NetSatBench-epoch1.json) |
| A | [examples/10nodes-ion-dtn/epochs/01-store-carry-forward/NetSatBench-epoch10.json](../examples/10nodes-ion-dtn/epochs/01-store-carry-forward/NetSatBench-epoch10.json) |
| A | [examples/10nodes-ion-dtn/epochs/01-store-carry-forward/NetSatBench-epoch11.json](../examples/10nodes-ion-dtn/epochs/01-store-carry-forward/NetSatBench-epoch11.json) |
| A | [examples/10nodes-ion-dtn/epochs/01-store-carry-forward/NetSatBench-epoch12.json](../examples/10nodes-ion-dtn/epochs/01-store-carry-forward/NetSatBench-epoch12.json) |
| A | [examples/10nodes-ion-dtn/epochs/01-store-carry-forward/NetSatBench-epoch13.json](../examples/10nodes-ion-dtn/epochs/01-store-carry-forward/NetSatBench-epoch13.json) |
| A | [examples/10nodes-ion-dtn/epochs/01-store-carry-forward/NetSatBench-epoch14.json](../examples/10nodes-ion-dtn/epochs/01-store-carry-forward/NetSatBench-epoch14.json) |
| A | [examples/10nodes-ion-dtn/epochs/01-store-carry-forward/NetSatBench-epoch15.json](../examples/10nodes-ion-dtn/epochs/01-store-carry-forward/NetSatBench-epoch15.json) |
| A | [examples/10nodes-ion-dtn/epochs/01-store-carry-forward/NetSatBench-epoch16.json](../examples/10nodes-ion-dtn/epochs/01-store-carry-forward/NetSatBench-epoch16.json) |
| A | [examples/10nodes-ion-dtn/epochs/01-store-carry-forward/NetSatBench-epoch2.json](../examples/10nodes-ion-dtn/epochs/01-store-carry-forward/NetSatBench-epoch2.json) |
| A | [examples/10nodes-ion-dtn/epochs/01-store-carry-forward/NetSatBench-epoch3.json](../examples/10nodes-ion-dtn/epochs/01-store-carry-forward/NetSatBench-epoch3.json) |
| A | [examples/10nodes-ion-dtn/epochs/01-store-carry-forward/NetSatBench-epoch4.json](../examples/10nodes-ion-dtn/epochs/01-store-carry-forward/NetSatBench-epoch4.json) |
| A | [examples/10nodes-ion-dtn/epochs/01-store-carry-forward/NetSatBench-epoch5.json](../examples/10nodes-ion-dtn/epochs/01-store-carry-forward/NetSatBench-epoch5.json) |
| A | [examples/10nodes-ion-dtn/epochs/01-store-carry-forward/NetSatBench-epoch6.json](../examples/10nodes-ion-dtn/epochs/01-store-carry-forward/NetSatBench-epoch6.json) |
| A | [examples/10nodes-ion-dtn/epochs/01-store-carry-forward/NetSatBench-epoch7.json](../examples/10nodes-ion-dtn/epochs/01-store-carry-forward/NetSatBench-epoch7.json) |
| A | [examples/10nodes-ion-dtn/epochs/01-store-carry-forward/NetSatBench-epoch8.json](../examples/10nodes-ion-dtn/epochs/01-store-carry-forward/NetSatBench-epoch8.json) |
| A | [examples/10nodes-ion-dtn/epochs/01-store-carry-forward/NetSatBench-epoch9.json](../examples/10nodes-ion-dtn/epochs/01-store-carry-forward/NetSatBench-epoch9.json) |
| A | [examples/10nodes-ion-dtn/epochs/01-store-carry-forward/README.md](../examples/10nodes-ion-dtn/epochs/01-store-carry-forward/README.md) |
| A | [examples/10nodes-ion-dtn/epochs/02-cloudy-ground/NetSatBench-epoch0.json](../examples/10nodes-ion-dtn/epochs/02-cloudy-ground/NetSatBench-epoch0.json) |
| A | [examples/10nodes-ion-dtn/epochs/02-cloudy-ground/NetSatBench-epoch1.json](../examples/10nodes-ion-dtn/epochs/02-cloudy-ground/NetSatBench-epoch1.json) |
| A | [examples/10nodes-ion-dtn/epochs/02-cloudy-ground/NetSatBench-epoch10.json](../examples/10nodes-ion-dtn/epochs/02-cloudy-ground/NetSatBench-epoch10.json) |
| A | [examples/10nodes-ion-dtn/epochs/02-cloudy-ground/NetSatBench-epoch11.json](../examples/10nodes-ion-dtn/epochs/02-cloudy-ground/NetSatBench-epoch11.json) |
| A | [examples/10nodes-ion-dtn/epochs/02-cloudy-ground/NetSatBench-epoch12.json](../examples/10nodes-ion-dtn/epochs/02-cloudy-ground/NetSatBench-epoch12.json) |
| A | [examples/10nodes-ion-dtn/epochs/02-cloudy-ground/NetSatBench-epoch13.json](../examples/10nodes-ion-dtn/epochs/02-cloudy-ground/NetSatBench-epoch13.json) |
| A | [examples/10nodes-ion-dtn/epochs/02-cloudy-ground/NetSatBench-epoch14.json](../examples/10nodes-ion-dtn/epochs/02-cloudy-ground/NetSatBench-epoch14.json) |
| A | [examples/10nodes-ion-dtn/epochs/02-cloudy-ground/NetSatBench-epoch15.json](../examples/10nodes-ion-dtn/epochs/02-cloudy-ground/NetSatBench-epoch15.json) |
| A | [examples/10nodes-ion-dtn/epochs/02-cloudy-ground/NetSatBench-epoch16.json](../examples/10nodes-ion-dtn/epochs/02-cloudy-ground/NetSatBench-epoch16.json) |
| A | [examples/10nodes-ion-dtn/epochs/02-cloudy-ground/NetSatBench-epoch2.json](../examples/10nodes-ion-dtn/epochs/02-cloudy-ground/NetSatBench-epoch2.json) |
| A | [examples/10nodes-ion-dtn/epochs/02-cloudy-ground/NetSatBench-epoch3.json](../examples/10nodes-ion-dtn/epochs/02-cloudy-ground/NetSatBench-epoch3.json) |
| A | [examples/10nodes-ion-dtn/epochs/02-cloudy-ground/NetSatBench-epoch4.json](../examples/10nodes-ion-dtn/epochs/02-cloudy-ground/NetSatBench-epoch4.json) |
| A | [examples/10nodes-ion-dtn/epochs/02-cloudy-ground/NetSatBench-epoch5.json](../examples/10nodes-ion-dtn/epochs/02-cloudy-ground/NetSatBench-epoch5.json) |
| A | [examples/10nodes-ion-dtn/epochs/02-cloudy-ground/NetSatBench-epoch6.json](../examples/10nodes-ion-dtn/epochs/02-cloudy-ground/NetSatBench-epoch6.json) |
| A | [examples/10nodes-ion-dtn/epochs/02-cloudy-ground/NetSatBench-epoch7.json](../examples/10nodes-ion-dtn/epochs/02-cloudy-ground/NetSatBench-epoch7.json) |
| A | [examples/10nodes-ion-dtn/epochs/02-cloudy-ground/NetSatBench-epoch8.json](../examples/10nodes-ion-dtn/epochs/02-cloudy-ground/NetSatBench-epoch8.json) |
| A | [examples/10nodes-ion-dtn/epochs/02-cloudy-ground/NetSatBench-epoch9.json](../examples/10nodes-ion-dtn/epochs/02-cloudy-ground/NetSatBench-epoch9.json) |
| A | [examples/10nodes-ion-dtn/epochs/02-cloudy-ground/README.md](../examples/10nodes-ion-dtn/epochs/02-cloudy-ground/README.md) |
| A | [examples/10nodes-ion-dtn/epochs/03-alternate-path/NetSatBench-epoch0.json](../examples/10nodes-ion-dtn/epochs/03-alternate-path/NetSatBench-epoch0.json) |
| A | [examples/10nodes-ion-dtn/epochs/03-alternate-path/NetSatBench-epoch1.json](../examples/10nodes-ion-dtn/epochs/03-alternate-path/NetSatBench-epoch1.json) |
| A | [examples/10nodes-ion-dtn/epochs/03-alternate-path/NetSatBench-epoch2.json](../examples/10nodes-ion-dtn/epochs/03-alternate-path/NetSatBench-epoch2.json) |
| A | [examples/10nodes-ion-dtn/epochs/03-alternate-path/NetSatBench-epoch3.json](../examples/10nodes-ion-dtn/epochs/03-alternate-path/NetSatBench-epoch3.json) |
| A | [examples/10nodes-ion-dtn/epochs/03-alternate-path/NetSatBench-epoch4.json](../examples/10nodes-ion-dtn/epochs/03-alternate-path/NetSatBench-epoch4.json) |
| A | [examples/10nodes-ion-dtn/epochs/03-alternate-path/NetSatBench-epoch5.json](../examples/10nodes-ion-dtn/epochs/03-alternate-path/NetSatBench-epoch5.json) |
| A | [examples/10nodes-ion-dtn/epochs/03-alternate-path/NetSatBench-epoch6.json](../examples/10nodes-ion-dtn/epochs/03-alternate-path/NetSatBench-epoch6.json) |
| A | [examples/10nodes-ion-dtn/epochs/03-alternate-path/NetSatBench-epoch7.json](../examples/10nodes-ion-dtn/epochs/03-alternate-path/NetSatBench-epoch7.json) |
| A | [examples/10nodes-ion-dtn/epochs/03-alternate-path/NetSatBench-epoch8.json](../examples/10nodes-ion-dtn/epochs/03-alternate-path/NetSatBench-epoch8.json) |
| A | [examples/10nodes-ion-dtn/epochs/03-alternate-path/NetSatBench-epoch9.json](../examples/10nodes-ion-dtn/epochs/03-alternate-path/NetSatBench-epoch9.json) |
| A | [examples/10nodes-ion-dtn/epochs/03-alternate-path/README.md](../examples/10nodes-ion-dtn/epochs/03-alternate-path/README.md) |
| A | [examples/10nodes-ion-dtn/epochs/04-multi-bundle-queue/NetSatBench-epoch0.json](../examples/10nodes-ion-dtn/epochs/04-multi-bundle-queue/NetSatBench-epoch0.json) |
| A | [examples/10nodes-ion-dtn/epochs/04-multi-bundle-queue/NetSatBench-epoch1.json](../examples/10nodes-ion-dtn/epochs/04-multi-bundle-queue/NetSatBench-epoch1.json) |
| A | [examples/10nodes-ion-dtn/epochs/04-multi-bundle-queue/NetSatBench-epoch2.json](../examples/10nodes-ion-dtn/epochs/04-multi-bundle-queue/NetSatBench-epoch2.json) |
| A | [examples/10nodes-ion-dtn/epochs/04-multi-bundle-queue/NetSatBench-epoch3.json](../examples/10nodes-ion-dtn/epochs/04-multi-bundle-queue/NetSatBench-epoch3.json) |
| A | [examples/10nodes-ion-dtn/epochs/04-multi-bundle-queue/NetSatBench-epoch4.json](../examples/10nodes-ion-dtn/epochs/04-multi-bundle-queue/NetSatBench-epoch4.json) |
| A | [examples/10nodes-ion-dtn/epochs/04-multi-bundle-queue/NetSatBench-epoch5.json](../examples/10nodes-ion-dtn/epochs/04-multi-bundle-queue/NetSatBench-epoch5.json) |
| A | [examples/10nodes-ion-dtn/epochs/04-multi-bundle-queue/NetSatBench-epoch6.json](../examples/10nodes-ion-dtn/epochs/04-multi-bundle-queue/NetSatBench-epoch6.json) |
| A | [examples/10nodes-ion-dtn/epochs/04-multi-bundle-queue/README.md](../examples/10nodes-ion-dtn/epochs/04-multi-bundle-queue/README.md) |
| A | [examples/10nodes-ion-dtn/epochs/05-short-contact-capacity/NetSatBench-epoch0.json](../examples/10nodes-ion-dtn/epochs/05-short-contact-capacity/NetSatBench-epoch0.json) |
| A | [examples/10nodes-ion-dtn/epochs/05-short-contact-capacity/NetSatBench-epoch1.json](../examples/10nodes-ion-dtn/epochs/05-short-contact-capacity/NetSatBench-epoch1.json) |
| A | [examples/10nodes-ion-dtn/epochs/05-short-contact-capacity/NetSatBench-epoch2.json](../examples/10nodes-ion-dtn/epochs/05-short-contact-capacity/NetSatBench-epoch2.json) |
| A | [examples/10nodes-ion-dtn/epochs/05-short-contact-capacity/NetSatBench-epoch3.json](../examples/10nodes-ion-dtn/epochs/05-short-contact-capacity/NetSatBench-epoch3.json) |
| A | [examples/10nodes-ion-dtn/epochs/05-short-contact-capacity/NetSatBench-epoch4.json](../examples/10nodes-ion-dtn/epochs/05-short-contact-capacity/NetSatBench-epoch4.json) |
| A | [examples/10nodes-ion-dtn/epochs/05-short-contact-capacity/README.md](../examples/10nodes-ion-dtn/epochs/05-short-contact-capacity/README.md) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch0.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch0.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch1.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch1.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch10.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch10.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch11.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch11.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch12.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch12.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch13.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch13.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch14.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch14.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch15.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch15.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch16.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch16.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch17.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch17.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch18.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch18.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch19.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch19.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch2.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch2.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch20.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch20.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch21.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch21.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch22.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch22.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch23.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch23.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch24.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch24.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch25.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch25.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch26.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch26.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch27.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch27.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch28.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch28.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch29.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch29.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch3.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch3.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch30.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch30.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch31.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch31.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch32.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch32.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch33.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch33.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch34.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch34.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch35.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch35.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch36.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch36.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch37.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch37.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch38.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch38.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch39.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch39.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch4.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch4.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch40.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch40.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch41.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch41.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch42.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch42.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch43.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch43.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch44.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch44.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch45.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch45.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch46.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch46.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch47.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch47.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch48.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch48.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch49.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch49.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch5.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch5.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch50.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch50.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch51.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch51.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch52.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch52.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch53.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch53.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch54.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch54.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch55.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch55.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch56.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch56.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch57.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch57.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch58.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch58.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch59.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch59.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch6.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch6.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch60.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch60.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch7.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch7.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch8.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch8.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch9.json](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/NetSatBench-epoch9.json) |
| A | [examples/10nodes-ion-dtn/epochs/06-handover-overlap/README.md](../examples/10nodes-ion-dtn/epochs/06-handover-overlap/README.md) |
| A | [examples/10nodes-ion-dtn/epochs/README.md](../examples/10nodes-ion-dtn/epochs/README.md) |
| A | [examples/10nodes-ion-dtn/ion/.gitignore](../examples/10nodes-ion-dtn/ion/.gitignore) |
| A | [examples/10nodes-ion-dtn/ion/IMPLEMENTATION.md](../examples/10nodes-ion-dtn/ion/IMPLEMENTATION.md) |
| A | [examples/10nodes-ion-dtn/ion/README.md](../examples/10nodes-ion-dtn/ion/README.md) |
| A | [examples/10nodes-ion-dtn/ion/ion-node-registry.json](../examples/10nodes-ion-dtn/ion/ion-node-registry.json) |
| A | [examples/10nodes-ion-dtn/sat-config.json](../examples/10nodes-ion-dtn/sat-config.json) |
| A | [examples/10nodes-ion-dtn/worker-config.example.json](../examples/10nodes-ion-dtn/worker-config.example.json) |
| M | [examples/10nodes/README.md](../examples/10nodes/README.md) |
| M | [nsb.py](../nsb.py) |
| A | [sat-container/Dockerfile.ion](../sat-container/Dockerfile.ion) |
| A | [sat-container/apply-ion-patches.sh](../sat-container/apply-ion-patches.sh) |
| A | [sat-container/patches/ion-bpclm-fragment-ordinal-tail.patch](../sat-container/patches/ion-bpclm-fragment-ordinal-tail.patch) |
| A | [sat-container/patches/ion-bpsendtest-repeat-file-reference.patch](../sat-container/patches/ion-bpsendtest-repeat-file-reference.patch) |
| A | [sat-container/patches/ion-udpcla-receive-buffer.patch](../sat-container/patches/ion-udpcla-receive-buffer.patch) |
| M | [sat-container/sat-agent.py](../sat-container/sat-agent.py) |
| A | [sat-container/stage-ion-runtime.sh](../sat-container/stage-ion-runtime.sh) |
| D | `test/handover/grd/__pycache__/connection_agent_grd.cpython-310.pyc` |
| D | `test/handover/grd/__pycache__/connection_agent_grd_fq.cpython-310.pyc` |
| D | `test/handover/usr/__pycache__/connection_agent_usr.cpython-310.pyc` |
| D | `test/handover/usr/__pycache__/connection_agent_usr_fq.cpython-310.pyc` |
| A | [test/test_ion_demo_scenarios.py](../test/test_ion_demo_scenarios.py) |
| A | [test/test_nsb_ion_generate_configs.py](../test/test_nsb_ion_generate_configs.py) |
| A | [test/test_nsb_ion_prepare.py](../test/test_nsb_ion_prepare.py) |
| A | [test/test_sat_agent_reconcile.py](../test/test_sat_agent_reconcile.py) |
| M | [utils/misc/force-netem-params.py](../utils/misc/force-netem-params.py) |
| A | [utils/nsb-ion-generate-configs.py](../utils/nsb-ion-generate-configs.py) |
| A | [utils/nsb-ion-generate-demo-scenarios.py](../utils/nsb-ion-generate-demo-scenarios.py) |
| A | [utils/nsb-load-image.py](../utils/nsb-load-image.py) |
