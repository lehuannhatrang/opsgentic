from __future__ import annotations

from opsgentic.graph.state import MachineState

# M1: placeholder, deterministic skills. M2/M4 add real SLO/metric checks.


def hypothesis_present(state: MachineState) -> dict:
    ok = bool(state.get("hypothesis"))
    return {
        "name": "hypothesis_present",
        "passed": ok,
        "detail": "RCA produced a hypothesis" if ok else "missing hypothesis",
    }


def context_present(state: MachineState) -> dict:
    ok = bool(state.get("context_data"))
    return {
        "name": "context_present",
        "passed": ok,
        "detail": "context available" if ok else "no context gathered",
    }
