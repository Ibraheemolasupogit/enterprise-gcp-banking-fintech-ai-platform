# M6M secure Terraform backup preparation (OneDrive, offline)

Date 2026-11-09. Starting revision `9c7703a`. Repository at `~/Developer/enterprise-gcp-banking-fintech-ai-platform`, outside the iCloud-synchronised Desktop. **Offline preparation only:**
- no Google Cloud API call, IAM, billing or provider change;
- no Terraform `plan`, `apply` or `destroy`, and no `init` against the real roots;
- no real state, no backend block, no custody tree;
- no access to OneDrive credentials or browser sessions, and no upload or download;
- no commit or push.

Runbook: [terraform_state_backup_onedrive.md](../terraform_state_backup_onedrive.md).

## Inputs inspected

| Item | Finding |
| --- | --- |
| M6L design | [terraform_state_custody.md](../terraform_state_custody.md), `scripts/state_custody.py` (`check`, `check --root`, `staged`, the `hdiutil` `drill`), `tests/test_state_custody.py`, `scripts/hooks/pre-commit`, `.gitignore`. The backup destination was unresolved, and the retention age edge case was open |
| Terraform roots | `infrastructure/terraform` (pilot-dev) and `infrastructure/terraform/bootstrap`. Still no `backend` block (test-enforced). Both `validate` OK. Lockfiles unchanged (`61cea4a1…`) |
| Host | FileVault **On**. `~/.secure` absent. Shell umask 022 (the tooling sets 077 itself). `core.hooksPath` unset |
| Crypto tooling | `age` and `gpg` absent. `openssl enc` has no authenticated (AEAD) mode. Python `cryptography` **46.0.0** is installed in the system framework Python. It was initially undeclared; it is declared since the hardening below |
| Session marker | `CLAUDECODE=1` is set in Claude Code subprocesses. The new CLI uses it to refuse real backup and restore commands |

## Design decisions

- **Encryption:** AES-256-GCM with a key from scrypt (N=2^17, r=8, p=1), under an operator passphrase of at least 24 characters (32+ recommended, from the password manager).
  - Each file gets a fresh salt and nonce.
  - The header is authenticated as associated data.
  - Each backup is one self-contained `<root>-<UTC>-serial-<NNNNNN>.tfstate.enc` file, suited to a browser upload.
  - On decrypt, the accepted KDF parameters are bounded: n in 2^14…2^18, r=8, p=1. This refuses both weak settings and memory-exhaustion headers.
- **Workflow:**
  1. `create` writes to custody `backups/outbox/` and verifies after write.
  2. The operator uploads it in the browser.
  3. The operator downloads it back into `backups/inbox/`.
  4. `verify-download` checks the ledger SHA256, authenticates, validates root, timestamp, serial, SHA256 and lineage, and deletes the plaintext.
  5. Only then is the ledger marked `verified`.
  6. Retention is applied, deletion is done manually, and `mark-deleted` records it.
- **Retention (confirmed by this task):** per root, the newest verified backup is always kept; up to 3 recent verified backups are kept while ≤ 30 days old; unverified files never count.
- **Separation:** the root is in the file name, the authenticated header and the manifest. Cross-root restore is refused. Lineage is tracked per root. A serial rollback at `create` is refused.
- **No artifacts in the repository:** custody paths pass the M6L location policy. `*.tfstate.enc` and `*.partial` are git-ignored (`*.tfstate.*`), blocked by the `staged` guard and flagged by `stray_state_paths()`. The drill refuses a workdir inside the repository.

## Changed files

| File | Change |
| --- | --- |
| `scripts/state_backup.py` (new) | Container format, `create`, `verify-download`, `retention`, `mark-deleted`, synthetic `drill`. Real commands refuse inside Claude Code or without a TTY, and refuse when the custody tree is missing, has loose modes, sits in a forbidden location or lacks FileVault. Passphrases are read only via `getpass` and never printed |
| `tests/test_state_backup.py` (new) | 29 tests (27 below, plus 2 dependency tests) |
| `scripts/state_custody.py` | `LAYOUT` gains `backups`, `backups/outbox`, `backups/inbox` and `recovery`, so `check` verifies their modes. No behaviour change otherwise |
| `Makefile` | `gcp-offline-check` includes `tests/test_state_backup.py` |
| `pyproject.toml`, `requirements.txt` | `cryptography>=46.0.0,<51` (dependency hardening); `pandas>=2.2,<3` (pandas compatibility) |
| `docs/terraform_state_backup_onedrive.md` (new) | Operator runbook: setup, backup, upload, verify-download, retention, restore test, disaster recovery, failure handling |
| `docs/terraform_state_custody.md` | Destination set to OneDrive; the confirmed retention rule; layout gains the backup directories; backup, restore and approvals sections rewritten; repository relocation recorded |
| `docs/index.md` | Links to the runbook and this evidence |
| `docs/upgrade/m6m_validation.md` (new) | This file |

`.gitignore`, the hook, the Terraform roots, providers and lockfiles are **unchanged**.

## Simulated end-to-end restore (synthetic fixtures)

`python3 scripts/state_backup.py drill --workdir "$TMPDIR"`, run with the **production KDF**. It took 2.4 s and exited 0. The drill builds a disposable fake home with the custody layout and writes `m6m-fixture-*` states with zero resources for **both roots**. A local directory stands in for OneDrive; nothing left the machine.

| Check | Result |
| --- | --- |
| Custody preconditions in the fake home | No problems |
| Backups created | `bootstrap-20261109T120000Z-serial-000005.tfstate.enc` and `pilot-dev-20261109T120001Z-serial-000006.tfstate.enc`, both 0600 |
| Ciphertext contains no plaintext markers (`lineage`, `fixture_marker`, `m6m-fixture`) | True |
| Live state deleted, then simulated upload, download and `verify-download` | **Both roots verified.** `recovery/` is empty afterwards |
| Wrong passphrase / bit flip / truncation / forged header root / restore as the other root | **All rejected** |
| Re-verification after the failed attempts | Verified |
| `terraform show -json` on the restored pilot-dev fixture (empty directory, `CHECKPOINT_DISABLE=1`) | Parses; the fixture marker was found |
| Retention plan | Each root keeps its single verified backup; nothing to delete; no warnings |
| Stray state under the repository | None |
| Work directory removed | True. No `m6m-drill-*` directory remains |

## Tests and validation

| Check | Result |
| --- | --- |
| `tests/test_state_backup.py` | **27 passed**. Covers: round trip; ciphertext hides plaintext and is non-deterministic; 4 mutation cases fail closed; wrong passphrase, short passphrase and weak, oversized or non-power-of-two KDF refused; production KDF floor; outbox mode, ledger content and no-overwrite; verify round trip with plaintext cleanup; 5 verify failure cases (wrong passphrase, corrupt, wrong root, renamed, missing ledger = authenticated but not recorded); `--keep-plaintext` stays in 0700 custody `recovery/`; `create` refuses lock info, loose mode, lineage change and serial rollback; root separation; retention (3 recent kept, newest kept past 30 days, unverified ignored, roots separate, single-copy warning); `mark-deleted` refuses retained backups; Claude/TTY refusal; the CLI refuses here and creates nothing in `HOME`; custody mode and repository refusal; drill refuses a repository workdir; backup names git-ignored and blocked; full synthetic drill |
| Full `pytest` | **379 passed, 1 skipped** (the opt-in M6L `hdiutil` drill) |
| M6L drill (`M6L_RUN_DRILL=1 … -k drill`) | 1 passed. No `M6LDRILL` image left attached |
| `make gcp-offline-check` | **135 passed, 1 skipped**; `terraform fmt` passed |
| `ruff check .` | Passed |
| Docs validator (`PYTHONPATH=src`) | Passed, 0 issues |
| `terraform validate` (both roots) | Valid. No `init` was run |
| `state_custody.py check` (umask 077) / `staged` | Exit 0 / exit 0 |
| Hygiene | `~/.secure` absent; no `*.tfstate*`, `*.enc`, `*.partial` or ledger under the repository; `core.hooksPath` unset; `git diff --check` clean; HashiCorp `checkpoint_cache` mtime unchanged (no checkpoint egress) |

## Dependency hardening (2026-11-09)

**Convention found.** `requirements.txt` mirrors `pyproject.toml` (`dependencies` plus the `dev` extra) as unpinned names. CI (`.github/workflows/python-ci.yml`, Python 3.11) and the README install with `pip install -r requirements.txt` and `pip install -e .`. There is no lockfile and no hash-pinning tool, so none was introduced.

**Policy.** `cryptography>=46.0.0,<51` is added to both files. It is a runtime dependency, because the operator CLI needs it, not a dev-only tool. It is the only bounded entry, because it guards the backup encryption:
- the floor is the version already in use;
- the ceiling excludes untested future major releases.

`state_backup.CRYPTO_REQUIREMENT` holds the same string, and a test keeps the three locations in sync.

**Import errors.** A missing package produces `REFUSED: the 'cryptography>=46.0.0,<51' package is required for AES-256-GCM and is not installed for this Python; install the project dependencies: python3 -m pip install -r requirements.txt` (exit 2, no traceback). `importorskip` was removed, so a missing package now fails the tests instead of silently skipping them.

| Check | Result |
| --- | --- |
| New tests | `test_cryptography_is_declared_consistently_and_installed` (`pyproject.toml`, `requirements.txt` and the script agree; installed version within range) and `test_missing_cryptography_gives_installation_message` (library API and CLI exit 2). `tests/test_state_backup.py` now has **29 passed** |
| Clean venv, Python 3.12.8, `pip install -r requirements.txt` (no system site-packages; resolved `cryptography` 50.0.2) | `pip check` clean; Ruff passed. Full suite: **378 passed, 2 failed, 2 skipped**. Both failures are **unrelated to M6M**: unpinned `pandas` resolved to 3.1.1 (system 2.3.3), which breaks `test_synthetic_v2_contracts.py::…[1.25]` and `test_synthetic_v2_workflow.py::test_serialized_runs_are_deterministic…`. Those tests and `src/` are unchanged by M6M. The extra skip is the optional HCL parser, which is not installed in the venv |
| Clean venv, Python 3.12.8, `cryptography==46.0.0` (lower bound) plus `pytest` only | Backup and custody tests **70 passed, 1 skipped** (the opt-in `hdiutil` drill). Production-KDF drill `ok` |
| Clean venv, Python 3.14.8, `cryptography` 50.0.2 from `requirements.txt` plus `pytest` | **70 passed, 1 skipped**. Drill `ok` |
| Clean venv without `cryptography` | CLI prints the message above (exit 2); tests fail loudly (18 failed) instead of skipping |
| Python 3.11 (the CI version) | **Not run locally**: no 3.11 interpreter is installed. `cryptography` 46–50 publishes wheels for 3.11 (abi3); CI will exercise it |
| Network | `pip` downloaded from PyPI into disposable scratchpad venvs only. No GCP, OneDrive or HashiCorp traffic |

## Pandas compatibility (2026-11-09)

**Finding.** `pandas` was unpinned in both dependency files. A fresh environment resolved pandas 3.1.1 (the working environment has 2.3.3), and two tests failed. Both failures reproduce on pandas 2.3.3 with `pd.set_option("future.infer_string", True)`, which previews the pandas 3 default. So the cause is pandas 3's default `str` dtype (PDEP-14), not a defect in the code:

| Test | Failure under pandas 3 | Cause |
| --- | --- | --- |
| `test_synthetic_v2_contracts.py::test_decimal_precision_and_lexical_money_cannot_be_silently_rounded[1.25]` | `TypeError: Invalid value '1.25' for dtype 'str'` while the test sets up its data, before the validator runs | The column read with `dtype=str` is now `str` and rejects a float, so the "validator must reject non-lexical money" contract can no longer be exercised |
| `test_synthetic_v2_workflow.py::test_serialized_runs_are_deterministic_revalidated_and_hashed` | `assert all(is_object_dtype(...))` fails | The v2 contract (`validation.py` reads `dtype=str, keep_default_na=False`) promises object-dtype string columns; pandas 3 returns `str` |

**Decision.** Add `pandas>=2.2,<3` to `pyproject.toml` (with a comment giving the reason) and `requirements.txt`. **No contract, source or test was changed.** Supporting pandas 3 means a deliberate contract migration later (out of scope). No repository-wide locking was introduced. `numpy` stays unpinned: pip pairs pandas 2.2.0 with NumPy 1.26.4, using pandas' own `numpy<2` metadata, and pandas 2.3.3 with NumPy 2.x.

**Clean environments** (scratchpad venvs, no system site-packages, `pip install -r requirements.txt`):

| Environment | Resolved | `pip check` | Two previously failing tests | Full `pytest` | Ruff |
| --- | --- | --- | --- | --- | --- |
| CPython 3.12.8, constraint as written | pandas 2.3.3, numpy 2.5.4, cryptography 50.0.2 | Clean | **7/7 passed** (all parameter cases) | **380 passed, 2 skipped** | Passed |
| CPython 3.12.8, floor `pandas==2.2.0` | pandas 2.2.0, numpy 1.26.4 | Clean | **7/7 passed** | **380 passed, 2 skipped** (one pyarrow deprecation warning) | Passed |
| CPython 3.14.8, constraint as written | pandas 2.3.3 | Clean | Passed | **380 passed, 2 skipped** | — |

The 2 skips are the optional HCL parser, which is not installed in the venvs, and the opt-in `hdiutil` drill.

**Python 3.11 (CI).** This is **compatibility evidence, not an executed validation**: no 3.11 interpreter is installed locally. `pip install --dry-run --only-binary=:all: --python-version 3.11 --implementation cp` resolved `requirements.txt` to 36 binary-wheel packages on both `manylinux_2_28/2_17 x86_64` (the CI runner) and macOS arm64. It selected pandas 2.3.3, numpy 2.4.6, cryptography 50.0.2, scikit-learn 1.9.1 and pydantic 2.14.3. **Negative control:** the same resolution with `pandas==3.1.1` fails with `ResolutionImpossible` (`pandas<3 and >=2.2 and pandas==3.1.1`), while unconstrained pandas 3.1.1 resolves for 3.11. The constraint is therefore what keeps CI on pandas 2. Actual 3.11 execution happens only when CI runs.

**Incidental observation (no change made).**
- `tests/__pycache__` holds git-ignored bytecode compiled at an earlier checkout path (`~/Desktop/GitHub_repository/gcp-financial-crime-ml-intelligence-platform`, now absent), so some tracebacks show that path. Source size and mtime are unchanged, so the cached code is current. The cache can be cleared at any time.
- The system Python also has a stale editable-install `.pth` pointing at that absent path. Pytest is unaffected (`pythonpath = ["src"]`).

## Remaining security risks

1. **Passphrase is the single secret.** Anyone holding it and the OneDrive file can read the state. Escrow, strength (only the length is enforced) and the paper copy are operator responsibilities and are unverified. Losing it makes every backup worthless; the routine restore test pasting from the password manager is the control.
2. **Single independent destination.** If the Microsoft account is locked, deleted or compromised, the backups are unavailable. Integrity is still protected, because tampering is detected, but availability is not. A second destination is not planned.
3. **Plaintext exists on the laptop by design.** Live state, and transient restore copies in `recovery/`, are plaintext to the logged-in user, protected only by FileVault at rest and by 0600/0700 modes. Deletion is a plain `unlink`; APFS and SSD give no secure erase. Python cannot zero the passphrase or plaintext in memory.
4. **The Claude-session refusal is a guard, not a boundary.** It relies on `CLAUDECODE` and a TTY check. Any process running as the operator can still read custody. A Claude Code deny rule for `~/.secure/**` is still recommended (M6L risk 3).
5. **Browser and clipboard path.** Pasting the passphrase exposes it to the clipboard (Universal Clipboard, clipboard managers). The browser and its extensions see the ciphertext and file names. Downloads land in `~/Downloads` before being moved; that is ciphertext only, but the operator must move it.
6. **Metadata visible to Microsoft:** the root name, UTC timestamp, serial and file size. There is no state content.
7. **Dependency supply chain.** `cryptography` is declared with a range, but not hash-pinned or locked, in line with the repository convention. A compromised PyPI release inside the range would be installed. Running `pip-audit` or adding a hash-locked constraints file would be a separate, repository-wide decision. `pandas` is now bounded `<3`. The other dependencies are still unpinned, so future major releases (for example NumPy or scikit-learn) can drift in fresh environments.
8. **Ledger integrity.** `backups/ledger.jsonl` is not authenticated. Someone able to edit it already has the live plaintext, so it adds no new exposure, but it can be lost with the laptop. Disaster restore therefore works without it (AEAD only).
9. **Freshness.** The tool does not yet block an apply when the newest verified backup is older than the live state. That remains a procedural gate (restore test before each apply).
10. **Browser round trip not yet exercised** against the real OneDrive folder, not even with synthetic data.
11. **Earlier M6L risks** remain open: advisory locking, FileVault recovery-key escrow unverified, optional hook, the `outputs/runs` evidence relocation, and the personal email in `m6b_validation.md`.

## Approvals required

None was granted by M6M beyond the user's selection of OneDrive and the retention rule stated in the task.

1. **Accept the M6M design and tooling.** Review this diff and the runbook, then commit (the user commits).
2. ~~Dependencies~~: **done:** `cryptography>=46.0.0,<51` and `pandas>=2.2,<3` are declared. Clean 3.12 and 3.14 environments pass. Python 3.11 has resolution evidence only, until CI runs.
3. **OneDrive folder hardening (operator, browser):** confirm `GCP-Banking-Terraform-Encrypted-Backups` has no sharing links, MFA is on, and no OneDrive sync client is linked.
4. **Passphrase generation and escrow (operator only):** password-manager item plus sealed paper copy. Not to be shared with Claude.
5. **Synthetic browser round trip:** approve adding a small `rehearse` command that writes one encrypted *synthetic* fixture into the outbox, and the operator's manual upload, download and `verify-download` of it. This needs the custody tree (gate 1).
6. **M6L gates 8 + 1 together:** backend block plus the custody tree, now including `backups/outbox`, `backups/inbox` and `recovery`.
7. Optional: a Claude Code deny rule for `~/.secure/**`; hook installation.

**Exact next approval required:** item 1 (accept and commit M6M, including the dependency hardening), then items 3–4. Confirm on the first CI run that Python 3.11 actually passes. Bootstrap implementation is **not** started. **Stopped after M6M.**
