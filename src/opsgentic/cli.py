from __future__ import annotations

import argparse
import json

from opsgentic import runner
from opsgentic.triggers import normalize


def main() -> None:
    parser = argparse.ArgumentParser(description="Run an opsgentic graph locally.")
    parser.add_argument("--file", required=True, help="Path to alert/chat JSON")
    parser.add_argument("--source", choices=["grafana", "chat"], default="grafana")
    parser.add_argument("--approve", action="store_true", help="Auto-approve remediation")
    args = parser.parse_args()

    with open(args.file) as f:
        payload = json.load(f)

    normalizer = normalize.from_grafana if args.source == "grafana" else normalize.from_chat
    result = runner.execute_run(normalizer(payload))   # CLI runs the graph synchronously
    print(json.dumps(result, indent=2, default=str))

    if result["awaiting_approval"] and args.approve:
        print("\n--- approving ---\n")
        result = runner.execute_approve(result["thread_id"])
        print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
