# Terraform state custody, backup and recovery (M6L design)

**Design only.** No state, custody directory, backup medium or hook exists yet. Every implementation step below needs the approvals listed at the end.

**Design choices approved by the user (M6L closeout, 2026-11-09):**
- **State root:** `~/.secure/ebank-pilot-dev-261031-k7m4/`, on the FileVault-protected internal Data volume.
- **Permissions:** Terraform runs under `umask 077`; folders are `0700`; sensitive files are `0600`.
- **Custodian:** the operator is the sole custodian.
- **Retention:** the latest 3 verified backups, kept for up to 30 days; **the newest verified backup is kept regardless of age** (confirmed in M6M).
- **Backup destination (M6M):** a private OneDrive folder, `GCP-Banking-Terraform-Encrypted-Backups`, reached through the browser only. It holds only client-side AES-256-GCM ciphertext; see [terraform_state_backup_onedrive.md](terraform_state_backup_onedrive.md).

Approving the design is not approval to create anything. OneDrive is the selected backup destination (M6M), but nothing has been uploaded and the custody tree does not exist. `~` means the operator's macOS home directory, so no username is recorded. Tooling: [`scripts/state_custody.py`](../scripts/state_custody.py) (stdlib only, no cloud access).

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
├── backups/outbox/  backups/inbox/                0700  M6M ciphertext only (*.tfstate.enc), staged for manual browser upload/download
├── backups/ledger.jsonl                          0600  M6M backup ledger: hashes, serials, lineage, verification events (no contents)
├── recovery/                                     0700  M6M restore tests; plaintext only transiently, 0600
├── evidence/runs/                                0700  PROPOSED home for relocated outputs/runs M6* evidence (separate approval)
└── custody-log.md                                0600  backup ledger: hashes, serials, lineage (no contents)
```

Why this location:
- **The repository is not acceptable.** It was on the iCloud-synchronised `~/Desktop` (M6L finding) and is now at `~/Developer/` (relocated by the operator before M6M). Even so, state stays out of every working tree. `~/Documents`, `~/Downloads`, `~/Library/Mobile Documents` and `~/Library/CloudStorage` are rejected by `path_violations()`.
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

**Destination: OneDrive via the browser (selected by the user in M6M; not yet used).** The full operator runbook is [terraform_state_backup_onedrive.md](terraform_state_backup_onedrive.md). In outline:
- `scripts/state_backup.py create --root <root>` encrypts the custody state with AES-256-GCM, using a key derived by scrypt from an operator passphrase held in the password manager. It writes one `*.tfstate.enc` file to `backups/outbox/` and records it in `backups/ledger.jsonl`.
- The operator uploads that file manually in the browser, downloads it back into `backups/inbox/`, and runs `verify-download`. That command byte-compares the file with the ledger, decrypts it in `recovery/`, validates it and deletes the plaintext. Only then does the backup count as verified.
- The real commands refuse to run inside a Claude Code session or without a TTY.

The earlier cloud rejection ("third-party custody and conflict risk") is addressed as follows:
- Microsoft holds only ciphertext, encrypted under a key it never sees.
- There is no sync client, and file names are immutable and unique, so no sync conflicts or lineage forks can occur.

Still rejected:
- a GCS bucket in the pilot project, because the provisioner could alter its own custody (M6J);
- an internal-only image, because it shares the laptop's failure domain.

The external-SSD and `hdiutil` sparse-bundle candidate from M6L is not pursued. The M6L `drill` is kept as a regression test.

**Passphrase custody:**
- Use one random passphrase of at least 32 characters (the tool refuses fewer than 24). Store it in the operator's password manager, plus a sealed paper copy kept apart from the laptop.
- It must not be the Microsoft account password. Never store it in Git, OneDrive, notes, shell history or assistant prompts.
- Separately confirm that the Mac's FileVault recovery key is escrowed. That has not been verified.

**When to back up:**
- after every successful apply;
- before any `state rm/mv/import/push` or backend migration;
- after any restore.

**Retention (confirmed):** applied per root.
- Keep the **newest verified backup regardless of age**.
- Keep up to **3** of the most recent verified backups while they are **≤ 30 days** old.
- Run `state_backup.py retention` to see which backups may be deleted. Deletion is manual, in the browser, and is recorded with `mark-deleted`, which refuses to release a retained backup.
- Unverified files never count.

## Restore (and independent restore test)

1. Download the backup into `backups/inbox/`. Run `verify-download --root <root> --file <name>`, which checks the GCM tag, the name/header/manifest/state agreement on root, timestamp and serial, the SHA256, the lineage and the ledger byte match.
2. For a real recovery, add `--keep-plaintext`. The state is left in a fresh 0700 `recovery/restore-*/` directory. Never restore directly over live state.
3. Run `terraform show -json <file>` (with `CHECKPOINT_DISABLE=1`) to prove the restored file parses offline.
4. Only then, under an approved recovery change, replace the live file. A serial lower than the last recorded one needs explicit approval.
5. Any reconciling `plan -refresh-only` is a cloud action requiring its own approval.

**Independent test without real state:**
- `python3 scripts/state_backup.py drill` (M6M; part of the default `pytest` run) exercises the full OneDrive workflow on synthetic `m6m-fixture-*` states for both roots in a temporary fake home, with a simulated OneDrive directory. It covers create, upload, download, verify and state loss, and rejects a wrong passphrase, a bit flip, truncation, a forged header root and a cross-root restore. It also checks the retention plan and cleans up.
- The older M6L `hdiutil` drill (`M6L_RUN_DRILL=1`) remains available.

Once real state exists, repeat step 1 against the newest real backup of each root before each apply gate.

## Plans, logs, outputs and credentials

| Artifact | Handling |
| --- | --- |
| Saved plans (`.tfplan`) and `show -json` exports | Same sensitivity as state: they embed prior state, configuration and variable values. Store only in `plans/<root>/` at 0600, named `<UTC>-<gitsha>.tfplan`. Record the SHA256 in evidence, never the contents. Delete after the apply is verified, or keep in the backup image if approved |
| Logs | `TF_LOG` unset by default. Provider debug logs can contain request and response bodies. Approved debugging writes only to `logs/` via `TF_LOG_PATH`, and is deleted after review. Move any `crash.log` there |
| Outputs | Names and emails only, no secrets. Do not paste raw `output -json`, state or plan JSON into evidence |
| Credentials | Per-session `GOOGLE_OAUTH_ACCESS_TOKEN` set by command substitution (never typed or echoed), `unset` afterwards. No ADC file, no keys, no tokens in tfvars, files or history, no `set -x` |
| Git | `.gitignore` covers state (including `terraform.tfstate.d/`), backups, lock info, plans, plan JSON, tfvars, override files, CLI config, crash logs, disk images, `.age`/`.gpg`/`.p12` and ADC/key JSON. `.gitignore` does not stop `git add -f`, so the optional [`scripts/hooks/pre-commit`](../scripts/hooks/pre-commit) blocks staged files by name and content (state markers, OAuth tokens, private keys, refresh tokens). It is installed only by the operator via `git config core.hooksPath scripts/hooks` |

## Approvals required before any real state exists

1. Create the custody tree at the exact path and modes above, including `backups/outbox`, `backups/inbox` and `recovery`, plus the Spotlight exclusion.
2. ~~Choose the backup medium~~: **OneDrive selected (M6M).** The operator still confirms the folder is private, MFA is on, and no sync client is linked.
3. Generate the backup passphrase in the password manager and make the sealed paper copy (operator-only).
4. Run a browser round trip with a **synthetic** encrypted fixture before any real state exists. Not yet implemented: it needs a small `rehearse` command and its own approval.
5. ~~Name the custodian and approve retention~~: **approved**, including the newest-verified-regardless-of-age rule.
6. ~~Relocate the repository out of `~/Desktop`~~: done by the operator. The `outputs/runs` M6* evidence relocation into custody is still open (needs gate 1).
7. Optionally install the pre-commit hook.
8. Approve the backend code change: `backend "local" {}` plus the `path` and `workspace_dir` partial config, the `TF_DATA_DIR` convention and the mandatory `check --root` preflight. This must precede or accompany gate 1.
9. ~~Declare the `cryptography` dependency~~: **done (M6M hardening).** `cryptography>=46.0.0,<51` is in `pyproject.toml` and `requirements.txt`.

Only after these can the M6K bootstrap gates proceed.
