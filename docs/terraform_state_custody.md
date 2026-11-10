# Terraform state custody, backup and recovery (M6L design; M6O backend implemented)

**Status (M6O, 2026-11-10):**
- Both roots declare a fail-closed local backend.
- The custody initialisation, preflight and wrapper tooling exist in [`scripts/terraform_custody.py`](../scripts/terraform_custody.py).
- **The custody tree has not been created, and no Terraform state exists.** Creating the tree is an operator action (gate 1). Every later step still needs the approvals listed at the end.

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

### Backend wiring (M6O, implemented)

Both roots declare the same explicit local backend, set to an **impossible sentinel**:

```hcl
  backend "local" {
    path          = "/dev/null/ebank-custody-init-required/terraform.tfstate"
    workspace_dir = "/dev/null/ebank-custody-init-required/workspaces"
  }
```

`/dev/null` is not a directory, so neither path can ever be created. The committed text contains no username. The real per-root custody paths are supplied **only** through `-backend-config`, which overrides both values. Only the wrapper supplies them:

```sh
# Terminal.app, never inside Claude Code; from the repository root.
python3 scripts/terraform_custody.py init-custody            # dry run: lists the 0700 directories
python3 scripts/terraform_custody.py init-custody --confirm  # gate 1: creates the tree, no state
python3 scripts/terraform_custody.py preflight --root bootstrap --phase init
python3 scripts/terraform_custody.py run --root bootstrap -- init      # gate 8 execution
python3 scripts/terraform_custody.py run --root bootstrap -- validate
```

`run` performs the preflight, then executes Terraform with:
- `-chdir=<root>`;
- `TF_DATA_DIR=tfdata/<root>`;
- `CHECKPOINT_DISABLE=1`;
- umask 077;
- every other `TF_*` variable removed (except `TF_LOG`, `TF_LOG_PATH`, `TF_IN_AUTOMATION` and `TF_INPUT`);
- for `init`: `-input=false -lockfile=readonly -backend-config=path=… -backend-config=workspace_dir=…`.

After `init`, it re-checks that the backend cache points at this root's custody paths and is 0600, and that no state artefact appeared under the repository. `--dry-run` shows the command and the preflight result without running anything. The pilot root uses `--root pilot-dev`.

**Command policy.**
- **Allowed:** `init`, `validate`, `plan`, `apply`, `show`, `output`, `providers`, `version`, `state list` and `state show`.
- **Plans:** `plan` must use `-out=` inside `plans/<root>/`. `apply` takes exactly one saved plan from `plans/<root>/`, so there is no unplanned apply and no other root's plan. `-var-file=` must be inside custody `inputs/`.
- **Refused commands:** `destroy`, `import`, `state rm/mv/push`, `workspace`, `force-unlock`, `taint`, `refresh` and `console`.
- **Refused flags:** `-lock` set to false/0, `-state`, `-state-out`, `-backup`, `-backend`, `-backend-config`, `-migrate-state`, `-force-copy`, `-chdir`, `-destroy`, `-upgrade`, `-lockfile`, `-plugin-dir` and `-from-module`; the `--flag` spellings; and the space-separated `-out x` / `-var-file x` forms.
- **Exceptions:** any refused command that is ever needed requires a reviewed wrapper change and a fresh verified backup first.

### Fail-safe review: what happens when initialisation or backend configuration is omitted

M6L results come from Terraform 1.16.5 with `backend "local" {}`. M6O re-ran them against the **exact sentinel block of the real roots**. Both used disposable sandboxes with no providers or resources, running only `init`, `state list` and `workspace`. They are encoded in `tests/test_state_custody.py` and `tests/test_terraform_custody.py`.

| Case | M6L (`backend "local" {}`) | **M6O (sentinel backend)** |
| --- | --- | --- |
| `init` never run (repository `.terraform/` or fresh `TF_DATA_DIR`) | Fails closed | **Fails closed** ("Backend initialization required") |
| Correct custody `init`, then `TF_DATA_DIR` forgotten | Fails closed | **Fails closed** |
| `init` **without** `-backend-config` | **Fails open**: state would default to `./terraform.tfstate` in the repository | **Fails closed**: `Failed to get existing workspaces: … not a directory`; no backend cache written |
| `init` with only `path` overridden | — | **Fails closed** (workspace sentinel) |
| `init` with only `workspace_dir` overridden | — | **Fails closed**: init errors; state can only be loaded from the unreachable sentinel |
| `init -backend=false` | — | init succeeds, but every state command stops at "Backend initialization required". The wrapper refuses `-backend` |
| `TF_WORKSPACE=dev` without custody `workspace_dir` | **Fails open**: creates `terraform.tfstate.d/` in the repository | **Fails closed**; nothing is created; the preflight refuses `TF_WORKSPACE` |
| Wrapper `init` (correct config) | — | Cache 0600 with this root's custody paths only; **no state file created**; nothing under the repository |
| Cross-root contamination (one root's cache points at the other's state) | — | The preflight refuses **both** roots |

M6L's concern that a sentinel "would fail only after cloud changes" applied to a writable or late-failing path. With the `/dev/null` sentinel, `init` itself fails, so no plan or apply can start. A deliberate `-backend-config` naming a wrong path is still possible outside the wrapper; the run-phase preflight catches it.

**Preflight** (`terraform_custody.py preflight --root <root> --phase init|run`, also run automatically by `run`). It is read-only and refuses on any of the following:
- **location:** the custody path fails the M6L policy (repository, Desktop, Documents, Downloads, iCloud, CloudStorage, or outside home);
- **symlinks:** `~/.secure` or the custody root is a symlink or resolves elsewhere; any custody directory is a symlink;
- **Git:** the custody root is inside **any** Git working tree, or the repository itself sits in a synchronised location;
- **tree contents:**
  - a directory is missing or not 0700;
  - a file is not 0600, or not owned by the operator;
  - the Spotlight marker is missing;
  - `state/<root>/` holds anything other than `terraform.tfstate`, `.backup`, lock info and `workspaces/`;
- **environment:**
  - `TF_WORKSPACE` is non-default;
  - any `TF_CLI_ARGS*`, `TF_CLI_CONFIG_FILE` or `TF_PLUGIN_CACHE_DIR` is set;
  - `TF_LOG` is set without `TF_LOG_PATH` inside custody `logs/`;
  - `TF_DATA_DIR` is set to anything but this root's `tfdata/<root>`;
- **root isolation:** either root's backend cache points at a path other than its own custody paths;
- **repository artefacts:** **any** state, plan, lock or workspace artefact under the repository;
- **backend block:** this root lacks the sentinel backend block;
- **credentials (M6P P1):**
  - any `GOOGLE_*` other than `GOOGLE_OAUTH_ACCESS_TOKEN`, any `CLOUDSDK_*`/`GCLOUD_*`/`GCE_METADATA_*`, `USER_PROJECT_OVERRIDE`, proxy or TLS-root variable is set;
  - the token is missing or malformed for `plan`/`apply`, or present for an offline command;
  - `TF_LOG` is set, or an ADC file exists, during a token run;
  - Terraform CLI configuration or plugin directories exist;
  - the provider blocks deviate from the identity design;
- **FileVault** is not confirmed;
- **run phase only:** the backend cache is missing or not local, or its `path`/`workspace_dir` are not the exact custody paths.

Further rules:
- **Default workspace only.**
- **`CHECKPOINT_DISABLE=1`**, which the wrapper always sets.
- The repository's provider-cache-only `.terraform/` directories hold **no backend cache** and are harmless: an init there fails closed. Removing them stays optional.

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

1. **Create the custody tree** (operator, Terminal.app): `python3 scripts/terraform_custody.py init-custody --confirm`, then `preflight --root bootstrap --phase init` and `preflight --root pilot-dev --phase init`, both of which must report no problems. Optionally also add `~/.secure` to System Settings → Spotlight → Privacy (see M6O risks).
2. ~~Choose the backup medium~~: **OneDrive selected (M6M).**
3. ~~Backup passphrase~~: **generated and stored by the operator (M6N preconditions).**
4. ~~Synthetic browser round trip~~: **passed (M6N).**
5. ~~Custodian and retention~~: **approved.**
6. ~~Repository relocation~~: **done.** The `outputs/runs` M6* evidence relocation into custody is still open; it needs gate 1.
7. Optionally install the pre-commit hook.
8. ~~Backend code change~~: **implemented in M6O** (sentinel backend, wrapper, preflight; pending the operator's review and commit). **Executing** `run --root <root> -- init` (provider download from the Terraform registry into custody `tfdata/`; no GCP call, no state) is a separate operator step after gate 1.
9. ~~`cryptography` dependency~~: **done.**
10. **First real state:** only through the M6K bootstrap gates (saved plan in `plans/bootstrap/`, explicit approval), immediately followed by the first real encrypted backup and download-verify for that root.

Only after these can the M6K bootstrap gates proceed.
