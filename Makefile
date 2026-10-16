# Developer gate — mirrors .github/workflows/ci.yml's Linux `test` job exactly, so
# `make gate` locally runs the same checks CI will, in the same order, failing fast.
# Commands are verbatim from ci.yml. No install steps: install dev deps first with
#   pip install -r requirements-dev.txt ruff
# (ruff is a dev tool, not a runtime dependency — the PTY backend stays stdlib-only).

.PHONY: gate lint test test-js

# The full gate: lint → Python tests → view-JS tests, stopping at the first failure.
gate: lint test test-js

# ci.yml "Lint": ruff check . && ruff format --check .
lint:
	ruff check .
	ruff format --check .

# ci.yml "Test (Python)"
test:
	pytest -q

# ci.yml "Test (view JS)"
test-js:
	node --test tests/js/*.test.mjs
