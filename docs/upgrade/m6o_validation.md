# M6O secure local Terraform backends and state custody preparation

Date 2026-11-10. Baseline `main` at `ccd2289` (M6N committed; the user reports that CI passed and the drill artefacts were deleted).

**Scope: implementation and offline verification only.** M6O did none of the following:
- create the custody tree (`~/.secure` is still absent);
- create any Terraform state;
- run `init` against a real root, or any plan, apply or destroy;
- call gcloud or GCP, or change IAM or billing;
- access OneDrive;
- commit or push.

Runbook: [terraform_state_custody.md](../terraform_state_custody.md#backend-wiring-m6o-implemented).

## Inspected before editing

| Item | Finding |
| --- | --- |
| Roots | `infrastructure/terraform` (pilot-dev) and `…/bootstrap` (M6K): no backend block; M6L test asserted its absence. Repository `.terraform/` dirs hold provider caches only (no backend cache, no state) |
| M6L | `state_custody.py check --root` preflight (TF_DATA_DIR, backend cache, TF_WORKSPACE, CHECKPOINT_DISABLE, stray state). The fail-safe matrix had **two fail-open cases**: `init` without `-backend-config` (state defaults into the repository) and `TF_WORKSPACE` without `workspace_dir` (creates `terraform.tfstate.d/` in the repository) |
| M6M/M6N | `state_backup.py` expects `state/<root>/terraform.tfstate`, plus `backups/` and `recovery/` in custody |
| Host | `~/.secure` absent; FileVault On (M6L/M6N); repository at `~/Developer/…`, not a synchronised location |

## Design decision: fail-closed sentinel backend

A probe in a disposable sandbox (no providers or resources; only `init`, `state list` and `workspace`) tested an explicit local backend set to an impossible `/dev/null/…` sentinel:
- **any `init` without the custody `-backend-config` failed closed**, with no backend cache and no state written;
- the custody `-backend-config` overrode both values cleanly;
- `init` created no state file.

This **closes both M6L fail-open cases at the Terraform layer**, rather than relying only on the preflight. M6L's earlier assumption that a sentinel would "fail only after cloud changes" was untested reasoning. Measured, the failure happens at `init`, before any operation can run. The sentinel contains no username (`/dev/null/ebank-custody-init-required/…`).

Defence in depth, three layers:
1. **Sentinel backend** in both roots.
2. **Wrapper** (`terraform_custody.py run`). It is the only supplier of per-root custody `path` and `workspace_dir`, with `TF_DATA_DIR`, umask 077, `CHECKPOINT_DISABLE=1`, a cleaned `TF_*` environment, `-lockfile=readonly`, a command and flag allow-list, and saved-plan-only `apply` confined to `plans/<root>/`.
3. **Fail-closed preflight:** location, symlinks, Git and iCloud, modes and ownership, tree contents, environment, root isolation, stray repository artefacts, backend block, FileVault, and the run-phase backend cache.

## Changed files

| File | Change |
| --- | --- |
| `infrastructure/terraform/main.tf`, `infrastructure/terraform/bootstrap/main.tf` | Explicit `backend "local"` with the sentinel `path` and `workspace_dir`, plus an explanatory comment. No other change; `fmt` and `validate` OK; lockfiles unchanged (`61cea4a1…`) |
| `scripts/terraform_custody.py` (new, stdlib) | `init-custody` (dry run by default; `--confirm` creates 0700 dirs and a 0600 Spotlight marker; idempotent; never loosens, repairs or replaces existing entries; refuses symlinks, foreign owners, Git work trees and forbidden locations; creates no state), `preflight --root --phase init\|run`, `run --root [--dry-run] -- <terraform args>`. Real `init-custody --confirm` and `run` refuse inside a Claude Code session |
| `scripts/state_custody.py` | `LAYOUT` gains `state/<root>/workspaces`, so `check` verifies them too |
| `tests/test_terraform_custody.py` (new) | 72 tests (below) |
| `tests/test_state_custody.py` | `test_backend_blocks_not_yet_implemented` replaced by `test_backend_blocks_use_fail_closed_sentinel` |
| `Makefile` | `gcp-offline-check` includes the new tests |
| `docs/terraform_state_custody.md` | Status; backend wiring and commands; command policy; M6L vs M6O fail-safe matrix; preflight list; updated approvals |
| `docs/terraform_state_backup_onedrive.md` | Gate-1 step now uses `init-custody` |
| `docs/index.md`, this file | Link and evidence |

## Tests (`tests/test_terraform_custody.py`, 72 passed)

- **Real roots:** exactly one backend block per root, `local`, the exact sentinel text, no username, and `/dev/null` is not a directory. Per-root custody paths are distinct, and the workspace directories are in the layout.
- **Initialisation:**
  - creates every directory at 0700 and the marker at 0600, with no other files; idempotent;
  - never loosens an existing 0755 directory, and refuses it instead;
  - refuses `~/.secure` symlinked into iCloud, Desktop or outside home, leaving the target untouched;
  - refuses a custody root inside a Git work tree (for example a dotfiles repository in `$HOME`), or inside the repository.
- **Adversarial preflight, 17 cases, all refused:**
  - loose directory or file mode;
  - one root's state directory symlinked to the other's;
  - an unexpected file in `state/`;
  - a missing directory, or a missing marker;
  - `TF_WORKSPACE`, `TF_CLI_ARGS_apply`, `TF_CLI_CONFIG_FILE`, or `TF_LOG` without a custody log path;
  - `TF_DATA_DIR` set to the other root or to the repository;
  - stray state, or a `terraform.tfstate.d`, in the repository;
  - the backend block removed;
  - the repository under Desktop (iCloud);
  - the tree missing.
- **Logging:** `TF_LOG` is allowed only into custody `logs/`. The run phase requires a correct backend cache.
- **Command policy:**
  - `init` injects the per-root custody backend config, `-lockfile=readonly` and `-input=false`;
  - `plan -out` and `apply` work only with saved plans in the root's own `plans/`; `-var-file` only from custody `inputs/`.
- **Refused invocations, 35 cases:** among them `destroy`, `import`, `state rm/push/mv`, `workspace`, `force-unlock`, `console`, `refresh` and `taint`; global `-chdir`; `-lock=false`, `--lock=FALSE` and `-lock=0`; `-state`, `-backend-config`, `-backend=false`, `--backend=0`, `-backend`, `-migrate-state`, `-upgrade` and a user-supplied `-lockfile`; `plan -destroy`; `apply -auto-approve` without a plan; a plan without `-out`, with a relative `-out`, or with `-out` in the other root's plans; `apply` of the other root's plan or a repository plan; a repository `-var-file`; the separate-value `-out x` / `-var-file x` forms; `show` of a repository plan; `state list -state=`.
- **CLI:** `init-custody --confirm` and `run` refuse inside Claude (exit 2) and create nothing in `HOME`. The dry-run `init-custody` only lists directories.
- **Sandbox Terraform** (executed, not skipped; Terraform 1.16.5): the **real roots' exact backend block** fails closed for plain `init`, path-only, workspace-dir-only, `-backend=false` (state commands stop) and `TF_WORKSPACE=dev`. No state or workspace artefact appears anywhere.
- **Wrapper end to end** (fake home and repository, both roots): `init` exits 0 with no post-problems; the cache is 0600 and holds only that root's custody paths; **no state file is created**; the run-phase preflight passes; `state list` creates nothing. Pointing the bootstrap cache at the pilot state makes the preflight refuse **both** roots.

**Findings fixed during testing:**
- The first draft accepted `init -backend=false`: the flag-name comparison missed it. `-backend` is now refused in any form.
- A `workspace_dir`-only init writes a backend cache before failing. It still fails closed at the sentinel state path, and the test now asserts fail-closed plus no state rather than one specific message.

## Validation

| Check | Result |
| --- | --- |
| Full `pytest` | **464 passed, 1 skipped** (the opt-in M6L `hdiutil` drill) |
| `make gcp-offline-check` | **220 passed, 1 skipped**; `terraform fmt` passed |
| `ruff check .` | Passed |
| Docs validator (`PYTHONPATH=src`) | Passed, 0 issues |
| `terraform validate` (both real roots, existing repository provider caches, no `init`) | Both valid; no backend cache or state created in the repository |
| Real-home `preflight --root bootstrap --phase init` (read-only) | **Exit 1, as intended:** "custody tree not initialised". No location, symlink, Git or iCloud problems. `run --dry-run -- init` refused for the same reason. `init-custody` dry run lists 18 directories |
| Hygiene | Nothing staged; staged-guard exit 0. Tracked and untracked files flagged: none. State, plan, lock or workspace artefacts under the repository (including ignored files): none. `~/.secure` absent. `git diff --check` clean. Sandbox probe directory removed |
| Egress | HashiCorp `checkpoint_cache` mtime unchanged (`CHECKPOINT_DISABLE=1` throughout); no registry download (sandbox configs have no providers); no GCP or OneDrive access |

## Risks and operator decisions (documented, not weakened)

1. **The wrapper is a guard, not a boundary.** Running `terraform` directly can still pass an arbitrary `-backend-config` path. The sentinel makes forgetting it fail closed; deliberately misdirecting it is caught only if the preflight or wrapper is used. *Operator rule:* use the wrapper only.
2. **Spotlight exclusion.** `.metadata_never_index` is honoured reliably at volume level; for a folder it is best-effort on current macOS. *Operator decision:* also add `~/.secure` under System Settings → Spotlight → Privacy.
3. **Time Machine.** No destination is configured today. If one is added, plaintext state in `~/.secure` would be copied to it. *Operator decision when that happens:* exclude `~/.secure`, or use an encrypted Time Machine destination only.
4. **Excluded commands.** `destroy`, `import`, `state rm/mv/push` and `force-unlock` are deliberately not available through the wrapper. Needing one later means a reviewed wrapper change plus a fresh verified backup, not a bypass.
5. **Provider download at gate-8 execution.** The real `run -- init` will contact `registry.terraform.io` to fetch the Google provider into custody `tfdata/`. It is pinned by the committed lockfile (`-lockfile=readonly`); there is no GCP call.
6. **Plaintext at rest only under FileVault.** Unchanged since M6L: any process running as the operator can read custody while the operator is logged in. A Claude Code deny rule for `~/.secure/**` is still recommended.
7. **Untested here:** creation of the real tree on this Mac, the real-provider `init`, and the M6M `create` and `verify-download` against real custody. All are operator steps after approval.
8. **Repository `.terraform/` provider caches** remain. They hold no backend cache, and an `init` there now fails closed. Removing them stays optional.

## Remaining approval gates

1. **Review and commit M6O** (operator).
2. **Gate 1, custody tree:** in Terminal.app, `python3 scripts/terraform_custody.py init-custody --confirm`, then `preflight --root bootstrap --phase init` and `preflight --root pilot-dev --phase init`, both reporting no problems. Plus the Spotlight Privacy decision.
3. **Gate 8 execution:** `run --root bootstrap -- init` and `run --root pilot-dev -- init` (provider download, no state), then `run --root <root> -- validate`.
4. Optional: relocate the `outputs/runs` M6* evidence into custody; add a Claude Code deny rule for `~/.secure/**`; install the pre-commit hook.
5. **M6K bootstrap gates:** saved plan in `plans/bootstrap/`, explicit approval and apply. The first real state follows, **immediately followed by** the first real encrypted backup and OneDrive download-verify for bootstrap.

**Stopped after M6O.** No bootstrap or M6P work was started.
