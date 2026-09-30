# Convenience targets. Every target wraps a plain command documented in README.md,
# so nothing here is required: run the commands directly if `make` is unavailable.
UV ?= uv
RUN := $(UV) run
DATA ?= data/sample

.PHONY: install lint format typecheck test cov check demo analyze simulate serve dashboard \
	benchmark docker-build docker-up

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

demo:
	$(RUN) execlens demo

analyze:
	$(RUN) execlens analyze --data $(DATA)

simulate:
	$(RUN) execlens simulate --scenario all --trials 20

serve:
	$(RUN) execlens serve --data $(DATA)

dashboard:
	$(RUN) execlens dashboard --data $(DATA)

# Full benchmark (>1M market rows, >100k fills) + API latency; updates the docs.
benchmark:
	$(RUN) execlens benchmark

docker-build:
	docker build -t execlens:latest .

docker-up:
	docker compose up --build
