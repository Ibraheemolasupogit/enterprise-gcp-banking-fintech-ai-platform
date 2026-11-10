# M6N synthetic OneDrive backup and recovery drill

Prepared 2026-11-09; operator drill run 2026-11-10. Starting revision `cac58dd` (M6M committed by the user; the user reports that Python CI passed).

**Status: PASSED, for a synthetic fixture only.** On 2026-11-10 the operator uploaded the encrypted synthetic backup through the OneDrive browser interface, downloaded it again, and recovered it interactively in Terminal.app. All checks passed (`ok: true`). Claude never accessed OneDrive or the passphrase. There was no GCP call, real state, backend, custody tree, OneDrive access, IAM or billing change, Terraform plan/apply/destroy, commit or push.

## Operator-confirmed preconditions (as stated by the user, not verified by tooling)

- The OneDrive folder `GCP-Banking-Terraform-Encrypted-Backups` is private.
- Microsoft two-step verification is on.
- The backup passphrase was generated and stored by the operator.
- No real state and no custody tree exist. The tooling confirms that `~/.secure` and `~/m6n-onedrive-drill` were absent before the drill.

## Why the operator, not Claude, produces the encrypted file

The drill must prove that the **escrowed operator passphrase** decrypts a file that has made a real browser round trip. Encrypting with any other passphrase would prove nothing about recovery. The passphrase must never reach Claude, and the M6M tool refuses real or drill encryption inside a Claude Code session (`CLAUDECODE`) or without a TTY. So Claude prepared and tested the tooling, and the operator runs one command in Terminal.app to produce the `.enc` file.

## Preparation (done)

| File | Change |
| --- | --- |
| `scripts/state_backup.py` | `rehearse-create` and `rehearse-verify` (operator-only: TTY, no Claude session, FileVault). The drill directory defaults to `~/m6n-onedrive-drill`. It must be new, inside home, outside the repository, outside `~/.secure` and outside synced folders; it is created at 0700 with a `SYNTHETIC-DRILL-NOT-REAL-STATE.txt` marker. The fixture is synthetic (zero resources, lineage `m6n-fixture-bootstrap-*`, explicit marker output) and its plaintext is deleted after encryption. Names use the `m6n-synthetic-drill-` prefix, and the real `NAME_RE` is anchored so production commands refuse drill files. `name_pattern()` and a `prefix` parameter reuse the production `create_backup` and `verify_download` paths unchanged. If encryption fails, the drill directory is removed |
| `tests/test_state_backup.py` | **11 new tests (40 total):** a single labelled ciphertext with no plaintext and real-name refusal; round trip with cross-root and wrong-passphrase rejection and fixture recovery; fail-closed for wrong passphrase, corrupted file and browser-renamed `(1)` file; drill directory refused in the repository, custody, Desktop or outside home; cleanup on failure; CLI refusal inside Claude with nothing created |
| `docs/terraform_state_backup_onedrive.md` | "M6N synthetic browser drill" operator section |
| `docs/index.md` | Link to this file |

**Pre-flight simulation (production KDF, throwaway random passphrase, scratch home, a local folder standing in for OneDrive):**
- `rehearse-create` took 0.4 s and produced one 889-byte `m6n-synthetic-drill-bootstrap-…-serial-000001.tfstate.enc`.
- `rehearse-verify` took 0.7 s and reported `ok: true`: byte-identical round trip; cross-root restore rejected; wrong passphrase rejected; passphrase authenticated; all name, header, manifest, SHA256, lineage and ledger checks true; recovered fixture SHA256 matches the ledger, with synthetic lineage, zero resources and the marker present; `terraform show -json` parses; ledger status `verified`.
- The scratch directory was removed.

**Validation:**
- full `pytest`: **392 passed, 1 skipped**;
- `make gcp-offline-check`: **148 passed, 1 skipped**;
- Ruff and the docs validator pass;
- `staged` guard exit 0; no state, plan, backup or credential file is tracked;
- `~/.secure` absent; `git diff --check` clean.

## Operator drill results (2026-11-10): passed

Source: `~/m6n-onedrive-drill/evidence-20261110T050503Z.json` (0600). The operator produced it in Terminal.app, and Claude read it after the run. It contains only names, sizes, hashes and booleans. Every value the operator reported matches the file.

| Check | Result |
| --- | --- |
| Encrypted file | `m6n-synthetic-drill-bootstrap-20261110T045557Z-serial-000001.tfstate.enc`, 889 bytes |
| Ciphertext SHA-256 | `be88189f7178a4c2e791eb80ec7ef7c2018d09fcd9dfe87d06bee2ac9adce95f` |
| Real browser upload to and download from `GCP-Banking-Terraform-Encrypted-Backups` | Done manually by the operator |
| Downloaded name matches exactly; byte-identical round trip (SHA-256 equals the ledger) | **True / true** |
| Operator's escrowed passphrase authenticates (AES-256-GCM tag) | **True**: escrow and recovery path proven |
| Random wrong passphrase rejected | **True** |
| Cross-root restore as `pilot-dev` rejected (`root_ok` false) | **True** |
| Name, created, serial, SHA256 and lineage checks; ledger and ledger-state match | **All true**; `verified: true`; ledger status `verified` |
| Restored file mode | **0600** |
| Recovered fixture: SHA256 matches ledger, synthetic lineage `m6n-fixture-bootstrap-*`, zero resources, marker present, serial 1 | **All true** |
| `terraform show -json` parses the recovered fixture (offline, `CHECKPOINT_DISABLE=1`) | **True** |
| Overall | **`ok: true`** |

**Minor finding (cosmetic, not fixed here because this step is limited to docs).** `expected_size` in the report is `null`: `backup_status()` does not copy the ledger `size` field. Byte identity is proven by the SHA-256 match, so the result is unaffected. A one-line fix plus a test can follow in a later approved change.

### What this proves, and what it does not

| Proven, for the **synthetic** fixture | **Not** proven: real infrastructure or state recovery |
| --- | --- |
| The browser upload and download preserve the ciphertext byte for byte | No real Terraform state exists. No real backup has been created, uploaded or restored |
| The operator's stored passphrase decrypts a OneDrive round-tripped file through the production `create_backup` and `verify_download` code | Recovery of real resources, lineage continuity after real applies, or a real serial history |
| Authentication fails closed for a wrong passphrase; root isolation refuses a cross-root restore | Placing a restored state back into custody, or reconciling it with GCP (`plan -refresh-only`). Both need separate approvals |
| The recovered file is intact, 0600, and parses with `terraform show` | That the real custody tree, backend wiring (`check --root`) and real `create` and `verify-download` work. They need gates 8 and 1, then the first real backup |
| | The pilot-dev root's own backup and round trip. Only rejection was exercised |

### Cleanup (operator; pending)

These are left to the operator; Claude deleted nothing.
- `~/Downloads/m6n-synthetic-drill-…tfstate.enc` (ciphertext) is still present: delete it.
- `~/m6n-onedrive-drill/` is still present. It holds two ciphertext copies, the drill ledger, the evidence JSON and the recovered **synthetic** plaintext fixture in `recovery/restore-*/`. Keep the evidence JSON somewhere private if wanted, then run `rm -rf ~/m6n-onedrive-drill`.
- The OneDrive copy: delete it, or keep it deliberately as a passphrase canary. Record the choice here: _operator to confirm_.

## Residual risks

- The synthetic ciphertext on OneDrive is encrypted under the real passphrase. It holds no secret content, but it gives an attacker one more offline guessing target. With a high-entropy passphrase and scrypt this is negligible. Delete it after the drill unless it is kept deliberately as a canary.
- The drill exercises the bootstrap root only. Root isolation is shown by refusing a `pilot-dev` restore, not by a second upload.
- The M6M risks still apply (single passphrase, single destination, plaintext on the laptop while the operator is logged in).

## Approval gates remaining

1. Operator review and commit of the M6N preparation and evidence (uncommitted: `scripts/state_backup.py`, `tests/test_state_backup.py`, both docs, `docs/index.md`).
2. Operator cleanup as listed above, and the OneDrive canary decision.
3. Gates 8 and 1 together: the backend block, plus the custody tree with `backups/` and `recovery/`. Only then the first real backup and verify for each root, then the M6K bootstrap gates. **Neither M6O nor bootstrap has been started.**
