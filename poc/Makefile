# The gates. `make check` is what CI runs; each target runs on its own too.
# The venv's Python is used when it exists, otherwise whatever `python` is
# on the path (CI installs into the runner's interpreter).
PY ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python)

.PHONY: check lint type test test-pure audit venv init reset fixtures

check: lint type test audit

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

type:
	$(PY) -m mypy

test:
	$(PY) -m pytest --cov=consult --cov-report=term-missing

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
