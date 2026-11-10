.PHONY: lint test check gcp-offline-check

lint:
	ruff check .

test:
	pytest

check: lint test

# Offline only. No init, downloads, plan, authentication or cloud API calls.
gcp-offline-check:
	python3 -m pytest -q tests/test_gcp_readiness.py tests/test_gcp_reconciliation.py tests/test_gcp_iam_separation.py tests/test_state_custody.py tests/test_terraform_custody.py tests/test_state_backup.py tests/test_bootstrap_plan_check.py
	@if command -v terraform >/dev/null 2>&1; then terraform -chdir=infrastructure/terraform fmt -check -recursive; else echo "Terraform unavailable: formatting check skipped"; fi

