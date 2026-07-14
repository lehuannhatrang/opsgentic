from __future__ import annotations

from opsgentic.pipeline.spec import PipelineSpec


def _tools_line(name: str, tools, width: int) -> str:
    joined = ", ".join(sorted(tools)) or "(none)"
    return f"  {name.ljust(width)}  {joined}"


def render_pipeline(spec: PipelineSpec) -> str:
    """Human-readable summary of a pipeline spec: nodes, edges (with routing), and
    per-agent tool wiring. Deterministic ordering (spec declaration order)."""
    lines: list[str] = []
    interrupts = ", ".join(spec.interrupt_before) or "(none)"
    lines.append(f"Pipeline: entrypoint={spec.entrypoint}  interrupt_before=[{interrupts}]")

    node_w = max((len(n.id) for n in spec.nodes), default=0)
    lines += ["", "Nodes:"]
    for n in spec.nodes:
        agents = ", ".join(n.agents) or "(none)"
        lines.append(f"  {n.id.ljust(node_w)}  step={n.step}  agents=[{agents}]")

    edge_w = max((len(e.source) for e in spec.edges), default=0)
    lines += ["", "Edges:"]
    for e in spec.edges:
        if e.conditional:
            branches = ", ".join(f"{k}->{v}" for k, v in e.branches.items())
            lines.append(f"  {e.source.ljust(edge_w)} -[{e.route}]-> {branches}")
        else:
            lines.append(f"  {e.source.ljust(edge_w)} -> {e.target}")

    if spec.agent_tools:
        agent_w = max(len(a) for a in spec.agent_tools)
        lines += ["", "Agent tools:"]
        for agent, tools in spec.agent_tools.items():
            lines.append(_tools_line(agent, tools, agent_w))

    if spec.off_graph_tools:
        off_w = max(len(a) for a in spec.off_graph_tools)
        lines += ["", "Off-graph agents:"]
        for agent, tools in spec.off_graph_tools.items():
            lines.append(_tools_line(agent, tools, off_w))

    return "\n".join(lines)
