import pytest

from opsgentic.pipeline.spec import PipelineSpecError, load_spec_file


def test_load_spec_file_valid(tmp_path):
    p = tmp_path / "ok.yaml"
    p.write_text(
        "entrypoint: a\n"
        "nodes:\n  - {id: a, step: rca, agents: []}\n"
        "edges:\n  - {from: a, to: END}\n"
        "agents: {}\n"
    )
    spec = load_spec_file(p)
    assert [n.id for n in spec.nodes] == ["a"]


def test_load_spec_file_missing_raises(tmp_path):
    with pytest.raises(PipelineSpecError):
        load_spec_file(tmp_path / "nope.yaml")


def test_load_spec_file_malformed_yaml_raises(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("nodes: [unclosed\n")
    with pytest.raises(PipelineSpecError):
        load_spec_file(p)


def test_load_spec_file_missing_node_key_raises(tmp_path):
    p = tmp_path / "x.yaml"
    p.write_text("entrypoint: a\nnodes:\n  - {id: a}\nedges: []\nagents: {}\n")
    with pytest.raises(PipelineSpecError):  # missing 'step'
        load_spec_file(p)


def test_load_spec_file_missing_edge_key_raises(tmp_path):
    p = tmp_path / "y.yaml"
    p.write_text(
        "entrypoint: a\n"
        "nodes:\n  - {id: a, step: rca, agents: []}\n"
        "edges:\n  - {to: END}\n"      # missing 'from'
        "agents: {}\n"
    )
    with pytest.raises(PipelineSpecError):
        load_spec_file(p)
