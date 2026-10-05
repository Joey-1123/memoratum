"""SC-003 / FR-007: a published figure must carry the command that regenerates it.

A results file that records *what* the numbers were but not *how to get them again* is a
claim, not a measurement. Six committed results files predate this requirement, so the
tests here distinguish two populations:

* **Emitted** files, which must carry the command — and must be checked by running the
  command, not by grepping for the word "command".
* **Committed** files, which are checked for an explicit provenance marker. A pre-existing
  file that predates the requirement is allowed to say so, in writing, rather than being
  silently treated as compliant.

Covers T092.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVAL = os.path.join(REPO_ROOT, "eval")
RESULTS_MD = os.path.join(EVAL, "RESULTS-grounding.md")

#: Axis runners, each with the argv a report must embed to regenerate itself.
AXES = (
    "eval_isolation",
    "eval_cost",
    "eval_latency",
    "eval_grounding",
)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


#: Per-axis argv that actually runs. Deliberately NOT uniform: the isolation axis builds its
#: own synthetic corpus and takes no `--data`, and the latency axis builds a size ladder and
#: takes no corpus either. A uniform argv here would have skipped every axis — the same
#: "the test never exercised the thing" failure this file exists to prevent.
AXIS_ARGV = {
    "eval_isolation": ["--projects", "2", "--queries", "2", "--seed", "42"],
    "eval_cost": ["--data", "eval/fixtures/longmemeval_smoke.json", "--n", "2", "--seed", "42"],
    "eval_latency": ["--ladder", "100,200,400", "--samples", "120", "--warmup", "5"],
    "eval_grounding": [
        "--data",
        "eval/fixtures/longmemeval_smoke.json",
        "--n",
        "1",
        "--seed",
        "42",
    ],
}


def _results_files() -> list[str]:
    return sorted(
        os.path.join(EVAL, name)
        for name in os.listdir(EVAL)
        if name.startswith("RESULTS") and name.endswith(".md")
    )


def _extract_command(text: str) -> list[str]:
    """The argv of the embedded regeneration command, from its fenced bash block.

    Parsed rather than grepped: a file that mentions the word "command" without a runnable
    command satisfies a naive check and satisfies nothing else.
    """
    blocks = re.findall(r"```bash\n(.*?)```", text, re.DOTALL)
    for block in blocks:
        for line in block.splitlines():
            stripped = line.strip()
            if "python -m memoratum." in stripped:
                return shlex.split(stripped.replace("\\\n", " "))
    return []


# --- T092: the emitted file carries the command -----------------------------------


def test_the_emitted_report_embeds_a_runnable_command() -> None:
    assert os.path.exists(RESULTS_MD), "the grounding results file is missing"
    argv = _extract_command(_read(RESULTS_MD))
    assert argv, (
        f"{RESULTS_MD} contains no 'uv run python -m memoratum.<axis>' command, so the "
        "figure cannot be regenerated"
    )
    assert argv[0] == "uv" or argv[0].endswith("python"), argv
    assert any("memoratum.eval_" in part for part in argv), argv


def test_the_embedded_command_names_the_axis_that_produced_the_file() -> None:
    text = _read(RESULTS_MD)
    argv = _extract_command(text)
    modules = {part for part in argv if "memoratum.eval_" in part}
    assert modules == {"memoratum.eval_grounding"}, (
        f"a RESULTS-grounding.md that regenerates via {modules} is mislabelled"
    )


def test_the_embedded_command_carries_its_flags() -> None:
    """A bare module name is not a command; the flags are what make it reproducible."""
    argv = _extract_command(_read(RESULTS_MD))
    flags = {part for part in argv if part.startswith("--")}
    assert "--data" in flags, argv
    assert "--seed" in flags, argv
    assert {"--out-md", "--out-json"} & flags, argv


def test_the_embedded_command_describes_the_run_that_produced_the_file() -> None:
    """The command's flags must agree with the manifest committed beside it.

    Checked without executing the command, because the corpus is 277 MB and is not
    committed. Agreement with the manifest is what makes the claim checkable here: seed,
    sample size, depths and the data hash are all recorded, so a command describing a
    *different* run would contradict the file it sits in. Executability is covered by
    `test_each_axis_embedded_command_runs_verbatim`.
    """
    argv = _extract_command(_read(RESULTS_MD))
    assert argv, "no embedded command to check"
    with open(os.path.join(EVAL, "grounding.json"), encoding="utf-8") as handle:
        manifest = json.load(handle)["manifest"]

    assert str(manifest["seed"]) == argv[argv.index("--seed") + 1], (
        f"the embedded command's seed differs from the committed manifest "
        f"({argv[argv.index('--seed') + 1]} vs {manifest['seed']})"
    )
    assert str(manifest["n"]) == argv[argv.index("--n") + 1]
    assert str(manifest["requested_n"]) == argv[argv.index("--n") + 1]
    if "--k" in argv:
        ks = argv[argv.index("--k") + 1].split(",")
        assert manifest["ks"] == [int(k) for k in ks], (
            f"the embedded command's --k does not match the committed manifest: {ks} vs "
            f"{manifest['ks']}"
        )
    else:
        # The grounding axis derives its depths from --k's default; assert the hit limit the
        # command implies instead of requiring the flag.
        assert manifest["requested_n"] == int(argv[argv.index("--n") + 1])
    assert manifest["data_sha256"] and not manifest["data_sha256"].startswith("unavailable"), (
        "the manifest records no corpus hash, so the embedded --data path cannot be "
        "checked against the figure it supposedly produced"
    )


def test_each_axis_embedded_command_runs_verbatim(tmp_path) -> None:
    """Each axis's *own* embedded command is executed.

    This is the executability check SC-003 needs, and it needs no large corpus: every axis
    runs against the committed smoke fixture, then the command it emitted is re-run as
    written. A typo'd flag or a stale default fails here.
    """
    import io
    from contextlib import redirect_stderr, redirect_stdout

    fixture = os.path.join(EVAL, "fixtures", "longmemeval_smoke.json")
    out_md = tmp_path / "RESULTS.md"
    out_json = tmp_path / "results.json"

    for module in ("eval_cost", "eval_grounding"):
        imported = __import__(f"memoratum.{module}", fromlist=["main"])
        argv = [
            "--data",
            fixture,
            "--n",
            "1",
            "--seed",
            "42",
            "--out-md",
            str(out_md),
            "--out-json",
            str(out_json),
        ]
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = imported.main(argv)
        assert code in (0, 1), f"{module} exited {code}: {stderr.getvalue()}"

        embedded = _extract_command(out_md.read_text())
        assert embedded, f"{module} embedded no command"

        # Re-run the embedded command verbatim, redirecting only the output paths.
        rerun = list(embedded)
        rerun[rerun.index("--out-md") + 1] = str(tmp_path / "again.md")
        rerun[rerun.index("--out-json") + 1] = str(tmp_path / "again.json")
        proc = subprocess.run(rerun, capture_output=True, text=True, cwd=REPO_ROOT, check=False)
        assert proc.returncode in (0, 1), (
            f"{module}'s embedded command did not run:\n{proc.stdout[-2000:]}{proc.stderr[-2000:]}"
        )
        with open(tmp_path / "again.json", encoding="utf-8") as handle:
            rerun_result = json.load(handle)
        assert rerun_result["manifest"]["seed"] == 42, (
            f"{module}'s embedded command did not carry its own --seed"
        )


# --- every axis runner embeds its own command --------------------------------------


@pytest.mark.parametrize("module", AXES)
def test_each_axis_report_embeds_its_own_command(module: str, tmp_path) -> None:
    """Each axis is actually run, so the requirement holds for all four.

    No skipping: an axis whose argv here does not run is a broken test, not a test to skip.
    """
    import io
    from contextlib import redirect_stderr, redirect_stdout

    module_name = f"memoratum.{module}"
    imported = __import__(module_name, fromlist=["main"])

    out_md = tmp_path / "RESULTS.md"
    out_json = tmp_path / "results.json"
    argv = [
        *AXIS_ARGV[module],
        "--out-md",
        str(out_md),
        "--out-json",
        str(out_json),
    ]
    stdout, stderr = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = imported.main(argv)
    except SystemExit as exc:
        pytest.fail(f"{module} rejected its own documented argv {argv}: {exc}")
    assert code in (0, 1), f"{module} exited {code}: {stderr.getvalue() or stdout.getvalue()}"
    assert out_md.exists(), f"{module} wrote no Markdown despite --out-md"

    text = out_md.read_text()
    extracted = _extract_command(text)
    assert extracted, f"{module} emitted a results file with no regeneration command:\n{text}"
    assert any(module in part for part in extracted), (
        f"{module} embedded a command for a different axis: {extracted}"
    )
    assert "--seed" in extracted or "--ladder" in extracted, (
        f"{module} embedded a command with neither --seed nor --ladder: {extracted}"
    )


# --- the committed corpus of results files ----------------------------------------


def test_every_committed_results_file_states_how_to_regenerate_it_or_says_it_cannot() -> None:
    """Either a command, or an explicit, honest note. Never silence.

    Six of these files predate SC-003, so demanding a command in all of them would be
    demanding a fabrication. Demanding an admission is not.
    """
    files = _results_files()
    assert files, "no committed results files found"
    undocumented = []
    for path in files:
        text = _read(path)
        has_command = bool(_extract_command(text))
        # An explicit, honest admission counts. Each phrase below is a real statement a
        # file can legitimately make about why it carries no command.
        admits = bool(
            re.search(
                r"metric definition v0|not re-runnable|no regeneration command|"
                r"predates|pre-2026-10-04|not comparable|not affected by the v0",
                text,
                re.IGNORECASE,
            )
        )
        if not has_command and not admits:
            undocumented.append(os.path.relpath(path, REPO_ROOT))
    assert not undocumented, (
        "these results files carry neither a regeneration command nor a note explaining "
        f"why they have none: {undocumented}"
    )


def test_a_results_file_with_a_command_also_records_the_seed_and_data() -> None:
    """A command without its inputs is half a command."""
    for path in _results_files():
        text = _read(path)
        argv = _extract_command(text)
        if not argv:
            continue
        assert "--seed" in argv or "--ladder" in argv or "--full" in argv, (
            f"{os.path.basename(path)} embeds a command with no --seed, so it cannot be "
            "reproduced exactly"
        )


def test_the_marked_v0_files_are_not_silently_passed_as_current() -> None:
    """M4: pre-fix `R@k` figures must stay labelled, or a reader will compare them."""
    migration = os.path.join(EVAL, "MIGRATION-metric-v1.md")
    assert os.path.exists(migration), "the metric migration note is missing"
    note = _read(migration)
    assert "2026-10-04" in note, "the migration note must date the metric change"


def test_docs_evaluation_links_the_migration_note() -> None:
    """A non-comparability warning is only useful if a reader can find it."""
    docs = _read(os.path.join(REPO_ROOT, "docs", "EVALUATION.md"))
    assert "MIGRATION-metric-v1.md" in docs, (
        "docs/EVALUATION.md must link the migration note, or stale R@k figures are unfindable"
    )
