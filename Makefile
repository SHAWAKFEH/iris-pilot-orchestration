PY ?= python

.PHONY: db-up db-down install init pilot status test demo

db-up:        ## start local PostGIS (Docker) and wait until healthy
	docker compose up -d --wait

db-down:
	docker compose down -v

install:
	$(PY) -m pip install -e ".[dev]"

init:         ## apply migrations + load the deterministic fixture set
	$(PY) -m iris_pilot init

pilot:        ## THE one command: promote -> refresh views -> export dossiers -> manifest
	$(PY) -m iris_pilot run

status:       ## show fresh/stale state of views and exports
	$(PY) -m iris_pilot status

test:         ## unit tests always; integration tests when IRIS_TEST_DATABASE_URL is set
	$(PY) -m pytest

demo: db-up init pilot
