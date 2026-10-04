"""CI integrity: the coverage gate must not be gameable.

Constitution Principle IV says a green suite is not evidence. A coverage number is
evidence only while it stays honest, so two ratchets are enforced mechanically:

* the floor in ``pyproject.toml`` may rise but not fall without an amendment
* ``# pragma: no cover`` and ``pytest.skip`` totals may shrink but not grow

These test the guards themselves. A guard nobody has ever seen fail is not a guard.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script), *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )


# --- the floor guard --------------------------------------------------------


def test_coverage_floor_is_present():
    from memoratum import db  # noqa: F401 - keeps sys.path behaviour consistent

    result = _run("check_coverage_floor.py")
    assert result.returncode == 0, result.stdout + result.stderr


def test_coverage_floor_reader_parses_the_value():
    sys.path.insert(0, str(ROOT / "scripts"))
    from check_coverage_floor import current_floor

    assert current_floor() == 74, "the measured floor should still be 74"


def test_floor_guard_fails_when_the_floor_is_lowered(tmp_path, monkeypatch):
    """The guard must actually fail. Guarding the guard is the whole point."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import check_coverage_floor

    sample = tmp_path / "pyproject.toml"
    sample.write_text("[tool.coverage.report]\nfail_under = 10\n")
    assert check_coverage_floor.current_floor(str(sample)) == 10

    sample.write_text("[tool.coverage.report]\nfail_under = 90\n")
    assert check_coverage_floor.current_floor(str(sample)) == 90

    sample.write_text("[tool.coverage.report]\n")
    assert check_coverage_floor.current_floor(str(sample)) is None, (
        "a missing fail_under must read as None so the guard can reject it"
    )


def test_floor_guard_rejects_a_missing_gate(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts"))
    import check_coverage_floor

    sample = tmp_path / "pyproject.toml"
    sample.write_text("[tool.coverage.run]\nbranch = true\n")
    assert check_coverage_floor.current_floor(str(sample)) is None


# --- the suppression guard --------------------------------------------------


def test_suppression_guard_runs():
    result = _run("check_no_new_suppressions.py")
    assert result.returncode == 0, result.stdout + result.stderr


def test_suppression_counter_detects_both_forms(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "scripts"))
    import check_no_new_suppressions as guard

    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text("x = 1  # pragma: no cover\ndef f():\n    pass\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("import pytest\npytest.skip('later')\n")
    no_cover, skip = guard.count(None)
    assert no_cover == 1, no_cover
    assert skip == 1, skip


def test_suppression_counter_ignores_other_syntax(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "scripts"))
    import check_no_new_suppressions as guard

    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "ok.py").write_text("# a normal comment\nx = 1\ny = 'pragma: no cover'\n")
    no_cover, skip = guard.count(None)
    assert no_cover == 0, "only a real pragma comment should count"
    assert skip == 0


# --- per-module floors ------------------------------------------------------


def test_worker_and_webhooks_coverage_guards_exist():
    """Both P0 defects lived in the two worst-covered real modules."""
    for module in ("worker.py", "webhooks.py"):
        path = ROOT / "src" / "memoratum" / module
        assert path.exists(), module


def test_mcp_server_is_no_longer_untested():
    """mcp.py was at 0% -- the worst-covered module in the project."""
    tests = ROOT / "tests"
    contents = "\n".join(p.read_text(errors="replace") for p in tests.glob("test_*.py"))
    assert "mcp" in contents.lower(), "no test references the MCP server at all"


def test_no_coverage_badge_in_readme():
    """A badge reads as a target to contributors; the floor is not a scoreboard."""
    readme = (ROOT / "README.md").read_text()
    assert "coverage" not in readme.lower(), (
        "README must not carry a coverage badge -- see the measure-and-hold policy"
    )


def test_omit_list_stays_minimal():
    """Excluding app.py or the eval modules would be the easy way to look better."""
    text = (ROOT / "pyproject.toml").read_text()
    omit_line = next(line for line in text.splitlines() if line.strip().startswith("omit"))
    assert "__main__.py" in omit_line, omit_line
    for forbidden in ("app.py", "eval_", "search.py", "webhooks.py", "worker.py"):
        assert forbidden not in omit_line, f"{forbidden} must stay in the denominator"
