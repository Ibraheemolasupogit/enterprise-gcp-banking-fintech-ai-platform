# Encrypted Terraform state backups to OneDrive (M6M runbook)

**Status (2026-11-10): the M6N synthetic browser drill passed** ([evidence](upgrade/m6n_validation.md)). It covered a real OneDrive round trip and recovery of a synthetic fixture with the operator's passphrase. **No real state has been backed up or restored:** the custody tree, backend block and real backups do not exist yet. Every step that touches real state waits for the approvals in [M6M evidence](upgrade/m6m_validation.md#approvals-required). Tooling: [`scripts/state_backup.py`](../scripts/state_backup.py). It never contacts a cloud API and never uploads, downloads or reads browser sessions or credentials. Custody layout and preflight: [terraform_state_custody.md](terraform_state_custody.md).

## Design summary

| Property | How it is met |
| --- | --- |
| OneDrive never holds plaintext | Only `*.tfstate.enc` files from `backups/outbox/` are uploaded. The CLI reads plaintext only from `state/<root>/terraform.tfstate` in custody, and writes plaintext only inside custody `recovery/` |
| Independent, authenticated encryption | **AES-256-GCM** under a 256-bit key from **scrypt** (N=2^17, r=8, p=1; 128 MiB). Each file gets a fresh 16-byte salt and a fresh 12-byte nonce. The public header (format, root, timestamp, KDF and cipher parameters) is GCM associated data, so changing any byte, truncating the file, using a wrong passphrase or swapping the root fails closed. Microsoft's own encryption is an extra layer and is not relied on |
| No secrets in Git, prompts, logs or OneDrive | The passphrase is read only with `getpass` from the terminal: never from `argv`, environment, files or shell history. It is never printed. `create` and `verify-download` **refuse to run inside a Claude Code session** (`CLAUDECODE`) and refuse without a TTY. Reports and the ledger hold names, sizes, SHA256s, serials and lineage only |
| Integrity | Verify-after-write at creation. After upload, the file is downloaded back, its SHA256 is byte-compared to the ledger, and it is decrypted and validated in a private directory: GCM tag; name, header, manifest and state all agree on root, timestamp and serial; state SHA256 and lineage match. Only then is it **verified** |
| Root separation | Names begin with `bootstrap-` or `pilot-dev-`. The root is authenticated in the header and the manifest. `verify-download --root` refuses a file from the other root. Lineage is tracked per root, and `create` refuses a lineage change or a serial rollback |
| Nothing in the repository | All paths come from `~/.secure/ebank-pilot-dev-261031-k7m4/` and pass the M6L location policy (no repository, no Desktop, Documents, Downloads or cloud-sync folders). `*.tfstate.enc` and `*.partial` are git-ignored, blocked by the pre-commit guard and flagged by `stray_state_paths()` |
| Never overwritten | Written via an `O_EXCL` partial file plus a no-clobber hard link. Names are unique by UTC second and serial |

File format: `MAGIC "M6MTFBK\x01"` ‖ 4-byte header length ‖ header JSON ‖ AES-GCM(ciphertext ‖ 16-byte tag). The plaintext is a 4-byte manifest length ‖ manifest JSON ‖ the exact state bytes.

**Dependency policy.** AES-256-GCM comes from `cryptography>=46.0.0,<51`. It is declared in `requirements.txt`, which CI and the README install from, and in `pyproject.toml`, so it no longer relies on a globally installed package.
- The lower bound, 46.0.0, and the newest release allowed (50.0.2 at the time of writing) were both tested in clean virtual environments.
- The upper bound keeps an untested major release from being picked up silently. Raising it means re-running `tests/test_state_backup.py` and the drill.
- When the package is missing, the CLI exits 2 with an installation hint, and the tests fail rather than skip.
- Install it with `python3 -m pip install -r requirements.txt`, preferably in a virtual environment.

Why not the alternatives: `openssl enc` has no authenticated mode; `age` and `gpg` are not installed (installing either is a supply-chain change); an `hdiutil` image has no per-file authentication tag and a sparse bundle is a directory, unsuitable for browser upload.

## One-time setup (each step needs its approval)

1. **OneDrive folder (operator, browser only).** In `GCP-Banking-Terraform-Encrypted-Backups`, open *Manage access* and confirm that **no sharing link and no other person** exists. Confirm MFA on the Microsoft account. **Do not install or link the OneDrive desktop sync app** for this folder: browser upload only, so nothing appears under `~/Library/CloudStorage`. Personal Vault is optional; it adds a second unlock but auto-locks.
2. **Passphrase (operator, password manager).** Generate a random password of at least 32 characters, or 6+ random words; the tool refuses fewer than 24 characters. Save it as a new item *"ebank-pilot Terraform backup encryption passphrase (M6M)"*. It must differ from the Microsoft account password. Keep a sealed paper copy apart from the laptop. Never type it into Claude, a chat, a file, a note or a commit. When pasting, prefer the password manager's auto-clearing clipboard; Universal Clipboard can copy it to other Apple devices.
3. **Custody tree (gate 1, together with gate 8).** Add the backup directories to the M6L tree:

   ```sh
   umask 077
   C="$HOME/.secure/ebank-pilot-dev-261031-k7m4"
   mkdir -p -m 700 "$C"/backups/outbox "$C"/backups/inbox "$C"/recovery
   chmod 700 "$C"/backups
   ```

## Backup procedure (plain Terminal.app, never inside Claude Code)

Back up after every successful apply, before any `state rm/mv/import/push` or backend migration, and after any restore. Commands run from the repository root; `$C` is as above.

1. **Create.** `python3 scripts/state_backup.py create --root bootstrap` (or `pilot-dev`). Paste the passphrase twice. The output gives the file name, size in bytes, ciphertext SHA256 and serial; nothing secret. The ledger `backups/ledger.jsonl` records `created`. An existing lock-info file, a non-0600 state, a lineage change or a serial lower than the newest verified backup is refused.
2. **Upload (browser).** OneDrive → `GCP-Banking-Terraform-Encrypted-Backups` → *Upload → Files*. In the macOS file dialog press **⌘⇧G**, paste `~/.secure/ebank-pilot-dev-261031-k7m4/backups/outbox/`, and select **only** the new `*.tfstate.enc` file. Never upload from `state/`, `recovery/` or anywhere else. Wait for OneDrive to report completion.
3. **Confirm the upload.** The file appears with exactly the expected name. *Details* shows a size consistent with step 1; the browser rounds it, so the byte-exact proof is step 5. *Manage access* still shows private.
4. **Download it back (browser).** Select the file → *Download*. Then, in Terminal: `mv ~/Downloads/<file> "$C/backups/inbox/"`. If the browser renamed it (for example `… (1).tfstate.enc`), rename it back to the exact name; any other name is refused.
5. **Verify.** `python3 scripts/state_backup.py verify-download --root bootstrap --file <file>`. **Paste the passphrase from the password manager, not from memory**; this also proves the escrowed copy is correct. Exit 0 and `"verified": true` record `verified` in the ledger. The temporary plaintext is deleted.
6. **Tidy.** `rm "$C/backups/inbox/<file>"`. Once verified, also remove the outbox copy. Both are ciphertext, but the laptop is not the backup.
7. **Retention.** Run `python3 scripts/state_backup.py retention --root bootstrap`. Delete only the files listed under `delete` from OneDrive (browser), then record each with `python3 scripts/state_backup.py mark-deleted --file <file>`. This command refuses any file the policy keeps.

## Retention policy

Applied per root (bootstrap and pilot-dev are independent):
- **The newest verified backup is always kept, regardless of age.**
- Up to **3** of the most recent verified backups are kept while **≤ 30 days** old.
- Other verified backups may be deleted manually.
- **Unverified or failed** files never count towards retention and are never kept in place of a verified one. They are listed under `unverified` for investigation, then deleted.
- Warnings appear when a root has no verified backup, or when the newest verified backup is over 30 days old (create a fresh one).

OneDrive keeps deleted files in its recycle bin for a while. They are ciphertext, so that is acceptable. Empty the bin only if desired.

## Restore test and disaster recovery

- **Routine restore test.** Before every apply gate, download the newest verified backup per root and run step 5 again. A failure blocks the gate.
- **Disaster recovery** (laptop or custody lost; ledger missing). Rebuild the custody tree (gate 1), download the newest backup into `backups/inbox/`, then run `verify-download … --keep-plaintext`. Without a ledger the report shows `ledger_match: null` and `verified: false`, and records nothing. `authenticated: true` with all `*_ok` checks true proves the file is intact and genuine. The restored state stays in a 0700 `recovery/restore-*/` directory at 0600. Moving it into `state/<root>/` is a separate, approved recovery change (M6L restore steps 2–5). Any `plan -refresh-only` is a cloud action with its own approval.

## Failure handling

| Failure | Response |
| --- | --- |
| Passphrases differ at `create` | Nothing written. Re-run |
| Verify-after-write or post-write hash mismatch | The file is removed and nothing is recorded. Check the disk, then re-run |
| `create` refused: lock info, mode, lineage or serial | Stop. Do not bypass. A lineage change or serial rollback means the live state is not what was backed up before: investigate under the recovery procedure |
| Upload interrupted, wrong name or wrong size | Delete the remote copy, then re-upload from the outbox |
| `ledger_match: false` (downloaded bytes differ) | Re-download once. If it is still different, treat the remote copy as corrupt: delete it, `mark-deleted`, and create and upload a fresh backup. The ledger records `verify-failed` |
| `authentication failed` with the escrowed passphrase | **Stop.** Either the stored passphrase is wrong or the file was modified. Confirm the escrowed value against the paper copy. If the backup cannot be opened with it, it is worthless: fix escrow, create a new backup, and keep the apply gate closed until a new one verifies |
| Root, serial, timestamp or lineage mismatch | Wrong or substituted file. Do not use it. Investigate |
| OneDrive unavailable or account locked | No independent backup is reachable. No state-changing Terraform command may run until one verifies again |
| Passphrase believed exposed | Generate a new passphrase, create and verify new backups, then delete every backup made with the old one (their contents stay readable to anyone holding the old passphrase) |

## M6N synthetic browser drill (before any real state)

This proves the real browser round trip and the operator's escrowed passphrase using a **synthetic fixture only**:
- no custody tree, backend or real state;
- a disposable `~/m6n-onedrive-drill/`, outside the repository, outside `~/.secure` and outside synced folders, containing a `SYNTHETIC-DRILL-NOT-REAL-STATE.txt` marker;
- file names prefixed `m6n-synthetic-drill-`, which the real `verify-download` refuses.

Run every command in **Terminal.app**, not in Claude Code (the tool refuses there), from the repository root:

1. `python3 scripts/state_backup.py rehearse-create`. Paste the backup passphrase twice from the password manager. This writes a fake bootstrap state (zero resources, lineage `m6n-fixture-bootstrap-*`), encrypts it with the production code path, deletes the plaintext and prints the `.enc` path, size and SHA256.
2. Upload that single file to `GCP-Banking-Terraform-Encrypted-Backups` (⌘⇧G → `~/m6n-onedrive-drill/backups/outbox/`), then download it back (*Download* → `~/Downloads`).
3. `python3 scripts/state_backup.py rehearse-verify --downloaded ~/Downloads/<file>`. Paste the passphrase once. This checks:
   - the downloaded bytes against the ledger;
   - that a restore as `pilot-dev` is refused;
   - that a random wrong passphrase is refused;
   - that **the operator passphrase authenticates**;
   - every name, header, manifest, SHA256 and lineage check;
   - that the recovered fixture matches and that `terraform show -json` parses.

   A non-secret report is written to `~/m6n-onedrive-drill/evidence-<UTC>.json`. Exit 0 means the drill passed.
4. Clean up: delete `~/Downloads/<file>`; delete the drill file from OneDrive, or keep it as a passphrase canary; then `rm -rf ~/m6n-onedrive-drill`.

## Residual risks

Recorded in [M6M evidence](upgrade/m6m_validation.md#remaining-security-risks).
