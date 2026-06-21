---
name: validation
description: What makes a remediation safe and valid.
agents: [remediation]
---
A good remediation is:

- Minimal — the smallest change that addresses the root-cause hypothesis.
- Targeted — touches only the field(s) the fix requires; no unrelated edits.
- Reversible — a plain config change a human can review and roll back via Git.
- Declarative-first — prefer adjusting Kubernetes config (resources, replicas, env) over
  changing application behavior or command/args.

Avoid: speculative or broad changes, edits to images/probes/unrelated fields, and changes
that mask a problem without addressing the stated cause.
