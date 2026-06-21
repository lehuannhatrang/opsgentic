---
name: gitops
description: Locate the owning GitOps manifest and make the minimal change to fix it.
agents: [remediation, resolver]
---
All changes flow through Git: never mutate the cluster directly, only edit manifests.

Locating the manifest under the GitOps source path:
- Account for plain manifests, Kustomize (bases/overlays/patches), and Helm values.
- Read the actual file to confirm its exact repo-relative path and current value before
  proposing a change — do not guess the path or the existing value.
- Match the responsible file to the alerting workload by kind and metadata.name.

Choosing the change:
- Make the MINIMAL change that addresses the hypothesis. Change only the field(s) the fix
  requires; never touch the image, probes, or unrelated fields.
- For resource pressure (OOMKilled / high memory or CPU), prefer adjusting
  resources.requests/limits rather than the container command or args.

Mapping an alert to its repo (resolution): match the workload's namespace and name to the
owning ArgoCD Application or Flux Kustomization that manages it.
