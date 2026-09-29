# Convenience targets. Every target wraps a plain command documented in README.md,
# so nothing here is required: run the commands directly if `make` is unavailable.
UV ?= uv
RUN := $(UV) run

.PHONY: install lint format typecheck test cov check

install:
	$(UV) sync --extra dev

lint:
	$(RUN) ruff check .

format:
	$(RUN) ruff format .

typecheck:
	$(RUN) mypy

test:
	$(RUN) pytest

cov:
	$(RUN) pytest --cov --cov-report=term --cov-report=json
	$(RUN) python scripts/check_coverage.py coverage.json

# Full local quality gate (what CI runs).
check: lint
	$(RUN) ruff format --check .
	$(RUN) mypy
	$(MAKE) cov
