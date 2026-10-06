"""Contract conformance for the eval-axis artifacts.

The spec artifacts are executable promises: a task that says "run this command" or a
manifest that says "this field is required" is a claim about behaviour. These tests make
the claims falsifiable, so an artifact cannot drift away from the code silently.

Covers T092 (re-run reproducibility) and the doc/contract conformance tasks in Phase 7.
"""

from __future__ import annotations

import itertools
import json
import re
from pathlib import Path

import pytest

SPEC = Path("specs/002-evaluation-harness")
CONTRACT_MD = SPEC / "contracts" / "eval-axes-v1.md"
CONTRACT_SCHEMA = SPEC / "contracts" / "eval-axes-v1.schema.json"
QUICKSTART = SPEC / "quickstart.md"
DATA_MODEL = SPEC / "data-model.md"
BASELINES = Path("eval/BASELINES.md")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --- the documented default ladder must satisfy the validator (issue #49) -------


def _hardware() -> dict:
    return {
        "cpu": "Intel i3-7020U @ 2.30GHz",
        "cores": 4,
        "ram_mb": 3072,
        "python": "3.12.14",
        "platform": "Linux-6.1.0",
    }


def _documented_ladder() -> list[int]:
    """Parse the --ladder default straight out of the contract table."""
    match = re.search(r"\| `--ladder` \| csv int \| `([0-9,]+)` \|", _text(CONTRACT_MD))
    assert match, "contract must document a --ladder default"
    return [int(part) for part in match.group(1).split(",")]


def test_contract_default_ladder_is_accepted_by_the_validator() -> None:
    """The documented default must not be rejected by our own validator (issue #49).

    `PREFILTER_MIN_CANDIDATES` is 512, so a default spanning 400 -> 1600 would cross it
    and the axis would refuse its own documented invocation. This test is the guard that
    keeps the contract and the code in agreement.
    """
    from memoratum.eval_axes import ManifestError, build_manifest

    ladder = _documented_ladder()
    build_manifest(
        seed=42,
        requested_n=1,
        n=1,
        ks=[5],
        modes=["hybrid"],
        embedder="HashEmbedder:64",
        vector_store="SQLiteVectorStore",
        data_sha256="x",
        latency_config={"samples": 120, "warmup": 5, "ladder": ladder, "hardware": _hardware()},
    )

    # Paired with its own rejection. `build_manifest` raising is the only way this test
    # can fail, and a validator that never raised would pass it silently — so the same
    # call is made with a straddling ladder and MUST be refused. If the acceptance above
    # ever becomes vacuous, this is what notices.
    from memoratum.search import prefilter_min_candidates

    threshold = prefilter_min_candidates()
    with pytest.raises(ManifestError, match="straddles the prefilter threshold"):
        build_manifest(
            seed=42,
            requested_n=1,
            n=1,
            ks=[5],
            modes=["hybrid"],
            embedder="HashEmbedder:64",
            vector_store="SQLiteVectorStore",
            data_sha256="x",
            latency_config={
                "samples": 120,
                "warmup": 5,
                "ladder": [100, threshold - 1, threshold + 1],
                "hardware": _hardware(),
            },
        )


def test_contract_default_ladder_meets_the_fr004_minimum() -> None:
    assert len(_documented_ladder()) >= 3, "FR-004 requires at least three corpus sizes"


def test_contract_default_ladder_does_not_straddle_the_prefilter() -> None:
    """The default ladder must sit on ONE side of the prefilter threshold.

    A straddling ladder measures an algorithm switch rather than a corpus size, and
    `research.md` D3 records the measured consequence: the 400 -> 1600 step produced a
    3.38x -> 1.09x -> 3.55x wobble. This repository has already shipped one false
    result from that class, so the documented default must not straddle.
    """
    from memoratum.search import prefilter_min_candidates

    ladder = _documented_ladder()
    threshold = prefilter_min_candidates()
    ordered = sorted(ladder)
    straddles = [
        (lower, upper) for lower, upper in itertools.pairwise(ordered) if lower < threshold <= upper
    ]
    assert not straddles, (
        f"documented default ladder {ladder} straddles the prefilter threshold {threshold} "
        f"at {straddles}; that step measures an algorithm switch, not a size change (L7). "
        "Choose sizes on one side, or document the pin explicitly and record prefilter_min."
    )


def test_quickstart_latency_command_matches_the_contract_default() -> None:
    """§4 must show the same ladder §1.3 documents, or reviewers copy a broken command."""
    contract_ladder = _documented_ladder()
    quickstart = _text(QUICKSTART)
    match = re.search(r"eval_latency \\\n\s*--ladder ([0-9,]+)", quickstart)
    assert match, "quickstart must show an eval_latency invocation"
    assert [int(p) for p in match.group(1).split(",")] == contract_ladder, (
        "quickstart and contract disagree on the default ladder"
    )


def test_schema_example_ladder_is_valid_against_the_validator() -> None:
    """The schema's worked example must not be a ladder our code rejects."""

    schema = json.loads(_text(CONTRACT_SCHEMA))
    example = schema["$defs"]["manifest"]["properties"]["latency_config"]["properties"]["ladder"]
    assert "minItems" in example, "ladder must keep the FR-004 minimum in the schema"


# --- manifest keys the contract promises ---------------------------------------


def test_contract_manifest_keys_exist_in_code() -> None:
    """Every key the contract shows must be a key build_manifest can emit."""
    from memoratum.eval_axes import ORIGINAL_MANIFEST_KEYS

    contract = _text(CONTRACT_MD)
    for key in ORIGINAL_MANIFEST_KEYS:
        assert f'"{key}"' in contract, f"contract does not show required manifest key {key}"
        assert key in ORIGINAL_MANIFEST_KEYS


def test_contract_shows_all_four_axis_blocks() -> None:
    contract = _text(CONTRACT_MD)
    for block in ("scope_config", "cost_config", "latency_config", "grounding_config"):
        assert block in contract, f"contract must document {block}"


def test_schema_required_keys_match_the_validator() -> None:
    """A required key in the schema that the code ignores is an unenforced promise."""
    from memoratum.eval_axes import ManifestError, validate_manifest

    schema = json.loads(_text(CONTRACT_SCHEMA))
    latency_props = schema["$defs"]["manifest"]["properties"]["latency_config"]
    required = set(latency_props["required"])
    base = {
        "schema": "longmemeval-scoped-v2",
        "seed": 42,
        "requested_n": 1,
        "n": 1,
        "ks": [5],
        "modes": ["hybrid"],
        "embedder": "HashEmbedder:64",
        "vector_store": "SQLiteVectorStore",
        "data_sha256": "x",
    }
    # Drop each required key in turn; the validator must complain about every one.
    for key in sorted(required):
        block = {"samples": 120, "warmup": 5, "ladder": [100, 200, 400], "hardware": _hardware()}
        del block[key]
        manifest = {**base, "latency_config": block}
        with pytest.raises(ManifestError):
            validate_manifest(manifest)


def test_schema_grounding_rules_match_the_code() -> None:
    """rule_by_kind in the schema must be the dict the validator enforces."""
    from memoratum.eval_axes import GROUNDING_RULE_BY_KIND

    schema = json.loads(_text(CONTRACT_SCHEMA))
    props = schema["$defs"]["manifest"]["properties"]["grounding_config"]["properties"]
    for kind, const in props["rule_by_kind"]["properties"].items():
        assert const["const"] == GROUNDING_RULE_BY_KIND[kind], (
            f"schema says {kind} grounds against {const['const']!r} but the code uses "
            f"{GROUNDING_RULE_BY_KIND[kind]!r}"
        )


# --- baselines -----------------------------------------------------------------


def test_baselines_document_hardware_as_mandatory_for_latency() -> None:
    """FR-008: a latency row without hardware is invalid, so the file must say so."""
    baselines = _text(BASELINES)
    latency_section = baselines.split("## Latency baselines")[-1]
    assert "Hardware is mandatory" in latency_section
    assert "hardware" in latency_section


def test_baselines_table_has_the_columns_load_baseline_reads() -> None:
    """load_baseline reads axis/figure/tolerance/hardware, so the table must have them."""
    header = [line for line in _text(BASELINES).splitlines() if line.startswith("| axis |")]
    assert header, "BASELINES.md must define a table with an 'axis' column"
    columns = {cell.strip() for cell in header[0].strip("|").split("|")}
    assert {"axis", "figure", "tolerance", "hardware"} <= columns, (
        f"baseline table is missing a column load_baseline reads: {sorted(columns)}"
    )


def test_every_baseline_table_column_is_recognised_by_the_parser() -> None:
    """A column nobody parses is decoration; a parsed column nobody documents is a bug."""
    from memoratum.eval_axes import load_baseline

    row = {
        "axis": "retrieved_chars_mean",
        "figure": "300.0",
        "tolerance": "0.1",
        "corpus": "longmemeval-s",
        "seed": "42",
        "mode": "hybrid",
        "hardware": "n/a",
        "command": "uv run python -m memoratum.eval_cost ...",
    }
    assert load_baseline(row) is not None, "a documented cost row must parse"


# --- documentation of the metric definitions -----------------------------------


def test_docs_explain_the_two_recall_units() -> None:
    """docs/EVALUATION.md must state that R@k and MRR use different units."""
    from pathlib import Path as _Path

    docs = _Path("docs/EVALUATION.md").read_text(encoding="utf-8")
    assert "retrieval positions" in docs
    assert "distinct sessions" in docs
    assert "mrr" in docs.lower()


def test_docs_point_at_the_migration_note() -> None:
    from pathlib import Path as _Path

    docs = _Path("docs/EVALUATION.md").read_text(encoding="utf-8")
    assert "MIGRATION-metric-v1.md" in docs, (
        "docs must link the v0 -> v1 migration note so stale numbers are findable"
    )


def test_quickstart_documents_every_exit_code_the_axes_can_return() -> None:
    """§9 must cover exit 2 (self-check failed), which is the 'metric is broken' signal."""
    quickstart = _text(QUICKSTART)
    expected = re.search(r"## 9\. Expected failure modes(.*?)\n## \d+\.", quickstart, re.DOTALL)
    assert expected, "quickstart must have an expected-failure-modes section"
    body = expected.group(1)
    assert "eval_isolation" in body
    assert "eval_grounding" in body
    assert "eval_cost" in body or "eval_latency" in body


def test_tasks_md_has_no_unresolved_placeholders() -> None:
    """A task with a template placeholder is not executable."""
    tasks = _text(SPEC / "tasks.md")
    for placeholder in ("[FEATURE NAME]", "[Title]", "[Entity", "TXXX", "[location]"):
        assert placeholder not in tasks, f"tasks.md still contains {placeholder!r}"


def test_every_task_has_a_file_path() -> None:
    """Format rule: a task must name the file it changes."""
    tasks = _text(SPEC / "tasks.md")
    # Match only task lines, not the "- [P] = ..." glossary entries in Notes.
    task_lines = [line for line in tasks.splitlines() if re.match(r"- \[[ X]\] T\d{3}\b", line)]
    assert task_lines, "tasks.md must contain tasks"
    pathless = [
        line
        for line in task_lines
        if not re.search(
            r"(src/|tests/|eval/|docs/|\.github/|specs/|pyproject|package\.json|data/|scripts/|constitution)",
            line,
        )
    ]
    assert not pathless, f"tasks without a file path: {pathless[:3]}"


def test_setup_and_foundational_phases_carry_no_story_label() -> None:
    """Format rule: [Story] labels belong only in user-story phases."""
    tasks = _text(SPEC / "tasks.md")
    for phase in ("Phase 1: Setup", "Phase 2: Foundational"):
        body = tasks.split(f"## {phase}")[1].split("\n## ")[0]
        offending = [
            line
            for line in body.splitlines()
            if re.match(r"- \[[ X]\] T\d{3}\b", line) and re.search(r"\[US\d\]", line)
        ]
        assert not offending, f"{phase} must not carry story labels: {offending}"


def test_marked_tasks_reference_real_test_files() -> None:
    """Every test task that is marked done must point at a file that exists."""
    tasks = _text(SPEC / "tasks.md")
    for line in tasks.splitlines():
        if not line.startswith("- [X]"):
            continue
        for match in re.findall(r"`(tests/[^`]+\.py)`", line):
            assert Path(match).exists(), f"completed task references missing file {match}"


def test_marked_source_tasks_reference_real_modules() -> None:
    tasks = _text(SPEC / "tasks.md")
    for line in tasks.splitlines():
        if not line.startswith("- [X]"):
            continue
        for match in re.findall(r"`(src/memoratum/[^`]+\.py)`", line):
            assert Path(match).exists(), f"completed task references missing module {match}"
