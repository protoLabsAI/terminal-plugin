"""The developer `gate` must mirror ci.yml's Linux `test` job exactly, so a coder who
runs `make gate` sees the same checks (and the same ordering/fail-fast) CI will. These
tests pin the Makefile's shape to the workflow — if either drifts, they fail.

The suite never *runs* the gate (that would re-enter pytest); it parses the Makefile and,
where `make` is on PATH, uses `make -n` (dry run — nothing executes) to confirm ordering.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
MAKEFILE = ROOT / "Makefile"

# Exactly what ci.yml's Linux `test` job runs, in order (see .github/workflows/ci.yml:
# "Lint" → "Test (Python)" → "Test (view JS)").
CI_COMMANDS = [
    "ruff check .",
    "ruff format --check .",
    "pytest -q",
    "node --test tests/js/*.test.mjs",
]


def _lines() -> list[str]:
    # Keep raw lines (with their leading tab/space) so we can assert on indentation.
    return MAKEFILE.read_text().splitlines()


def _recipes() -> dict[str, list[str]]:
    """target -> its recipe command lines, in order (recipe lines are tab-indented)."""
    recipes: dict[str, list[str]] = {}
    current: str | None = None
    for line in _lines():
        if line.startswith("\t"):
            if current is not None:
                recipes[current].append(line[1:].strip())
            continue
        m = re.match(r"^([A-Za-z0-9_-]+)\s*:(?!=)", line)
        current = m.group(1) if m else None
        if current is not None:
            recipes.setdefault(current, [])
    return recipes


def _prereqs(target: str) -> list[str]:
    for line in _lines():
        m = re.match(rf"^{re.escape(target)}\s*:(?!=)(.*)", line)
        if m:
            return m.group(1).split()
    raise AssertionError(f"no rule for target {target!r}")


def test_makefile_exists_at_repo_root():
    assert MAKEFILE.is_file()


def test_phony_declares_all_four_targets():
    phony = set()
    for line in _lines():
        if line.startswith(".PHONY:"):
            phony |= set(line.split(":", 1)[1].split())
    assert {"gate", "lint", "test", "test-js"} <= phony


def test_recipe_lines_are_tab_indented_not_spaces():
    # A space-indented recipe is the classic silent Makefile breakage ("missing separator").
    in_recipe = False
    for line in _lines():
        if re.match(r"^[A-Za-z0-9_-]+\s*:(?!=)", line):
            in_recipe = True
            continue
        if in_recipe and line and not line[0].isspace():
            in_recipe = False
        if in_recipe and line.strip() and line.startswith(" "):
            raise AssertionError(f"recipe line indented with spaces, not a tab: {line!r}")


def test_each_target_runs_its_verbatim_ci_commands():
    recipes = _recipes()
    assert recipes["lint"] == ["ruff check .", "ruff format --check ."]
    assert recipes["test"] == ["pytest -q"]
    assert recipes["test-js"] == ["node --test tests/js/*.test.mjs"]


def test_gate_depends_on_the_three_targets_in_order():
    assert _prereqs("gate") == ["lint", "test", "test-js"]


def test_gate_expands_to_exactly_the_ci_commands_in_order():
    """Walk gate's prerequisites and concatenate each one's recipe — the result must be
    the CI command list, in CI order."""
    recipes = _recipes()
    expanded = [cmd for target in _prereqs("gate") for cmd in recipes[target]]
    assert expanded == CI_COMMANDS


@pytest.mark.skipif(shutil.which("make") is None, reason="make not on PATH")
def test_make_dry_run_emits_the_ci_commands_in_order():
    # `make -n` prints the recipe it *would* run without executing anything.
    out = subprocess.run(
        ["make", "-n", "gate"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    emitted = [ln.strip() for ln in out.splitlines() if ln.strip()]
    assert emitted == CI_COMMANDS


def test_gate_commands_match_the_ci_workflow():
    """Guard against ci.yml drifting away from the Makefile: pull the Linux job's run
    steps straight from the workflow and check the gate covers the same commands."""
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    runs = "\n".join(step["run"] for step in wf["jobs"]["test"]["steps"] if isinstance(step.get("run"), str))
    # Every command the gate runs is a command the CI job runs.
    for cmd in CI_COMMANDS:
        assert cmd in runs, f"{cmd!r} not found in ci.yml test job"
