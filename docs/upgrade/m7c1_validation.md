# M7C1 dedicated loader identity and least-privilege IAM

Date 2026-11-12. Baseline `main` at `594a25d` (M7A and M7B committed). Project `ebank-pilot-dev-261031-k7m4`, region `europe-west2`.

**Offline implementation only.** M7C1 did none of the following:
- call gcloud, `bq` or any cloud API;
- run any Terraform command except `fmt -check` (no init, validate, plan or apply);
- change IAM;
- touch custody files or credentials;
- download packages;
- commit or push.

The provisioner's temporary access stays revoked. Everything added is **disabled by default**: the deployed bootstrap state (11 resources) is unaffected until an approved apply.

The provider schema was not executed or queried. A strings scan of the local provider binary could not settle whether `condition` is supported on BigQuery table IAM, so that is live gate L0.

## Design summary

| Decision | Choice | Why |
| --- | --- | --- |
| Identity | New keyless account `bankpilot-dev-loader`, created by the bootstrap root | Separation of duties: the provisioner stays "no data, no jobs" (M6K), and the runner keeps its read-only access to the input tables. No key resource exists, and checkers reject one |
| Activation | Two switches. `loader_identity_enabled` creates the account and three custom roles, which grant nothing on their own. `loader_access_enabled` creates six expiring grants | The durable identity is reviewed once. Each window is a separate, small, reversible change |
| Who uses it | Only the single named operator, through an expiring token-minting grant on the loader account (the existing `impersonate` role: `getAccessToken` only) | The same proven pattern as the M6S provisioner window |
| Overlap | Loader access is refused while provisioner access is on (Terraform validation and checker) | One privileged window at a time |
| Owning root | Bootstrap root (bootstrap administrator), its own state, applied without impersonating anyone | Grants of data privileges stay outside the provisioner's reach; the provisioner holds `tables.setIamPolicy` and would otherwise be able to grant itself load rights |

## Permission matrix

| Operation (M7D) | Permission | Role (custom) | Scope | Granted where | Condition |
| --- | --- | --- | --- | --- | --- |
| Run three load jobs | `bigquery.jobs.create` | `bankpilot_dev_loader_jobs` | Project (the only scope this permission has) | `google_project_iam_member.loader_jobs[0]` | Expiry |
| Create-only upload | `storage.objects.create` | `bankpilot_dev_loader_objects` | Objects under `m7/ingestion/<packet_id>/` in the pilot bucket | `google_project_iam_member.loader_objects[0]` | Expiry, `resource.type == storage.googleapis.com/Object` **and** packet-prefix `resource.name` |
| Read-back of uploaded objects (MD5, size, generation); the load job's source read | `storage.objects.get` | same | same | same | same |
| Load into the existing table | `bigquery.tables.updateData` | `bankpilot_dev_loader_tables` | Exactly `customers`, `accounts` and `curated_transactions` in `bankpilot_dev` | `google_bigquery_table_iam_member.loader["<table>"]` (×3) | Expiry |
| Read table metadata (row count, schema) | `bigquery.tables.get` | same | same | same | Expiry |
| `tabledata.list` read-back for reconciliation | `bigquery.tables.getData` | same | same | same | Expiry |
| Mint loader tokens | `iam.serviceAccounts.getAccessToken` | existing `bankpilot_dev_impersonate` | The loader account only | `google_service_account_iam_member.operator_loader_impersonation[0]` | Expiry; the single `user:` operator |

**Deliberately excluded** (tests enforce each exclusion):

| Permission | Reason for exclusion |
| --- | --- |
| `storage.objects.delete` / `update` | Without delete, an existing object cannot be overwritten. Create-only is then enforced by IAM, independently of `ifGenerationMatch=0` |
| `storage.objects.list` | Object conditions cannot narrow listing reliably. The packet's three exact names are checked with `get` |
| `storage.buckets.*` | Bucket metadata and IAM are out of scope |
| `bigquery.tables.create` | Load uses `CREATE_NEVER`. Whether the API still demands this permission is live gate L4 |
| `bigquery.tables.update` | It allows schema relaxation and **setting a table expiry (deferred deletion)**. It is excluded even though Google's general load documentation lists it. If L4 shows a load needs it, stop for review; do not add it in the window |
| `bigquery.tables.delete`, `export`, `*IamPolicy`; `bigquery.datasets.*` | Destructive, exfiltration or administrative rights |
| `bigquery.jobs.get` / `list` / `update` / `delete` | Inspecting the job the loader itself created is expected to work with `jobs.create` alone (gate L4). Otherwise the operator reads jobs as Owner |
| `serviceusage.services.use` | Only needed if a client sends a quota-project header; gate L4 |
| `iam.*`, `resourcemanager.*` | No identity or project administration |

### Why each scope is placed where it is

1. **Jobs at project level, narrowed only by expiry.** `bigquery.jobs.create` is checked on the project, so a resource-name condition cannot narrow it. The loader can only write to tables where it also holds `updateData`.
2. **Objects as a project-level member with a resource condition, not a bucket member.** The pilot root's bucket policy is **authoritative** (`google_storage_bucket_iam_policy`). A bucket-level loader member would be removed by the next pilot apply, and it would show as policy drift. Cloud Storage documents IAM Conditions on `resource.type` and `resource.name` (`projects/_/buckets/<bucket>/objects/<object>`) for buckets with uniform bucket-level access, which the pilot bucket enforces. This scoping is **documented but not live-verified** (gates L3 and L5).
3. **Tables as additive table-level members, not a dataset grant.** The dataset policy is authoritative in the pilot root (M6T D1) and must not be touched. Table-level membership confines the grant to the three tables **by attachment**, without relying on a `resource.name` condition. Additive members do not conflict with the pilot root's own runner members on the same tables.
4. **BigQuery table IAM Conditions are unverified** (L0 for the provider schema; L3 for API acceptance; optional L6 for enforcement). The expiry on these three grants is the second time limit, not the first; see the time controls below.

## Access and revocation model

| Step | Bootstrap variables | Plan (checker mode) | Result |
| --- | --- | --- | --- |
| 0. Today | All loader variables default (`false` / `null`) | None | **11 resources**, unchanged |
| 1. Identity (once) | `loader_identity_enabled = true` | 4 creates, 11 unchanged (`--mode loader-identity`) | Account plus 3 roles; **no grants** |
| 2. Window | plus `loader_access_enabled = true`, `loader_access_expiry` (≤ 8 h), `loader_bucket_name`, `loader_object_prefix = m7/ingestion/<packet_id>/` | 6 creates, 15 unchanged (`--mode loader-access --packet-id=… --bucket=…`) | Six expiring grants |
| 3. Revoke (straight after M7D) | Access back to `false`; expiry, bucket and prefix back to `null` | 6 deletes, 15 unchanged (`--mode loader-revoke`); grants already removed by hand are accepted as delete-drift | No loader grants; the identity stays dormant |
| Later M6 provisioner windows | Loader identity `true`, loader access off | The M6 `access`/`revoke` modes accept the 4 identity resources **unchanged only** | Unchanged M6 behaviour |

**Time controls, in order of strength:**
1. **Impersonation expiry.** The loader is keyless, and only the operator's expiring grant can mint its tokens. This mechanism was exercised in M6S/M6T (impersonation denied after revocation).
2. **Per-grant expiry conditions** on all six grants.
3. **Explicit revocation** apply with read-back, without waiting for expiry.

Tokens minted before expiry remain valid for **up to 1 hour**. The effective worst case is therefore expiry + 1 h, so choose the expiry to account for it.

**Terraform validation** (`bootstrap/variables.tf`):
- loader access requires the identity and **excludes provisioner access**;
- the expiry must be later than plan time and at most 8 h after it;
- the bucket must match the pilot bucket pattern;
- the prefix must match `^m7/ingestion/m7p-[0-9a-f]{24}/$`;
- the expiry, bucket and prefix must all be `null` while access is off.

**Never permanent:** every grant has `count` or `for_each` set by `loader_access_enabled` and carries a condition. The checker rejects an unconditioned or non-expiry condition, a condition with anything added (`|| true`), and any later expiry on a single grant.

## Plan checker (`scripts/bootstrap_plan_check.py`)

**New modes:**
- `loader-identity`: exactly the 4 identity creates. Roles must equal `loader_roles.json`; the account ID must be `bankpilot-dev-loader`.
- `loader-access`: exactly the 6 grant creates, with the 15 resources unchanged. Each grant has its exact role, member, scope and **exact** condition: the expiry for five, and expiry + object type + `projects/_/buckets/<approved bucket>/objects/m7/ingestion/<packet_id>/` for objects. Window ≤ 8 h. The variables must equal the approved packet prefix and bucket. **`--bucket` is never printed**; the report shows only `bucket_approved`.
- `loader-revoke`: exactly the 6 deletes, the identity unchanged, every loader variable off or null. Each deleted grant must still look like the approved grant (role, member, expiring loader condition).

**Drift:**
- The loader modes tolerate the same strict etag-only drift on `runner_jobs` and the audit config as the M6 windows.
- `loader-revoke` additionally tolerates etag-only drift on the two loader **project** grants. They are created in one apply, so the first one's stored project-policy etag goes stale. The rule is the same strict one: every other attribute identical and approved.
- Drift on table grants, the impersonation grant or the identity is rejected.

**M6 modes not weakened:**
- `ETAG_DRIFT_BY_MODE` is unchanged; the loader tolerances live in a separate `LOADER_ETAG_DRIFT_BY_MODE`.
- In `initial`, `recovery`, `access` and `revoke`, any loader variable present must hold its disabled value. Plans made before M7C1, which omit those variables, still pass.
- The loader identity may appear only in `access`/`revoke`, and only unchanged. Any loader grant is an unexpected resource.
- All earlier checker tests pass unmodified.

## Tests

| File | Change |
| --- | --- |
| `tests/test_loader_plan_check.py` | **New: 150 cases** |
| `tests/test_gcp_iam_separation.py` | Inventory list extended (an intended change); 5 new loader tests |
| `tests/test_terraform_custody.py` | The instance-count test now also counts the loader gates: **the disabled baseline is still 11**, with 2 for the provisioner window, 4 for the loader identity and 6 for a loader window. Its original assertions are kept |
| `tests/test_access_window_plan_check.py`, `test_bootstrap_plan_check.py`, `test_pilot_recovery_plan_check.py` | **Unchanged**; all pass |

The `test_loader_plan_check.py` cases:
- **Positive and reporting:**
  - valid identity, access and revoke plans;
  - eight hours exactly;
  - strict etag drift accepted (runner and audit in access; loader project grants in revoke);
  - grants already removed by hand.
  - The bucket and operator never appear in the report or CLI output.
  - The checker's conditions are rendered from the Terraform text and compared.
- **Privilege escalation and role widening:**
  - roles widened in the identity plan (jobs update, objects delete, tables update, tables `setIamPolicy`);
  - grants given the provision role, `bigquery.admin`, `storage.admin`, `dataEditor`, the runtime runner roles, Token Creator, or **a different loader role** (table ↔ jobs ↔ objects).
- **Wrong identities:**
  - grants to the runner, the provisioner, a user or `allUsers`;
  - impersonation on the provisioner or runner, or for a group, another user or the loader itself.
- **Missing or weakened expiry:**
  - no condition, two conditions, `|| true`;
  - a later expiry on one grant;
  - the M6 provisioner condition reused;
  - an object grant without the prefix, for the whole bucket, for another packet or bucket, or with an expiry-only condition;
  - an expiry over 8 h, before the plan, not `Z`, or missing;
  - no plan timestamp.
- **Scope:**
  - a fourth (mart) table;
  - a table key that disagrees with its ID;
  - another dataset or project;
  - a missing table grant;
  - a dataset member;
  - a bucket member.
- **Variables:**
  - the prefix or bucket differs;
  - `--packet-id` or `--bucket` missing or invalid;
  - identity off;
  - access off or missing;
  - overlap with provisioner access;
  - the provisioner expiry set;
  - a group as the operator;
  - audit logging off.
- **Unexpected resources and actions:**
  - a bundled provisioner grant, a key, `google_project_iam_binding` text, a data source;
  - an update, a recreated identity, a changed loader role;
  - an errored plan.
- **Drift:**
  - non-etag drift on the runner;
  - drift on a loader grant or the identity during access;
  - a "removed grant" drift outside revoke;
  - in revoke: a condition removed or a role widened by drift, drift on a table grant, duplicate drift, provisioner drift.
- **Incomplete revocation:**
  - a table, objects or impersonation grant left in place;
  - access still enabled;
  - expiry, bucket or prefix left set;
  - the identity or a role destroyed;
  - replacement or provisioner grants created;
  - a deleted grant that is not the approved one;
  - claiming one removal while skipping the other five.
- **Mode isolation:**
  - each loader plan passes only its own mode (all 7 bootstrap modes tried);
  - the M6 `access`/`revoke` modes pass with the identity unchanged, and reject 8 loader changes in each mode;
  - `initial`/`recovery` reject the loader variable;
  - plans without loader variables still pass the M6 modes.
- **CLI:** the loader modes, the missing-bucket exit code, and a wrong bucket that is not echoed.

**Mutation checks (all restored byte-identical; SHA-256 compared):**
- **Checker, 22 mutations, all detected:**
  - member, impersonation target, table scope, dataset, exact condition, object prefix, prefix and bucket variables;
  - window, revoke deletes, M6 loader variables, identity in `initial`;
  - table drift in revoke, loader drift in access, removal outside revoke;
  - identity required, operator check, condition shape, roles in `expected_roles`;
  - table role and project-grant role checked by name only, the access variable.

  Three survived the first pass (removal outside revoke, role-by-name, the access variable); tests were added for each.
- **Terraform and role JSON, 13 mutations, all detected:**
  - table grants or access count not gated;
  - the object prefix dropped;
  - impersonation on the provisioner;
  - the table condition removed;
  - `prevent_destroy` removed;
  - jobs granted to the runner;
  - the identity on by default;
  - the overlap validation removed;
  - the prefix regex broadened;
  - the 8 h bound widened;
  - an objects delete or tables update permission added.

## Validation

| Check | Result |
| --- | --- |
| Full `pytest` | **1428 passed, 1 skipped** (M7B baseline 1273 + 155) |
| `make gcp-offline-check` (now including `test_loader_plan_check.py`) | **1184 passed, 1 skipped**; `terraform fmt` passed |
| `ruff check .` | Passed |
| `terraform fmt -check -recursive` | Passed |
| Docs validator; `final_project_check.py` | 0 issues; 79 checks, 0 issues |
| `git diff --check` (including new files) | Passed |

**`terraform validate` was not run.** It is gate L0.

## Unresolved provider and API questions

> **M7D update (2026-11-13):** Q1 and Q2 were answered at apply. The provider accepted the table `condition` block, but **BigQuery rejected conditional table bindings**, and the `loader-access` apply partially failed. The table grants are now unconditional, ended by revocation. Gates L3 and L6 below are superseded by the [incident correction](m7d_incident_table_iam.md) and the updated [M7D runbook](m7d_runbook.md). Residual risk 2 is replaced by incident risks R-A to R-D.

| # | Question | Effect if wrong | Resolved by |
| --- | --- | --- | --- |
| Q1 | Does `google_bigquery_table_iam_member` (google 7.31.0) accept a `condition` block? | `validate` or plan fails: fails closed, nothing granted | L0 |
| Q2 | Does BigQuery accept, store and enforce IAM Conditions on table bindings? | Apply error: fails closed, partial apply. A silently ignored condition would leave the table grants bounded only by impersonation expiry and revocation | L3 (read-back, policy version 3); optional L6 |
| Q3 | Do project-level `resource.type`/`resource.name` conditions narrow `storage.objects.create`/`get` to the packet prefix? | Too narrow: the upload is denied (safe). Too broad: the loader could create objects elsewhere in the project | L5 |
| Q4 | Does a load into an existing table with `CREATE_NEVER` + `WRITE_EMPTY` need only `jobs.create` + `tables.updateData` (+ `get`)? Google's general docs also list `tables.create`/`update` | Load denied: atomic, table stays empty. **Stop; do not add `tables.update` in the window** | L4 |
| Q5 | Is the load job's read of the source object checked against the loader's conditional `objects.get`? | Load denied: atomic | L4 |
| Q6 | Can the loader read its own job with `jobs.create` alone? | The operator inspects the job as Owner (read-only) | L4 |
| Q7 | Does `gcloud storage cp --if-generation-match=0` need `storage.buckets.get` or `objects.list`? Do clients need `serviceusage.services.use`? | Upload or call denied: review before adding anything | L4 |
| Q8 | Does refresh drift at revoke match the model (etag-only on the loader project grants)? | The checker fails closed and names the attribute | L7 |
| Q9 | Does anyone besides the operator hold token-minting rights on the loader (inherited Token Creator)? | Unbounded loader use | L1 |

## Live verification gates before and during M7D

All gates run in the operator's own session (Terminal.app), never in Claude Code. They are evidence for M7D, **not authorised by M7C1**.

- **L0, offline:** `terraform_custody.py run --root bootstrap -- validate` on the committed code. This confirms Q1 and the cross-variable validations.
- **L1, read-only:** re-read the project IAM policy and the loader account's policy once it exists. No principal other than the operator's window grant may hold `getAccessToken`, Token Creator or `actAs` on the loader.
- **L2, identity:**
  - plan → `--mode loader-identity` (`ok`, 4 creates) → approve the SHA-256 → apply → back up the bootstrap state.
  - Read back: the account exists with **no user-managed keys**, and the 3 roles have exactly the JSON permissions.
  - Record the account's numeric `uniqueId` for the audit mapping.
- **L3, window:**
  - plan with the packet prefix → `--mode loader-access --packet-id=<approved> --bucket=<custody bucket>` → approve the SHA-256 → apply → back up.
  - Read back the project policy (2 conditional loader bindings, exact) and each table's policy with **requestedPolicyVersion 3**: the loader binding must be present **with its condition**.
  - If BigQuery rejected or dropped the condition: **stop**, revoke immediately, design review. **Never retry without the condition.**
- **L4, functional (this is M7D itself):** upload with `--if-generation-match=0`; check MD5, size and generation; three load jobs; job status; `tabledata.list`. **Any permission error: stop, revoke, review.** No role widening in the window.
- **L5, non-destructive negative checks, as the loader:**
  - `bigquery.tables.testIamPermissions` on the 3 tables must return exactly `get`, `getData` and `updateData`. `update`, `delete`, `setIamPolicy` and `export` must be absent.
  - The same call on a mart table returns nothing.
  - `projects.testIamPermissions` must return only `bigquery.jobs.create`.
  - `gcloud storage ls gs://<bucket>` must be denied.
  - **Object-scope probe** (decision needed): a zero-byte create outside the packet prefix, expected to be denied.
    - If it is wrongly allowed, it leaves a stray object that the loader cannot delete; it would be removed at Gate D.
    - The alternative is Policy Troubleshooter, but that needs a separately approved API enablement.
- **L6 (optional), table-condition enforcement:** mint a loader token shortly before expiry and call `tabledata.list` after expiry. Expected: denied.
- **L7, revoke:**
  - plan → `--mode loader-revoke` (6 deletes) → apply → back up.
  - Read back: no loader bindings on the project, the tables or the loader account. Impersonation is denied.
  - Match the `GenerateAccessToken` audit entries to the loader's recorded `uniqueId`, which closes the M6T audit-identity limitation for this window.

## Residual risks

1. **Job type cannot be restricted by IAM.** `bigquery.jobs.create` plus `getData` and `updateData` on the three tables also allows **query and DML jobs** on them during the window (billed; could modify rows). Mitigations:
   - a short window;
   - `WRITE_EMPTY` loads;
   - full post-load reconciliation, which would expose any modification;
   - audit logs;
   - an optional per-user query quota (outside Terraform; not configured).
2. **BigQuery table condition support is unverified** (Q1/Q2). If the API accepts but does not enforce the condition, the table grants rely on impersonation expiry and revocation (risk 4).
3. **Cross-root IAM.** The bootstrap root manages additive members on tables owned by the pilot root. They do not conflict, but a reviewer must look at both roots for each table's full access.
4. **Token lifetime:** up to 1 h past expiry for tokens minted inside the window.
5. **Bucket name in bootstrap state and plans** (custody only). The checker never prints it. The operator must pass the same custody value to `--bucket` and to the variable.
6. **Dormant loader identity after M7D.** It has no grants, but it is durable (`prevent_destroy`), like the provisioner. Removing it requires a reviewed change. Deleted custom role IDs cannot be reused for 37 days.
7. **Modelled plan shapes.** The checker models expected plan JSON. Real provider representations, such as a refreshed fully qualified `table_id` (accepted), may differ and will **fail closed**.
8. **Carried over:** the unrestricted project Owner (P8), the personal-account dataset owner, budget alerts that are not caps, and wrapper bypass by a determined operator.

## Readiness for M7C2

**Ready:**
- the identity and window design;
- the Terraform (disabled);
- the three checker modes with adversarial and mutation coverage;
- the live gates L0–L7 defined.

**M7C2 still needs:**
- G6, pilot data-metadata drift tolerance after loads;
- G3, a command bundle that consumes the M7B packet, binds the custody bucket and runs these modes;
- G4, verifiers for captured object and job JSON;
- G5, a `tabledata.list` converter and inputs-only reconciliation;
- G7, the Gate C rewrite (the runbook still describes provisioner-based loading);
- the M7D runbook, including L0–L7.

**Decisions needed:**
- the L5 object-scope probe (stray-object risk) versus enabling Policy Troubleshooter;
- the window length, allowing for the 1 h token tail.

**Stopped after M7C1 offline implementation.**
