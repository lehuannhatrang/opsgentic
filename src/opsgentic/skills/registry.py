from __future__ import annotations

from typing import Callable

from opsgentic.graph.state import MachineState
from opsgentic.skills import metric_checks

# Validation Skills: plain-Python business logic, called directly (independent of MCP).
# Each skill: (state) -> {"name": str, "passed": bool, "detail": str}
SKILLS: list[Callable[[MachineState], dict]] = [
    metric_checks.hypothesis_present,
    metric_checks.context_present,
]


def run_validation_skills(state: MachineState) -> dict:
    results = [skill(state) for skill in SKILLS]
    passed = all(r["passed"] for r in results)
    return {
        "passed": passed,
        "summary": "all checks passed" if passed else "one or more checks failed",
        "results": results,
    }
