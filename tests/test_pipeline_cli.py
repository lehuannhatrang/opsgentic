from opsgentic.cli import _pipeline_main


def test_pipeline_cli_validate_ok(capsys):
    rc = _pipeline_main(["validate"])
    assert rc == 0
    assert "OK" in capsys.readouterr().out


def test_pipeline_cli_validate_bad(tmp_path, capsys):
    p = tmp_path / "bad.yaml"
    p.write_text(
        "entrypoint: ghost\n"                       # entrypoint not a node
        "nodes:\n  - {id: a, step: rca, agents: []}\n"
        "edges: []\n"
        "agents: {}\n"
    )
    rc = _pipeline_main(["validate", str(p)])
    assert rc == 1
    assert "INVALID" in capsys.readouterr().out


def test_pipeline_cli_show(capsys):
    rc = _pipeline_main(["show"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "entrypoint=rca" in out
    assert "after_validation" in out
