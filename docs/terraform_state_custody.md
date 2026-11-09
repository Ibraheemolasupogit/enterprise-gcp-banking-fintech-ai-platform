# Terraform state custody, backup and recovery (M6L design)

**Design only.** No state, custody directory, backup medium or hook exists yet. Every implementation step below needs the approvals listed at the end.

**Design choices approved by the user (M6L closeout, 2026-11-09):**
- **State root:** `~/.secure/ebank-pilot-dev-261031-k7m4/`, on the FileVault-protected internal Data volume.
- **Permissions:** Terraform runs under `umask 077`; folders are `0700`; sensitive files are `0600`.
- **Custodian:** the operator is the sole custodian.
- **Retention (preliminary):** the latest 3 verified backups, kept for up to 30 days.

Approving the design is not approval to create anything. **The independent encrypted backup destination is unresolved:** no medium exists or is assumed. `~` means the operator's macOS home directory, so no username is recorded. Tooling: [`scripts/state_custody.py`](../scripts/state_custody.py) (stdlib only, no cloud access).

## Proposed layout (FileVault-protected internal Data volume, outside Git and iCloud)

```
~/.secure/ebank-pilot-dev-261031-k7m4/          0700  custody root (.metadata_never_index)
├── state/bootstrap/terraform.tfstate             0700 dir / 0600 files  bootstrap root only
├── state/pilot-dev/terraform.tfstate             0700 dir / 0600 files  pilot root (dev) only
├── tfdata/bootstrap/  tfdata/pilot-dev/          0700  TF_DATA_DIR per root (backend cache, providers)
├── plans/bootstrap/   plans/pilot-dev/           0700  saved plans + JSON exports + SHA256
├── inputs/bootstrap-dev.tfvars  inputs/pilot-dev.tfvars   0600
├── state/<root>/workspaces/                      0700  backend workspace_dir (must stay empty: default workspace only)
├── logs/                                         0700  only approved TF_LOG/crash output
├── evidence/runs/                                0700  PROPOSED home for relocated outputs/runs M6* evidence (separate approval)
└── custody-log.md                                0600  backup ledger: hashes, serials, lineage (no contents)
```

Why this location:
- **The repository is not acceptable.** `~/Desktop` is synchronised by iCloud Desktop & Documents (M6L finding), so even git-ignored files under the repository are uploaded. `~/Documents`, `~/Downloads`, `~/Library/Mobile Documents` and `~/Library/CloudStorage` are rejected by `path_violations()`.
- `~/.secure` is on the internal APFS Data volume, with FileVault verified On, and is not a file-provider domain.

Ownership and isolation:
- Ownership and custody: the operator's macOS account only. **The operator is the sole custodian** of both states, the plans, the inputs and the backup passphrases. No group or other permission bits, no shared accounts, no `sudo`.
- Each root has its own state directory, inputs file and `TF_DATA_DIR`, so a backend cache from one root cannot be reused by the other.
- Record each state's **lineage** after its first apply. Every later backup must match it.

Future backend wiring is a code change at approval gate 8 and is **not implemented in M6L**: both roots still have no `backend` block, and a test asserts this. At that gate, add a partial `backend "local" {}` to both roots, then initialise with **both** `path` and `workspace_dir`:

```sh
umask 077
export CHECKPOINT_DISABLE=1
export TF_DATA_DIR="$HOME/.secure/ebank-pilot-dev-261031-k7m4/tfdata/bootstrap"
terraform -chdir=infrastructure/terraform/bootstrap init \
  -backend-config="path=$HOME/.secure/ebank-pilot-dev-261031-k7m4/state/bootstrap/terraform.tfstate" \
  -backend-config="workspace_dir=$HOME/.secure/ebank-pilot-dev-261031-k7m4/state/bootstrap/workspaces"
python3 scripts/state_custody.py check --root bootstrap   # must exit 0 before any later command
```

The pilot root follows the same pattern with `pilot-dev`.

### Fail-safe review: what happens when initialisation or backend configuration is omitted

These results come from Terraform 1.16.5 in disposable sandboxes (no providers, no resources; only `init`, `state list` and `workspace`). They are encoded as tests in `tests/test_state_custody.py`.

| Case | Result | Status |
| --- | --- | --- |
| `backend "local" {}` present, `init` never run (repository `.terraform/` or a fresh `TF_DATA_DIR`) | "Backend initialization required"; nothing written | **Fails closed** |
| Correct custody `init`, then `TF_DATA_DIR` forgotten | "Backend initialization required"; nothing written | **Fails closed** |
| `init` run **without** `-backend-config` | Succeeds. The cached `path` is unset, so state defaults to `./terraform.tfstate` in the repository root directory | **Fails open.** Caught by `check --root` |
| `TF_WORKSPACE=dev`, with `workspace_dir` not set | Even a read-only `state list` **creates `terraform.tfstate.d/dev/` in the repository** | **Fails open.** Fixed by `-backend-config=workspace_dir=…`; `check --root` refuses both |
| `TF_WORKSPACE=dev`, with custody `workspace_dir` | The directory is created inside custody, not in the repository | Contained; still refused by preflight |
| `init` alone (correct config) | No state file created | Safe |
| **Today's roots:** no backend block (implicit local backend); the pilot `.terraform/` is already initialised | A future apply would write `infrastructure/terraform/terraform.tfstate` inside the iCloud-synced repository | **Fails open.** No apply is approved; gate 8 must precede or accompany gate 1 |

A Terraform-native default cannot close the two fail-open cases. `-backend-config` overrides any hard-coded `path`, and an unwritable sentinel path would fail only **after** cloud changes, when state is written, orphaning resources. The safeguard is therefore a **mandatory preflight**: `python3 scripts/state_custody.py check --root <bootstrap|pilot-dev>` must exit 0 before every state-touching command. It refuses when:
- `TF_DATA_DIR` is not `tfdata/<root>` in custody;
- the backend cache is missing, is not `local`, or its `path`/`workspace_dir` are not the exact custody locations;
- `TF_WORKSPACE` is non-default;
- `CHECKPOINT_DISABLE` is not `1`;
- `umask` is not 077;
- the location policy, FileVault or mode checks fail;
- **any** `*.tfstate*`, `terraform.tfstate.d`, `*.tfplan` or lock-info file exists anywhere under the repository, git-ignored or not.

Further rules:
- **Default workspace only.** `terraform workspace new/select` and `TF_WORKSPACE` are prohibited.
- **`export CHECKPOINT_DISABLE=1`.** Otherwise even `terraform version` contacts HashiCorp's checkpoint service; this was observed on 2026-11-09 and is unrelated to GCP.
- **Before gate 8, remove the repository's provider-cache-only `.terraform/` directories**, or simply never use them, so that only custody `TF_DATA_DIR`s are ever initialised. This is a separate, reversible approval.

`python3 scripts/state_custody.py check` (without `--root`) verifies the location policy, FileVault, modes and ownership, symlinks, stray state under the repository and `umask 077`. Terraform creates files as `0666 & ~umask`, so the **default umask 022 would produce world-readable state**. The 0700 directories are the second barrier.

## Locking limitations

The local backend takes an advisory OS lock on the state file. That lock:
- works on this host only;
- is bypassed by `-lock=false` (prohibited) and by manual copies or edits;
- gives no ordering between the two roots (separate files, so none is needed);
- can leave a stale `.terraform.tfstate.lock.info` after a crash. Investigate one rather than deleting it blindly.

Operating rule: **one operator, one terminal, one root at a time.** No state ever goes on synced or network storage, where sync conflicts could fork the lineage.

## Encrypted backup

**Destination: UNRESOLVED.** No independent encrypted backup destination exists, and none is assumed. Until one is approved and its restore drill passes, there is **no backup**: the state has a single copy on the laptop, and no real state may be created. The candidate design, not approved, is:
- a dedicated external SSD, formatted **APFS (Encrypted)**, holding one **AES-256 encrypted sparse bundle** `ebank-pilot-state-backups.sparsebundle` created with `hdiutil`, giving two independent passphrases;
- the drive stored physically apart from the laptop.

Destinations rejected:
- Cloud and sync services: third-party custody and conflict risk.
- A GCS bucket in the pilot project: the provisioner's bucket rights could alter its own custody (M6J).
- An internal-only image: it shares the laptop's failure domain.

**Recovery-key custody:**
- Generate a random passphrase of at least 32 characters for each of the drive and the image.
- Store both in the operator's password manager, plus a sealed paper copy kept apart from the drive.
- Do not store them in the macOS keychain: always use `-stdinpass` and decline keychain prompts. Never share them with tooling or assistants.
- Separately confirm that the Mac's own FileVault recovery key is escrowed. That was not verified here.

**When to back up:**
- after every successful apply;
- before any `state rm/mv/import/push` or backend migration;
- after any restore.

**Procedure:**
1. Mount read-write with `-stdinpass -nobrowse`.
2. Create a new snapshot directory `<root>/<UTC>-serial-NNNNNN/`. Snapshots are created exclusively (`O_EXCL`) and never overwritten.
3. Write `terraform.tfstate` and `MANIFEST.json` (SHA256, serial, lineage, Terraform version, plus Git commit and saved-plan SHA256).
4. Detach.
5. Append the hash, serial and timestamp to `custody-log.md`.

The drill exercises these same snapshot and verify functions. A real `backup` command is to be added at the state gate.

**Retention (approved, preliminary):** keep the **latest 3 verified backups**, for **up to 30 days**. A backup counts only after its manifest hash, lineage and serial verify on the medium. Deletion is a manual, logged custodian action recorded in `custody-log.md`.

**Open point for confirmation:** if no apply happens for more than 30 days, the age limit alone would delete every backup. The proposed reading is that the newest verified backup is never deleted by age.

## Restore (and independent restore test)

1. Attach the image **read-only**. Verify `MANIFEST.json`: hash, lineage equal to the custody log, and the expected serial.
2. Copy to a fresh 0700 recovery directory. Never restore directly over live state.
3. Run `terraform show -json <file>` to prove it parses offline.
4. Only then, under an approved recovery change, replace the live file. A serial lower than the last recorded one needs explicit approval.
5. Any reconciling `plan -refresh-only` is a cloud action requiring its own approval.

**Independent test without real state:** `M6L_RUN_DRILL=1 python3 -m pytest tests/test_state_custody.py -k drill`, or `python3 scripts/state_custody.py drill --workdir <dir>`. The drill:
- writes a synthetic fixture (`lineage m6l-fixture-*`, zero resources) under umask 077;
- backs it up into a throw-away AES-256 sparse bundle with a random in-memory passphrase;
- deletes the live copy;
- checks that a wrong passphrase is rejected;
- restores read-only and verifies hash, lineage, serial and 0600 mode;
- checks that tampering is detected and that Terraform parses the restored file;
- removes everything.

Run it with `--workdir` on the backup medium to test that drive. Once real state exists, repeat steps 1–3 against the latest real snapshot before each apply gate.

## Plans, logs, outputs and credentials

| Artifact | Handling |
| --- | --- |
| Saved plans (`.tfplan`) and `show -json` exports | Same sensitivity as state: they embed prior state, configuration and variable values. Store only in `plans/<root>/` at 0600, named `<UTC>-<gitsha>.tfplan`. Record the SHA256 in evidence, never the contents. Delete after the apply is verified, or keep in the backup image if approved |
| Logs | `TF_LOG` unset by default. Provider debug logs can contain request and response bodies. Approved debugging writes only to `logs/` via `TF_LOG_PATH`, and is deleted after review. Move any `crash.log` there |
| Outputs | Names and emails only, no secrets. Do not paste raw `output -json`, state or plan JSON into evidence |
| Credentials | Per-session `GOOGLE_OAUTH_ACCESS_TOKEN` set by command substitution (never typed or echoed), `unset` afterwards. No ADC file, no keys, no tokens in tfvars, files or history, no `set -x` |
| Git | `.gitignore` covers state (including `terraform.tfstate.d/`), backups, lock info, plans, plan JSON, tfvars, override files, CLI config, crash logs, disk images, `.age`/`.gpg`/`.p12` and ADC/key JSON. `.gitignore` does not stop `git add -f`, so the optional [`scripts/hooks/pre-commit`](../scripts/hooks/pre-commit) blocks staged files by name and content (state markers, OAuth tokens, private keys, refresh tokens). It is installed only by the operator via `git config core.hooksPath scripts/hooks` |

## Approvals required before any real state exists

1. Create the custody tree at the exact path and modes above, plus the Spotlight exclusion.
2. Choose the backup medium, then format it as APFS (Encrypted). **Formatting destroys the drive's contents.**
3. Create the backup image and set up passphrase custody (operator-only).
4. Run the restore drill on that medium and record the evidence.
5. ~~Name the custodian and approve retention~~: **approved at design level** (operator as sole custodian; latest 3 verified backups, up to 30 days; age-limit edge case open).
6. Relocate the repository out of `~/Desktop`, and the `outputs/runs` M6* evidence into custody. The procedure is proposed in [M6L evidence](upgrade/m6l_validation.md#m6l-closeout-2026-11-09); execution is not approved.
7. Optionally install the pre-commit hook.
8. Approve the backend code change: `backend "local" {}` plus the `path` and `workspace_dir` partial config, the `TF_DATA_DIR` convention and the mandatory `check --root` preflight. This must precede or accompany gate 1.

Only after these can the M6K bootstrap gates proceed.
