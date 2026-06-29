"""Meta-test: every dependency in requirements.txt is pinned to an exact version."""

import pathlib
import re

REQ = pathlib.Path(__file__).resolve().parent.parent / "requirements.txt"

EXPECTED_PINS = {
    "numpy": "2.4.6",
    "pandas": "3.0.4",
    "python-dateutil": "2.9.0.post0",
    "six": "1.16.0",
    "pytest": "9.1.1",
    "hypothesis": "6.155.7",
    "freezegun": "1.5.5",
}

_PIN_RE = re.compile(r"^(?P<name>[A-Za-z0-9_.\-]+)==(?P<version>[A-Za-z0-9_.\-]+)\s*$")


def _parse_pins():
    pins = {}
    for raw in REQ.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = _PIN_RE.match(line)
        assert m, f"requirement is not an exact pin: {raw!r}"
        pins[m.group("name").lower()] = m.group("version")
    return pins


def test_requirements_file_exists():
    assert REQ.exists()


def test_every_line_is_exact_pin():
    pins = _parse_pins()
    assert pins, "requirements.txt has no requirements"


def test_expected_packages_present_and_pinned():
    pins = _parse_pins()
    for name, version in EXPECTED_PINS.items():
        assert name in pins, f"{name} missing from requirements.txt"
        assert pins[name] == version, f"{name} pinned to {pins[name]}, expected {version}"
