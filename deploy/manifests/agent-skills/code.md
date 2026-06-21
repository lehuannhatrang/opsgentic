---
name: code
description: Edit Kubernetes manifests cleanly and safely.
agents: [remediation]
---
When editing a manifest:

- Preserve the file's existing structure, comments, and formatting; change only the
  target field(s).
- Keep values valid: correct YAML types and Kubernetes units (e.g. memory "256Mi",
  cpu "500m").
- One change per distinct field — never emit the same field twice.
- The result must be valid YAML that still parses to the same kind/metadata.name.
