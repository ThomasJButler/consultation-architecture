# The gates. `make check` is what CI runs; each target runs on its own too.
# The venv's Python is used when it exists, otherwise whatever `python` is
# on the path (CI installs into the runner's interpreter).
PY ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python)

.PHONY: check lint type test test-slow test-pure audit venv init reset fixtures validate ingest

check: lint type test test-slow audit

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

type:
	$(PY) -m mypy

# The coverage run deselects the `slow` tests: the plan benchmark's ingest of
# 20,000 rows took 231 s under coverage against 37 s without it (measured
# 26 September 2026 on Postgres 16), and coverage is 95% without them.
test:
	$(PY) -m pytest -m 'not slow' --cov=consult --cov-report=term-missing

# The `slow` tests (the exactly-once race, the plan benchmark) on their own,
# without coverage, so `make check` still runs every test.
test-slow:
	$(PY) -m pytest -m slow

# The pure-function tests, no database needed (pytest.ini marks the rest `db`).
test-pure:
	$(PY) -m pytest -m 'not db'

audit:
	$(PY) -m pip_audit --strict
	$(PY) -m bandit -q -r consult

venv:
	python3.12 -m venv .venv
	.venv/bin/python -m pip install --upgrade pip
	.venv/bin/python -m pip install -e '.[dev]'

init:
	$(PY) -m consult.cli init

reset:
	$(PY) -m consult.cli init --reset

fixtures:
	$(PY) scripts/make_fixture_data.py

validate:
	$(PY) -m consult.cli validate tests/fixtures/responses.csv --definition tests/fixtures/definition.xlsx

ingest:
	$(PY) -m consult.cli ingest tests/fixtures/responses.csv --definition tests/fixtures/definition.xlsx \
		--name "Riverside cycle route" --department "Department of Fictional Affairs"
