# M7D incident: conditional BigQuery table IAM bindings rejected

Status: **recovery completed by the operator; offline correction implemented (2026-11-13), not applied.** The correction is uncommitted. Nothing in this document was executed by the assistant: no Terraform plan or apply, no cloud call, no access to custody or to the encrypted backups.

Related documents:
- [M7C1 loader identity, questions Q1–Q9 and gates L0–L7](m7c1_validation.md)
- [M7D runbook](m7d_runbook.md) (updated for this correction)
- [M7C2 validation](m7c2_validation.md)

## 1. What happened (as reported by the operator)

1. The operator applied the approved bootstrap `loader-access` saved plan (runbook §6.2). The plan had passed `--mode loader-access` with six creates, each carrying an expiring IAM condition.
2. The apply **partially failed**. GCP rejected the conditional IAM bindings on the three BigQuery tables: `customers`, `accounts` and `curated_transactions`.
3. The other three grants were created: loader jobs (project), loader objects (project, packet-prefix condition) and operator token minting on the loader account.

This was the failure mode predicted for M7C1 question **Q2** ("Apply error: fails closed, partial apply"). The provider accepted the `condition` block (Q1), but the BigQuery service rejected it.

No data was uploaded or loaded. The run stopped before the command bundle (§6.3–§6.4).

## 2. Emergency recovery (completed by the operator)

- The three temporary IAM grants that had been created were revoked.
- A live IAM read-back passed.
- The loader service account and its three custom roles were preserved.
- An encrypted Terraform state backup at **serial 42** was verified through the OneDrive round-trip.
- The 4,000-transaction ingestion packet still verifies against its approved manifest.

**Not known offline:** whether bootstrap state serial 42 still records any of the three revoked grants. This depends on how they were revoked (by a Terraform apply, or outside Terraform). The new `loader-recovery` checker mode (§4.2) accepts either case, and reports which applies.

## 3. Root cause

The M7C1 design bounded the three table members with the same expiry condition as the other loader grants. Whether BigQuery table IAM accepts conditions was an open question, Q1/Q2, assigned to live gates L0 and L3. The plan checker could only check the plan's shape. It could not know that the service would refuse that shape.

Contributing factor: the plan check passed a configuration that the service could not accept. A plan check proves intent, not service support.

## 4. Correction (offline, this change)

### 4.1 Terraform

`infrastructure/terraform/bootstrap/main.tf`:
- `google_bigquery_table_iam_member.loader` now has **no `condition` block**. It remains:
  - exactly three additive members, on `customers`, `accounts` and `curated_transactions`;
  - the custom role `bankpilot_dev_loader_tables` (`tables.get`, `getData`, `updateData`);
  - the loader service account as member;
  - created only while `loader_access_enabled = true`.
- **Unchanged:**
  - the expiring loader jobs grant (project);
  - the expiring, packet-prefix-conditioned loader objects grant (project);
  - the expiring operator token-minting grant on the loader account;
  - the loader account and the three custom roles. `infrastructure/iam/loader_roles.json` is deliberately not edited, because a description change would plan an in-place update of a preserved role. Its `loader_tables` description still says "with an expiring condition"; that wording is now out of date and is a reviewed follow-up.
- Comments and the descriptions of two variables were updated. No variable, validation or default changed.

### 4.2 Plan checker (`scripts/bootstrap_plan_check.py`)

- **`loader-access`:** each of the three table grants must be **unconditional**. Any condition is a problem, including the old approved expiry, an object condition or an empty condition object, as is a missing `condition` key. The other three grants still require the **exact** approved expiry (and, for objects, the exact prefix and bucket). Six creates and 15 unchanged, as before.
- **`loader-revoke`:** six deletes, as before. The deleted table grants must be unconditional; the other three must still carry one expiring loader condition.
- **New `loader-recovery`:** for the state left by this incident only. Requirements:
  - loader access disabled, with all loader variables at their disabled values;
  - the 15 resources unchanged;
  - nothing created;
  - **no table grant anywhere**, in the changes or the drift;
  - each of the three expiring grants is either deleted, already removed outside Terraform (drift delete), or absent from state;
  - etag-only drift is tolerated on exactly the same set as `loader-revoke`.

  The report lists `delete_addresses`, `already_removed` and `absent_from_state`. A full window revocation, with table grants in state, fails this mode and must use `loader-revoke`.
- **New `loader-revoke-partial`:** for a `loader-revoke` (or `loader-access`) apply that failed part-way. Requirements:
  - loader access disabled, with all loader variables at their disabled values;
  - the 15 resources unchanged;
  - nothing created, updated or replaced;
  - each of the **six** grants is either deleted with its exact approved shape, already removed outside Terraform, or absent from state. The approved shape is an expiring loader condition on jobs, objects and token minting, and no condition on the three input tables, with the right table, dataset, role, member and project;
  - **at least one grant is absent from state.** A plan in which all six are still recorded is a normal revocation, and is refused with a pointer to the strict `loader-revoke`;
  - etag-only drift on exactly the `loader-revoke` set. A drifted grant must also be deleted (a drifted grant is in state).

  The report adds `live_iam_readback_required: true`. A plan cannot see a grant that is live but missing from state, so the live read-back before and after the apply (runbook §6.8.1) is mandatory.
- **Shared hardening:** in both partial modes, a grant whose etag drifted must also appear as a delete.
- `loader-revoke`, the M6 modes, `loader-identity`, the pilot modes and their tables are unchanged.

### 4.3 Tests

`tests/test_loader_plan_check.py` (156 → **259** tests):
- The fixtures now render unconditional table grants.
- The Terraform text is checked: the table block has no condition, and the three other grants each keep exactly one expiry condition built from the reviewed locals.
- Adversarial cases:
  - `loader-access`: a table grant carrying the approved expiry, an object condition, `[{}]`, or no `condition` key, reported per table; and the three expiring grants made unconditional.
  - `loader-revoke`: a deleted table grant that had a condition, or that sits on a mart; and a deleted jobs or impersonation grant without its condition.
  - `loader-recovery`, 27 cases: any table grant created, deleted, unchanged or drift-removed; an expiring grant created or left in place; double accounting; duplicate drift; wrong role or missing condition; provisioner or runner changes; identity or role changes; every variable; non-etag or out-of-set drift; an errored plan; a key resource.
  - `loader-revoke-partial`, 42 cases:
    - unexpected creates: a recreated table or jobs grant, a fourth (mart) table grant, a key;
    - updates, replacement, a grant left in place, a role update;
    - unexpected deletions: a mart grant, the runner, provisioner or M6 impersonation grant, the loader account, a role, the audit config;
    - out-of-set drift removals;
    - every way a deleted grant can differ from the approved shape (table condition, role, member, dataset, table key, missing or wrong condition, wrong operator or account, project);
    - double accounting, duplicate drift, a removal reported as an update, widened drift, etag drift on a table grant or on a grant that is not deleted;
    - every variable, an errored plan, forbidden binding text.
  - Positive cases: every single-grant subset, seven mixed subsets, and etag drift. The strict `loader-revoke` still rejects each partial state with exactly one "expected delete missing" per absent grant. Complete revocations (including drift-removed grants) are refused by the partial mode and still pass `loader-revoke`.
- Mode isolation: the incident-state plan passes only the two partial modes; window plans pass neither.
- The CLI never prints the operator.

For every adversarial case, the failure reason was printed and confirmed to come from the intended check. Targeted mutations: **9 of 9** caught for the table correction and `loader-recovery`, and **9 of 9** for `loader-revoke-partial`. The checker was restored byte-identical each time.

## 5. Security effect of the correction (for review)

The table grants are now **standing until revoked**, not self-expiring. This departs from the M7C1 rule "never introduce permanent load privileges". The operator instructed the change explicitly. The controls that remain:

1. **No new credential or job after expiry.** The loader is keyless, and the only approved way to act as it is the operator's token-minting grant, which expires at `EXP`. After `EXP`:
   - no **new** loader token can be minted;
   - no load, query or DML job can run as the loader, because `bigquery.jobs.create` also expires.

   This does **not** end the table grants. A token issued before `EXP` stays usable on the three tables until the token's own expiry (R-A). The grants themselves persist until they are explicitly revoked (R-B).
2. **Scope by attachment.** The grants exist only on the three input tables, never on the dataset or marts, and with no create, update, delete or IAM rights.
3. **Plan checks.** `loader-access` refuses any other table, role or member. `loader-revoke` requires all three table grants to be deleted. `loader-recovery` refuses any table grant. `loader-revoke-partial` deletes only the exact approved grants still in state.

What is weaker than the M7C1 design, and must be accepted or mitigated:
- **R-A, token tail.** A loader token minted shortly before `EXP` stays valid for up to 1 hour. With the M7C1 design, table access stopped at `EXP`. Now `tabledata.list` (read) and `tabledata.insertAll` (streaming insert, which needs `tables.updateData` but no job) keep working with that token until **revocation**, not `EXP`.
  - Mitigation: the runbook now requires revocation to **start no later than `EXP` − 15 minutes**, when the bundle stops accepting steps. IAM removal takes effect for existing tokens after propagation.
- **R-B, missed revocation.** If the `loader-revoke` apply fails or is skipped, the table grants persist indefinitely. They stay usable by any token issued before `EXP`, until that token expires. After that, they are usable by any principal able to mint loader tokens or attach the loader (`actAs`). Today that is only the project Owner (L1 check).
  - Mitigation: the L7 read-back of each table policy is now mandatory, and closure (A11) is blocked until it shows no loader member.
- **R-C, partial revoke (addressed).** A `loader-revoke` apply that fails part-way is checked with `loader-revoke-partial` (runbook §6.8.1). Two limits remain:
  - a plan cannot see a grant that is live but absent from state, so the live IAM read-back before and after is mandatory;
  - removing such a grant outside Terraform is an operator decision.
- **R-D, cross-root visibility.** The pilot root does not see these members. `pilot-loaded` therefore cannot detect a leftover loader table grant; only the L7 read-back can.

Alternatives not implemented (they need a separate decision):
- a dataset-level grant: not possible, because the pilot root's dataset policy is authoritative (M6T D1);
- a project-level conditional grant narrowed by `resource.name` to the three tables: this would replace attachment scoping with condition scoping at project level, and its enforcement for BigQuery is unverified.

## 6. Re-entry gates (before a new loader-access window)

Each gate runs in Terminal.app by the operator; none is authorised by this document.

1. **R1, code:** commit this correction with CI green (new A1).
2. **R2, L0:** run `terraform_custody.py run --root bootstrap -- validate` on that commit.
3. **R3, live baseline (read-only):**
   - the project policy has no loader binding;
   - the loader account policy is empty;
   - the three table policies have no loader member;
   - the three input tables still report `numRows` 0. A partial apply cannot have loaded data, but confirm.
4. **R4, state reconciliation:**
   - Plan with the **base tfvars only** (`loader_access_enabled` false), then check:
     ```sh
     … show -json "$P" | python3 scripts/bootstrap_plan_check.py --mode loader-recovery
     ```
   - Expect `ok`, 0 creates, 15 unchanged and **0 deletes**. Because R3 confirmed that no grant is live, any planned delete means a grant still exists: stop and review.
   - If `already_removed` is non-empty, apply the saved plan to persist the refreshed state, then back up and verify. Confirm with `state list` that none of the six grant addresses remain. If one does, stop and review.
   - If `absent_from_state` lists all three, state is already clean; nothing is applied.
5. **R5, onward:** continue from runbook A4 with a **new** window and a new `loader-access` plan, which must report three expiring and three unconditional creates. Then render a new bundle (A6) for the new window.
   - The A2 packet and its manifest SHA-256 are unchanged.
   - **Never re-apply the failed saved plan.**

Any failure: stop, revoke if a window is open, and review.
