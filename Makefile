PY ?= python3

.PHONY: test typecheck check bench serve docker-build clean

test:
	$(PY) -m pytest

typecheck:
	$(PY) -m mypy triage

check: typecheck test   ## what CI runs

bench:
	$(PY) -m triage.benchmark

serve:   ## local server on 127.0.0.1:8080 with the offline mock model
	$(PY) -m triage.server

docker-build:
	docker build -t agentic-incident-triage .

clean:
	find . -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .mypy_cache
