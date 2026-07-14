from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from opsgentic.graph.state import MachineState
from opsgentic.pipeline.registry import ROUTER_REGISTRY, STEP_REGISTRY
from opsgentic.pipeline.spec import PipelineSpec, PipelineSpecError, load_spec

_END_TOKEN = "END"


def _resolve_target(name: str):
    """Map the spec's END sentinel to LangGraph's END; pass node ids through."""
    return END if name == _END_TOKEN else name


def build_app(checkpointer, spec: PipelineSpec | None = None):
    """Compile the LangGraph app from the declarative pipeline spec. Node functions and
    routers are looked up by name in the engine registries, so the topology lives in
    config/pipeline.yaml rather than in code."""
    spec = spec or load_spec()
    g = StateGraph(MachineState)

    for node in spec.nodes:
        try:
            fn = STEP_REGISTRY[node.step]
        except KeyError:
            raise PipelineSpecError(
                f"node {node.id!r} uses unknown step {node.step!r} (known: {sorted(STEP_REGISTRY)})"
            )
        # Pass the bare function so LangGraph introspects its signature: nodes taking
        # (state) and (state, config) are both supported (e.g. action_node needs config).
        g.add_node(node.id, fn)

    g.add_edge(START, spec.entrypoint)

    for e in spec.edges:
        if e.conditional:
            try:
                router = ROUTER_REGISTRY[e.route]
            except KeyError:
                raise PipelineSpecError(
                    f"edge from {e.source!r} uses unknown router {e.route!r} "
                    f"(known: {sorted(ROUTER_REGISTRY)})"
                )
            mapping = {label: _resolve_target(tgt) for label, tgt in e.branches.items()}
            g.add_conditional_edges(e.source, router, mapping)
        else:
            g.add_edge(e.source, _resolve_target(e.target))

    return g.compile(checkpointer=checkpointer, interrupt_before=list(spec.interrupt_before))
