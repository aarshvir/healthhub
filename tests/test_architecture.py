"""Enforcement: prove that every *other* module routes through `integrity`.

This is the build-failing guarantee behind "every other module MUST route through these".
It statically scans every application module (any top-level ``*.py`` that is not
`integrity.py`, `_rules.py`, or a test) and fails if it bypasses the trust boundary by:

  * reading the wall clock directly (``.now`` / ``.utcnow``)  -> must use ``integrity.freshness`` / ``integrity.now_utc``
  * coercing values itself (``pandas.to_numeric``)             -> must use ``integrity.validate_readings``
  * re-defining the single-source-of-truth primitives          -> ranges / tokenizer live only in integrity/_rules
  * importing the private ``_rules`` module or private integrity symbols, or ``dateutil`` directly

Plus a runtime tripwire and a frozen-public-surface check.
"""

import ast
import pathlib

import pytest

import integrity

ROOT = pathlib.Path(__file__).resolve().parent.parent
ALLOWED_RAW = {"integrity.py", "_rules.py"}
FORBIDDEN_DEFS = {"_RANGES", "RANGES", "RANGE_LIMITS", "NUMBER_RE", "_NUM", "_DATELIKE", "YEAR_RE"}
SENSITIVE_ATTRS = {"now", "utcnow"}      # raw clock access
COERCION_ATTRS = {"to_numeric"}          # raw numeric coercion

EXPECTED_PUBLIC = {
    "FreshnessState", "Severity", "MetricRange", "RowError", "ValidationReport",
    "Freshness", "CacheEntry", "IntegrityError", "ValidationError",
    "NarrationIntegrityError", "CacheMiss", "validate_readings", "freshness",
    "cache_get", "cache_set", "cache_get_value", "cache_clear",
    "sanitize_narration", "assert_validated", "now_utc", "RANGES",
}


def _app_modules():
    out = []
    for path in sorted(ROOT.glob("*.py")):
        if path.name in ALLOWED_RAW or path.name.startswith("test_") or path.name == "conftest.py":
            continue
        out.append(path)
    return out


APP_MODULES = _app_modules()


def test_there_is_at_least_one_consumer_module():
    # the enforcement is meaningless if nothing actually routes through integrity
    names = {p.name for p in APP_MODULES}
    assert "clinical.py" in names
    assert "narration.py" in names


@pytest.mark.parametrize("path", APP_MODULES, ids=lambda p: p.name)
def test_no_module_bypasses_integrity(path):
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in SENSITIVE_ATTRS:
            raise AssertionError(f"{path.name}: direct clock access '.{node.attr}'; use integrity.freshness/now_utc")
        if isinstance(node, ast.Attribute) and node.attr in COERCION_ATTRS:
            raise AssertionError(f"{path.name}: raw '.{node.attr}'; coercion must go through integrity.validate_readings")
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name) and t.id in FORBIDDEN_DEFS:
                    raise AssertionError(f"{path.name}: redefines single-source-of-truth name '{t.id}'")


@pytest.mark.parametrize("path", APP_MODULES, ids=lambda p: p.name)
def test_no_private_or_dateutil_imports(path):
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "_rules" or alias.name.split(".")[0] == "dateutil":
                    raise AssertionError(f"{path.name}: imports '{alias.name}'; route through integrity")
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "_rules":
                raise AssertionError(f"{path.name}: imports private _rules; route through integrity")
            if mod.split(".")[0] == "dateutil":
                raise AssertionError(f"{path.name}: parses timestamps directly; use integrity.freshness")
            if mod == "integrity":
                for alias in node.names:
                    if alias.name == "*":
                        raise AssertionError(f"{path.name}: 'from integrity import *' would leak privates")
                    if alias.name.startswith("_"):
                        raise AssertionError(f"{path.name}: imports private integrity symbol '{alias.name}'")


def test_public_surface_is_frozen():
    assert set(integrity.__all__) == EXPECTED_PUBLIC
    # and everything advertised actually exists
    for name in integrity.__all__:
        assert hasattr(integrity, name), f"integrity.__all__ lists missing symbol {name!r}"


def test_violating_fixture_is_caught(tmp_path):
    bad = tmp_path / "bad_module.py"
    bad.write_text("import pandas as pd\n_RANGES = {'x': (0, 1)}\nx = pd.to_numeric([1])\n")
    tree = ast.parse(bad.read_text())
    flagged_coercion = any(
        isinstance(n, ast.Attribute) and n.attr in COERCION_ATTRS for n in ast.walk(tree)
    )
    flagged_def = any(
        isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in FORBIDDEN_DEFS for t in n.targets
        )
        for n in ast.walk(tree)
    )
    assert flagged_coercion and flagged_def


def test_runtime_tripwire_flips_when_guards_used():
    # exercising any public guard sets the _USED tripwire (catches imported-but-never-invoked)
    integrity.freshness(integrity.now_utc())
    assert integrity._USED is True
