from opsgentic import agent_registry as reg


def test_agent_tools_mapping_matches_ground_truth():
    assert reg.AGENT_TOOLS["context"] == {"kubernetes", "prometheus"}
    assert reg.AGENT_TOOLS["remediation"] == {"kubernetes", "github", "prometheus"}
    assert reg.AGENT_TOOLS["pr-responder"] == {"kubernetes", "prometheus", "github"}
    # Reasoning-only agents declare no MCP servers.
    assert reg.AGENT_TOOLS["rca"] == set()
    assert reg.AGENT_TOOLS["resolver"] == set()
    assert reg.AGENT_TOOLS["validation"] == set()


def test_node_agents_cover_the_four_graph_nodes():
    assert set(reg.NODE_AGENTS) == {"rca", "resolve_target", "validation", "action"}
    assert reg.NODE_AGENTS["rca"] == ["context", "rca"]
    assert reg.NODE_AGENTS["action"] == ["remediation"]
    assert reg.OFF_GRAPH_AGENTS == ["pr-responder"]
