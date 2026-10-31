# ADR-003: Separated IAM bootstrap and resource-only provisioner

Status: accepted for offline implementation (M6K, 2026-10-31). Nothing bootstrapped, planned or applied.

Context: [M6J](../upgrade/m6j_review.md) found that the provisioner candidate could escalate. With `iam.roles.create/update` it could rewrite its own custom role, and unconditioned `resourcemanager.projects.setIamPolicy` let it grant itself any project role. Conditions cannot constrain a principal that can edit its own role. The user rejected accepting that risk for a temporary window (Option B) and approved Option A.

Decision:
- Administrative IAM lives in a **separate Terraform root** (`infrastructure/terraform/bootstrap/`) with its own state. It is applied only by the bootstrap administrator (currently the sole project Owner), using a short-lived session token. **It never impersonates the provisioner.**
- The bootstrap root owns:
  - the runner and provisioner service accounts (keyless, prevent-destroy);
  - the five runtime custom roles and the provision and impersonate roles;
  - the runner's project jobs membership;
  - the provisioner's project grant and the operator's account-scoped token-minting grant, both disabled by default and expiring at most 8 hours after plan time;
  - IAM Credentials audit logging.
- It uses only additive `*_iam_member` resources, never authoritative project policy or binding resources. The existing Owner binding is untouched.
- The pilot root keeps resource provisioning: the bucket, dataset, tables and their resource IAM. It reads the runner, the runtime roles, the provision role and the project policy, and fails at plan time on drift or escalation.
- Role definitions have one source of truth in `infrastructure/iam/*.json`, consumed by both roots.

Consequences:
- The provisioner's escalation is limited to data-plane IAM on pilot Storage and BigQuery resources (documented; not narrowed further, because condition support is unverified).
- Bootstrap adds a second state file, with the same custody requirements, and a second approval gate.
- Pilot instances fall from 25 to 18. Bootstrap adds 11 instances, or 13 with access enabled.
- The Owner remains unrestricted. Separation limits the provisioner token's blast radius, not the human.
- A dedicated non-Owner bootstrap-admin service account was not introduced: while the sole operator is Owner it would add an identity without reducing effective risk.
- Revoking access does not invalidate tokens already issued; they remain valid for up to 1 hour.
- Gate D identity and role removal moves to the bootstrap root.
