#!/usr/bin/env bash
# M8 runner window: READ-ONLY live IAM capture, for scripts/runner_window_verify.py iam.
# Operator only, in Terminal.app, never in Claude Code. Every API call is a GET or a
# getIamPolicy read to fixed https://*.googleapis.com endpoints, plus ONE runner token-mint
# attempt whose token (if any) is discarded unread. No setIamPolicy, no Terraform plan/apply,
# no state change. Output stays in custody ($C/m8/runner-<phase>-<UTC>/, 0700/0600).
# Usage (repository root, approved commit):  bash scripts/runner_window_collect.sh window|revoked
set -euo pipefail
umask 077
unset CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT CLOUDSDK_AUTH_ACCESS_TOKEN_FILE \
  GOOGLE_OAUTH_ACCESS_TOKEN GOOGLE_APPLICATION_CREDENTIALS
PROJECT="ebank-pilot-dev-261031-k7m4"
DATASET="bankpilot_dev"
RUNNER="bankpilot-dev-runner@${PROJECT}.iam.gserviceaccount.com"
LOADER="bankpilot-dev-loader@${PROJECT}.iam.gserviceaccount.com"
PROVISIONER="bankpilot-dev-provisioner@${PROJECT}.iam.gserviceaccount.com"
TABLES=(customers accounts curated_transactions transaction_daily customer_daily account_daily operations_daily transaction_risk_scores)  # M9D: score table
C="$HOME/.secure/${PROJECT}"
PHASE="${1:-}"
case "$PHASE" in window|revoked) ;; *) echo "usage: bash $0 window|revoked" >&2; exit 2 ;; esac
[[ -d "$C" ]] || { echo "custody directory missing" >&2; exit 2; }
[[ -f scripts/terraform_custody.py ]] || { echo "run from the repository root" >&2; exit 2; }
OUT="$C/m8/runner-${PHASE}-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -m 700 -p "$C/m8"
mkdir -m 700 "$OUT"
# The active gcloud account must be the approved operator (compared, never printed).
OPERATOR="$(sed -n 's/^operator_member *= *"\(.*\)"$/\1/p' "$C/inputs/bootstrap-dev.tfvars")"
ACCOUNT="$(gcloud config get-value account 2>/dev/null)"
[[ -n "$OPERATOR" && "user:$ACCOUNT" == "$OPERATOR" ]] \
  || { echo "gcloud account is not the approved operator" >&2; exit 2; }
unset OPERATOR ACCOUNT
TOKEN="$(gcloud auth print-access-token)"   # operator identity; never written to disk
CURL=(curl -q -sS --fail-with-body --proto =https --max-time 60)
get() { "${CURL[@]}" -H @<(printf 'Authorization: Bearer %s\n' "$TOKEN") "$1" > "$2"; }
pol() { "${CURL[@]}" -X POST -H 'Content-Type: application/json' \
          -H @<(printf 'Authorization: Bearer %s\n' "$TOKEN") \
          -d '{"options":{"requestedPolicyVersion":3}}' "$1" > "$2"; }
sapol() { "${CURL[@]}" -X POST -H @<(printf 'Authorization: Bearer %s\n' "$TOKEN") -d '' \
          "$IAM/serviceAccounts/$1:getIamPolicy?options.requestedPolicyVersion=3" > "$2"; }
IAM="https://iam.googleapis.com/v1/projects/${PROJECT}"
BQ="https://bigquery.googleapis.com/bigquery/v2/projects/${PROJECT}/datasets/${DATASET}/tables"
pol "https://cloudresourcemanager.googleapis.com/v1/projects/${PROJECT}:getIamPolicy" \
  "$OUT/project_policy.json"
sapol "$RUNNER" "$OUT/runner_sa_policy.json"
sapol "$LOADER" "$OUT/loader_sa_policy.json"
sapol "$PROVISIONER" "$OUT/provisioner_sa_policy.json"
get "${IAM}/serviceAccounts/${RUNNER}" "$OUT/runner_sa.json"
get "${IAM}/serviceAccounts/${RUNNER}/keys?keyTypes=USER_MANAGED" "$OUT/runner_sa_keys.json"
for t in "${TABLES[@]}"; do pol "${BQ}/${t}:getIamPolicy" "$OUT/table_${t}_policy.json"; done
for r in jobs metadata objects readers tables; do
  get "${IAM}/roles/${DATASET}_${r}" "$OUT/role_${r}.json"
done
unset TOKEN
# Live check: the operator mints a runner token. Window: must succeed (token discarded, never
# stored). Revoked: must fail with PERMISSION_DENIED (the attempt is audit-logged).
rc=0
gcloud auth print-access-token --impersonate-service-account="$RUNNER" \
  > /dev/null 2> "$OUT/runner_mint.err" || rc=$?
echo "$rc" > "$OUT/runner_mint.rc"
# Bootstrap state through the custody wrapper only (no plan, no refresh, no state change).
rc=0
env -i HOME="$HOME" PATH="$PATH" python3 scripts/terraform_custody.py run --root bootstrap -- \
  state list > "$OUT/state_list.txt" 2> "$OUT/state_list.err" || rc=$?
echo "$rc" > "$OUT/state_list.rc"
chmod 600 "$OUT"/*
echo "captured: $OUT"
echo "evaluate: python3 -I scripts/runner_window_verify.py iam --phase $PHASE --capture \"$OUT\" \\"
echo "  --bootstrap-tfvars \"$C/inputs/bootstrap-dev.tfvars\"$([[ $PHASE == window ]] && echo ' --expiry "$EXP"')"
