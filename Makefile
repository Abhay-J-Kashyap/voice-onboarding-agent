.PHONY: install seed run run-email test lint evals check docker clean

install:
	pip install -r requirements-dev.txt

# Apply all outstanding migrations. This is the only supported way to change a
# deployed schema; init_db exists for tests and scratch databases only.
migrate:
	alembic upgrade head

# Generate a migration after changing models. Always read the generated file
# before committing it — autogenerate is a good first draft, not an oracle.
migration:
	@test -n "$(m)" || (echo 'usage: make migration m="what changed"'; exit 1)
	alembic revision --autogenerate -m "$(m)"

seed: migrate
	python -m app.seed

# Demo mode is on locally so the passcode appears in the tool response and the
# full evaluation suite can run. Never enable it in a deployed service.
run: seed
	OTP_DEMO_MODE=true uvicorn app.main:app --reload --port 8000

# The other delivery channel. CI runs the evaluation suite against both, so it
# is worth being able to reproduce either locally.
run-email: seed
	OTP_DEMO_MODE=true OTP_DELIVERY_CHANNEL=email uvicorn app.main:app --reload --port 8000

test:
	pytest -q

lint:
	ruff check app tests evals

# Requires the service to be running on --base-url.
evals:
	python -m evals.run_evals --base-url http://localhost:8000 --json evals/report.json

# What CI runs, minus the service lifecycle. Use before pushing.
check: lint test

docker:
	docker compose up --build

clean:
	rm -f kyc_agent.db kyc_agent.db-wal kyc_agent.db-shm
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
