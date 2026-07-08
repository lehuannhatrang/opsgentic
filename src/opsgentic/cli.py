from __future__ import annotations

import argparse
import json
import logging
import sys

from opsgentic import runner
from opsgentic.config import get_settings
from opsgentic.triggers import normalize


def _run_main(argv: list[str]) -> None:
    """Legacy entry: run the graph synchronously from a trigger payload file."""
    parser = argparse.ArgumentParser(description="Run an opsgentic graph locally.")
    parser.add_argument("--file", required=True, help="Path to alert/chat JSON")
    parser.add_argument("--source", choices=["grafana", "chat"], default="grafana")
    parser.add_argument("--approve", action="store_true", help="Auto-approve remediation")
    args = parser.parse_args(argv)

    with open(args.file) as f:
        payload = json.load(f)

    normalizer = normalize.from_grafana if args.source == "grafana" else normalize.from_chat
    result = runner.execute_run(normalizer(payload))   # CLI runs the graph synchronously
    print(json.dumps(result, indent=2, default=str))

    if result["awaiting_approval"] and args.approve:
        print("\n--- approving ---\n")
        result = runner.execute_approve(result["thread_id"])
        print(json.dumps(result, indent=2, default=str))


def _cmd_validate(path: str) -> int:
    from opsgentic.pipeline.spec import PipelineSpecError, load_spec_file

    try:
        spec = load_spec_file(path)
    except PipelineSpecError as exc:
        print(f"INVALID: {exc}")
        return 1
    print(f"OK: {path} ({len(spec.nodes)} nodes, entrypoint={spec.entrypoint})")
    return 0


def _cmd_show(path: str) -> int:
    from opsgentic.pipeline.render import render_pipeline
    from opsgentic.pipeline.spec import PipelineSpecError, load_spec_file

    try:
        spec = load_spec_file(path)
    except PipelineSpecError as exc:
        print(f"INVALID: {exc}")
        return 1
    print(render_pipeline(spec))
    return 0


def _pipeline_main(argv: list[str]) -> int:
    """`opsgentic pipeline {validate,show} [path]` -- inspect/validate the blueprint."""
    parser = argparse.ArgumentParser(
        prog="opsgentic pipeline", description="Inspect and validate the pipeline blueprint."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, help_text in (("validate", "Validate a pipeline spec file"),
                            ("show", "Print the pipeline topology")):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("path", nargs="?", default=None,
                        help="Spec path (default: configured pipeline_config_path)")
    args = parser.parse_args(argv)

    path = args.path or get_settings().pipeline_config_path
    return _cmd_validate(path) if args.cmd == "validate" else _cmd_show(path)


def main() -> None:
    logging.basicConfig(level=getattr(logging, get_settings().log_level.upper(), logging.INFO))
    argv = sys.argv[1:]
    if argv and argv[0] == "pipeline":
        raise SystemExit(_pipeline_main(argv[1:]))
    _run_main(argv)


if __name__ == "__main__":
    main()
