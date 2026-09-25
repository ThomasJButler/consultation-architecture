"""Rules about the tests themselves, checked by a test so they hold without
anyone having to remember them.

One rule so far. `pytest -m 'not db'` has to run on a machine with no
Postgres, so a test module that needs one says so at the top with
`pytestmark = pytest.mark.db`, and a module that says so needs one. What
"needs one" means here: it imports psycopg at module level, or one of its
tests asks for a database fixture by name.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
DB_FIXTURES = {"db", "db_settings", "blank_database"}


def _module_marks_db(tree: ast.Module) -> bool:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "pytestmark" for target in node.targets
        ):
            return ast.unparse(node.value) == "pytest.mark.db"
    return False


def _imports_psycopg_at_module_level(tree: ast.Module) -> bool:
    for node in tree.body:
        if isinstance(node, ast.Import) and any(
            a.name.split(".")[0] == "psycopg" for a in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "psycopg":
            return True
    return False


def _asks_for_a_db_fixture(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            names = {arg.arg for arg in node.args.args + node.args.kwonlyargs}
            if names & DB_FIXTURES:
                return True
    return False


def test_pure_tests_run_without_a_database() -> None:
    modules = sorted(TESTS_DIR.glob("test_*.py"))
    assert modules, "no test modules found"
    for path in modules:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        needs_db = _imports_psycopg_at_module_level(tree) or _asks_for_a_db_fixture(tree)
        marked = _module_marks_db(tree)
        assert needs_db == marked, (
            f"{path.name}: {'needs a database but is not marked db' if needs_db else 'is marked db but needs no database'}"
        )


def test_advance_consultation_is_the_only_writer_of_consultation_status() -> None:
    # docs/02 section 6: there is no second way to change the column, which
    # is what stops a reopen racing a worker's fan-in. transitions.py holds
    # the routine and the reopen; nothing else may UPDATE consultation.
    consult_dir = TESTS_DIR.parent / "consult"
    for path in sorted(consult_dir.glob("*.py")):
        if path.name == "transitions.py":
            continue
        source = path.read_text(encoding="utf-8").lower()
        assert "update consultation" not in source, f"{path.name} writes consultation"
