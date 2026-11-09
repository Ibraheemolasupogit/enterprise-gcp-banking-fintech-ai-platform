# M6L secure Terraform state design and recovery plan

Drafted 2026-10-31; **re-verified and completed 2026-11-09**. Starting revision `24447c7` (M6K committed by the user). The working tree was preserved: the earlier draft was reviewed and amended, not replaced. **Review and design only.** No Terraform state, custody directory, backup medium, hook installation, cloud call, IAM/billing/provider/authentication change, Terraform plan/apply/destroy, commit or push. The design is in [terraform_state_custody.md](../terraform_state_custody.md).

## Inspected facts (local, read-only)

| Item | Finding |
| --- | --- |
| Terraform roots | `infrastructure/terraform` (pilot) and `infrastructure/terraform/bootstrap`. Both use an implicit local backend; no `*.tfstate`, `*.tfplan` or real `*.tfvars` exist |
| FileVault | **On**: `fdesetup status`, and `diskutil` reports FileVault Yes on the internal APFS Data volume holding `~`. The sealed system volume shows "Encrypted: No", which is normal |
| iCloud | **`~/Desktop` is an iCloud Desktop & Documents file-provider domain.** The repository, its `.terraform/` caches and the private `outputs/runs/` M6H/M6I evidence are synchronised despite being git-ignored. `~` itself is not a file-provider domain |
| Shell umask | **022**: Terraform would create world-readable state |
| Time Machine | No destinations configured (no unencrypted copies today) |
| External media | None attached; backup destination unresolved |
| Tools | `hdiutil` (native AES-256 images), `shasum`; `age`/`gpg` absent (none installed) |
| Git hooks | None active; no `core.hooksPath` |
| Terraform CLI config | No `~/.terraformrc` and no `~/.terraform.d/credentials.tfrc.json`. `~/.terraform.d` holds only checkpoint files |
| Repository `.terraform/` | Provider caches, `modules.json` and `plugin_path` only. **No backend cache, `terraform.tfstate` or lock file.** No `backend` block in either root |

All rows were re-checked on 2026-11-09 and are unchanged (FileVault On; umask 022; `~/Desktop` is a file-provider domain; no Time Machine destination; `~/.secure` absent; no ADC file).

## Changes

| File | Change |
| --- | --- |
| `.gitignore` | Added `crash.*.log`, `*.tfplan.json`, override files, `.terraformrc`/`terraform.rc`, `*.sparsebundle/`, `*.sparseimage`, `*.dmg`, `*.age`, `*.gpg`, `application_default_credentials.json` and SA key JSON. No tracked file collides |
| `scripts/state_custody.py` (new) | `check` (location policy, FileVault, modes, ownership, symlinks, umask; read-only), `staged` (pre-commit guard by name and content; never prints content), `drill` (disposable encrypted restore rehearsal) |
| `scripts/hooks/pre-commit` (new, **not installed**) | Calls `state_custody.py staged` |
| `tests/test_state_custody.py` (new) | 33 tests: path policy, modes, guard name/content rules and false positives, a whole-repository tracked-file scan, `git check-ignore` coverage, example/lockfile trackability, snapshot round-trip and tamper detection, hook presence, and the opt-in encrypted drill |
| `Makefile` | `gcp-offline-check` includes the custody tests |
| 2026-11-09 amendments | `.gitignore` adds `*.p12`, which the guard already blocked by name but Git did not ignore. `LAYOUT` adds `tfdata/bootstrap` and `tfdata/pilot-dev`, so `check` also verifies the `TF_DATA_DIR` modes. Tests: two new ignore probes (`key.p12`, `terraform.tfstate.d/`) and a `tfdata` layout assertion. Runbook: fail-closed `TF_DATA_DIR` behaviour, a default-workspace-only rule and `CHECKPOINT_DISABLE=1` |
| `docs/terraform_state_custody.md`, `docs/index.md` | Design runbook and link |

Terraform roots, provider and lockfiles are **unchanged**. The backend block is deferred to the state gate.

## Results

| Check | Result |
| --- | --- |
| `state_custody.py check`, default shell | **Exit 1**, as intended: umask 022. Policy violations none; FileVault true; custody root absent |
| `state_custody.py check` under `umask 077` | **Exit 0** |
| Restore drill (`drill`, scratch workdir; re-run 2026-11-09) | **Exit 0**. Fixture and restored modes 0600, workdir 0700. AES-256 sparse bundle created and mounted with an in-memory random passphrase. **Wrong passphrase rejected.** Manifest hash, lineage and serial verified inside the backup. Restored hash equals the original. **Tamper detected.** `terraform show -json` parsed the restored fixture. Work directory removed; no `M6LDRILL` image left attached |
| Opt-in drill via pytest (`M6L_RUN_DRILL=1`) | 1 passed |
| `state_custody.py staged` | Exit 0 (nothing staged) |
| Tracked-file scan | 0 files flagged (no state, plan, input, key or token in Git) |
| Full `pytest` (2026-11-09) | **345 passed, 1 skipped** (the opt-in drill) |
| `make gcp-offline-check` (2026-11-09) | **101 passed, 1 skipped**; fmt passed |
| `ruff check .` | Passed |
| `terraform fmt -check -recursive`; `validate` for both roots | Passed; both valid. Roots and provider unchanged |
| 43 baseline sample artifacts | **43/43 SHA256 match** |
| Provider lockfiles | Both `61cea4a1…0fef`, unchanged |
| Post-run hygiene | `~/.secure` absent; no state/plan/tfvars; no drill image attached; `core.hooksPath` unset; `git diff --check` passed |

## Unresolved risks

1. **iCloud exposure.** The repository and its private evidence on `~/Desktop` are uploaded to iCloud. Only redacted metadata was stored there by design, but it is outside the custody model. Options: move the repository off Desktop, relocate `outputs/runs` evidence into custody, or accept. User decision.
2. **No backup medium**, so the single-device failure domain remains until approval 2.
3. **FileVault protects only at rest.** While the operator is logged in, any process running as the operator (including development tools and AI agents) can read `~/.secure`. Consider a Claude Code deny rule for `~/.secure/**`, as a separate settings change.
4. **No native state encryption** in Terraform's local backend beyond FileVault. The state is plaintext to the logged-in user.
5. **Locking is advisory and same-host only.** It is controlled by the single-operator rule.
6. **Passphrase quality and escrow, and escrow of the Mac's FileVault recovery key,** are operator responsibilities and are unverified.
7. **The hook is optional and bypassable** (`--no-verify`). The tracked-file test is the backstop.
8. The pre-existing personal email in `docs/upgrade/m6b_validation.md` (noted in M6K) is still unredacted.
9. **Workspaces escape custody.** `terraform workspace new` under the local backend writes `terraform.tfstate.d/` into the repository working directory, which is iCloud-synchronised. The runbook mitigates this by prohibiting workspaces; it is git-ignored and tested.
10. **Forgotten `TF_DATA_DIR`.** This fails closed only after the explicit backend block exists (approval 8). Until then, any `init`/`apply` in the repository would use the implicit local backend in the repository directory. No apply is approved, so this is a documented ordering constraint: approval 8 must precede any state-creating command.
11. **Outbound checkpoint call during review (2026-11-09).** A `terraform version` probe sent Terraform's default version check to HashiCorp (`checkpoint_cache` mtime matched). It is not a GCP call and makes no change, but it is network egress. All later commands set `CHECKPOINT_DISABLE=1`, and the runbook now requires it.

## M6L closeout (2026-11-09)

The user accepted the M6L review in principle and asked for an offline closeout. This section is the closeout record. Nothing was moved, created, deployed, committed or pushed, and no backend block was added.

### Design choices approved by the user

| Decision | Value |
| --- | --- |
| State root | `~/.secure/ebank-pilot-dev-261031-k7m4/` on the FileVault-protected internal Data volume. FileVault was re-verified On |
| Permissions | `umask 077`; folders `0700`; sensitive files `0600` |
| Custodian | The operator, as sole custodian |
| Backup retention (preliminary) | The latest 3 verified backups, for up to 30 days. **Open point:** the age limit must not delete the only remaining verified backup; the proposed reading is to keep the newest one regardless of age |
| Independent encrypted backup destination | **UNRESOLVED.** None exists and none is assumed. No real state may be created until one is approved and drilled |

Approving these choices does not approve creating the custody tree (gate 1).

### Repository relocation out of iCloud: proposal, not executed

**Findings (read-only):**
- The repository is 171 MB, about 1,500 files; `.git` is 4 MB.
- There is one worktree, no stashes and no in-repository `.claude/`.
- **0 iCloud-evicted ("dataless") files**, so a full local copy is possible.
- Terraform caches use relative paths (`plugin_path` is `../.terraform/providers`; `modules.json` is relative).
- No editable Python install and no tracked file references the Desktop path. Git configuration is global only.
- Three cache directories carry `com.apple.fileprovider` extended attributes. Plain `rsync -a` (macOS openrsync, no `-X`) does not copy extended attributes.
- Claude project context lives outside the repository at `~/.claude/projects/<path-key>/`: 2 session transcripts, plus an empty `memory/`. The key is the absolute path with `/` and `_` replaced by `-`. A trust entry in `~/.claude.json` is keyed by the old path.

**Target:** `~/Developer/enterprise-gcp-banking-fintech-ai-platform`. It is on the internal FileVault Data volume and is not an iCloud Desktop & Documents location. `~/Developer` does not exist yet.

**Procedure.** Copy, verify, then retire the old copy. Run it in a plain Terminal, with no Claude Code session, editor or terminal open in the repository.

```sh
set -eu; umask 077
SRC="$HOME/Desktop/GitHub_repository/enterprise-gcp-banking-fintech-ai-platform"
DST="$HOME/Developer/enterprise-gcp-banking-fintech-ai-platform"
W="$(mktemp -d)"                                   # private scratch for manifests

# 1. Preconditions
test ! -e "$DST"
test "$(find "$SRC" -flags +dataless | wc -l | tr -d ' ')" = 0   # nothing evicted to iCloud
git -C "$SRC" fsck --full --no-progress
git -C "$SRC" rev-parse HEAD > "$W/head"; git -C "$SRC" status --porcelain=v1 > "$W/status"
(cd "$SRC" && find . -type f -print0 | sort -z | xargs -0 shasum -a 256) > "$W/manifest.sha256"

# 2. Copy: preserves Git history, the uncommitted M6L changes and all ignored local files
mkdir -p -m 700 "$HOME/Developer"
rsync -a "$SRC/" "$DST/"                           # no -X: iCloud file-provider xattrs are dropped

# 3. Verify the copy is complete and identical
(cd "$DST" && shasum -a 256 -c --quiet "$W/manifest.sha256")
git -C "$DST" fsck --full --no-progress
test "$(git -C "$DST" rev-parse HEAD)" = "$(cat "$W/head")"
git -C "$DST" status --porcelain=v1 | diff - "$W/status"
test "$(xattr -lr "$DST" | grep -ci fileprovider)" = 0
(cd "$DST" && CHECKPOINT_DISABLE=1 python3 -m pytest -q && CHECKPOINT_DISABLE=1 make gcp-offline-check)

# 4. Claude project context: copy, never move; the old key stays as rollback
OLDKEY="$(printf %s "$SRC" | tr '/_' '--')"; NEWKEY="$(printf %s "$DST" | tr '/_' '--')"
test -d "$HOME/.claude/projects/$OLDKEY" && test ! -e "$HOME/.claude/projects/$NEWKEY"
cp -Rp "$HOME/.claude/projects/$OLDKEY" "$HOME/.claude/projects/$NEWKEY"
# Then: `cd "$DST" && claude`. Accept the folder-trust prompt once (do not hand-edit
# ~/.claude.json), and confirm earlier sessions appear in `claude --resume`.
```

**Rollback:** delete `$DST` and the new project key, then keep working in `$SRC`. Nothing in the source is changed by steps 1–4.

**Retiring the Desktop copy is a separate, later approval.** Keep it until at least one verified working session in `$DST`. Removing it also removes it from iCloud Drive. iCloud's "Recently Deleted" may keep it for up to 30 days, and copies already uploaded cannot be recalled by local action. M6L treats that residual exposure as accepted history, not as remediated.

### `outputs/runs` sensitive-evidence review: proposal, nothing deleted

32 run directories exist; all are git-ignored and untracked. A content scan recorded counts only and printed no values. **No OAuth token, private key, refresh token, access-token field or bearer token was found anywhere.**

| Directories | Content classes found | Classification |
| --- | --- | --- |
| `m6c-verification` | Billing-account-format IDs (8), personal email (4), IAM policy bindings (2), service-account emails (11) | **Sensitive deployment evidence** |
| `m6e-eligibility` | Billing-account-format IDs (7), project ID | **Sensitive** |
| `m6b-preflight`, `m6h-verification`, `m6i-api-bootstrap` | Personal email; project ID (up to 117); IAM policy bindings (`m6i`) | **Sensitive deployment evidence** |
| `m6a-provider-validation`, `m6d-design`, `m6g-evidence` | Project ID; service-account emails (`m6d`) | Deployment evidence, lower sensitivity; keep together with the above |
| `m1-*` … `m5-*` (24 directories) | Synthetic data; macOS home path (username) only | Low; may stay in the repository |

**Proposal.** Copy the eight `m6*` directories into custody, verify them, and **keep the originals** until a separate retention decision. The code (`DEFAULT_RUNS_ROOT`, used by the M1–M4 pipelines) keeps writing new runs to `outputs/runs`, and eight M6 docs cite the original paths. This requires gate 1 (custody root).

```sh
set -eu; umask 077
E="$HOME/.secure/ebank-pilot-dev-261031-k7m4/evidence/runs"; mkdir -p -m 700 "$E"
cd "$DST/outputs/runs"      # after relocation; otherwise run from $SRC/outputs/runs
for d in m6a-provider-validation m6b-preflight m6c-verification m6d-design \
         m6e-eligibility m6g-evidence m6h-verification m6i-api-bootstrap; do
  rsync -a "$d" "$E/"
done
find "$E" -type d -exec chmod 700 {} +; find "$E" -type f -exec chmod 600 {} +
(find m6* -type f -print0 | sort -z | xargs -0 shasum -a 256) > "$E/MANIFEST.sha256"
(cd "$E" && shasum -a 256 -c --quiet MANIFEST.sha256)
chmod 600 "$E/MANIFEST.sha256"
```

Optional follow-ups, each needing its own approval:
- tighten the originals to `go-rwx`;
- later, replace them with a pointer file once the docs cite the custody location;
- redact the personal email already committed in `docs/upgrade/m6b_validation.md` (an existing finding).

### State-backend fail-safe review (no backend added)

These are disposable sandbox probes (Terraform 1.16.5, `CHECKPOINT_DISABLE=1`, no providers or resources; only `init`, `state list` and `workspace`). They are now permanent tests.

| Case | Result |
| --- | --- |
| Explicit `backend "local" {}`, `init` omitted (repository `.terraform/` or a fresh `TF_DATA_DIR`) | **Fails closed**: "Backend initialization required" |
| Correct custody init, then `TF_DATA_DIR` forgotten | **Fails closed** |
| `init` without `-backend-config` | **Fails open**: the cached `path` is unset, so state defaults to `./terraform.tfstate` in the repository. `check --root` catches it |
| `TF_WORKSPACE=dev`, `workspace_dir` unset | **Fails open**: a read-only `state list` created `terraform.tfstate.d/dev/` in the working directory. Fixed by passing `workspace_dir` into custody; `check --root` refuses it |
| Correct init | Creates no state file |
| Current real roots (implicit backend; pilot `.terraform/` initialised) | A future apply would write state inside the repository. **Mitigated only by ordering**: gate 8 before or with gate 1, and no apply approved |

A Terraform-native setting cannot close the two fail-open cases. `-backend-config` overrides a hard-coded path, and an unwritable sentinel path fails only after cloud changes have been made. The safeguard is therefore the mandatory `scripts/state_custody.py check --root <root>` preflight, added in this closeout. It is read-only and refuses when:
- `TF_DATA_DIR` is not in custody;
- the backend `path`/`workspace_dir` are missing or wrong;
- `TF_WORKSPACE` is non-default;
- `CHECKPOINT_DISABLE` is not 1;
- any state, plan, lock or workspace artefact exists anywhere under the repository.

Against the real roots today it correctly refuses both, because no custody backend exists.

### Closeout changes

| File | Change |
| --- | --- |
| `scripts/state_custody.py` | `ROOTS`; `expected_backend`, `backend_cache_problems`, `environment_problems`, `stray_state_paths`; `check --root`. `check` now also fails on stray state under the repository. Read-only; no backend added |
| `tests/test_state_custody.py` | 7 tests. Four are sandbox Terraform probes, skipped if `terraform` is absent: fails closed when init or `TF_DATA_DIR` is omitted; correct init passes preflight and contains workspaces; init without backend config fails open and preflight catches it; workspace escapes without `workspace_dir`. Three are offline: preflight rejects the repository `.terraform` and a missing `CHECKPOINT_DISABLE`; no state artefacts on disk in the repository; **backend blocks not yet implemented** |
| `docs/terraform_state_custody.md` | Approved decisions, unresolved backup destination, retention, fail-safe matrix, `workspace_dir` in the init pattern, `check --root`, updated gates |
| This file | This closeout section |

### Closeout validation

| Check | Result |
| --- | --- |
| Full `pytest` | **352 passed, 1 skipped** (the opt-in drill; it passed separately when run with `M6L_RUN_DRILL=1`) |
| `make gcp-offline-check` | **108 passed, 1 skipped**; fmt passed |
| `ruff`; `terraform fmt`; `validate` (both roots); docs validator | Passed; both roots valid; 0 issues |
| Relocation procedure | Dry-run end to end on a throwaway Git repository with a fake `HOME`. Copy verified; Git HEAD and status identical; project key copied; evidence copied at 0600; **source left intact** |
| Sandbox Terraform probes | 4/4 executed and passed (not skipped) |
| `check` (no `--root`), umask 077 | Exit 0; no stray state under the repository |
| `check --root bootstrap` | Exit 1, as intended (no custody backend) |
| Checkpoint calls since the disclosed one | None (`checkpoint_cache` mtime unchanged) |
| `~/.secure`, `~/Developer` | Both absent; nothing created |
| Real roots | No backend block; no `init`, plan or apply run against them |

## Approvals required (none granted except the design choices above)

**Ordering constraint:** approval 8 must land before, or together with, approval 1. Without the backend block, the real roots fall back to state inside the repository.

1. Create the custody tree at `~/.secure/ebank-pilot-dev-261031-k7m4/` with the approved modes and the Spotlight exclusion.
2. **Choose the independent backup destination (unresolved).** Then **destructive** APFS (Encrypted) formatting.
3. Create the backup image and set up operator-only passphrase custody.
4. Run the restore drill on that medium.
5. ~~Custodian and retention~~: approved at design level. Confirm the retention edge case (never age-delete the newest verified backup).
6. **Repository relocation.** Execute the procedure above; retiring the Desktop copy is separate (6b). **Evidence relocation:** the `m6*` copy into custody (needs gate 1).
7. Optional hook installation.
8. Backend code change: partial `backend "local" {}` in both roots, `path` + `workspace_dir` backend config, the `TF_DATA_DIR` convention, the mandatory `check --root` preflight, and updating `test_backend_blocks_not_yet_implemented`.

**Exact next approval required:**
- **6:** execute the repository relocation, run by the operator outside any Claude session;
- **2:** name the backup destination;
- **retention edge case:** confirm it.

Gates 8 and 1 follow together, then 3–4, and only then the M6K bootstrap gates. **No M6M work was started. Stopped after M6L closeout.**
