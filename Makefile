.PHONY: install seed run test lint evals docker clean

install:
	pip install -r requirements-dev.txt

seed:
	python -m app.seed

run: seed
	python -m uvicorn app.main:app --reload --port 8000

test:
	python -m pytest -q


lint:
	ruff check app tests evals

# Requires the service to be running on --base-url.
evals:
	python -m evals.run_evals --base-url http://localhost:8000 --json evals/report.json

docker:
	docker compose up --build

clean:
	rm -f kyc_agent.db kyc_agent.db-wal kyc_agent.db-shm
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
