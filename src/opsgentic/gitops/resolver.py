from __future__ import annotations

import logging
import re
from typing import Optional

from opsgentic.gitops.giturl import parse_repo_url
from opsgentic.gitops.providers import get_provider

logger = logging.getLogger(__name__)

WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Rollout"}


def derive_service_ref(alert: dict) -> dict:
    """Best-effort workload identity (namespace/name/kind) from the alert."""
    labels = alert.get("labels", {}) or {}
    name = labels.get("workload") or labels.get("deployment") or labels.get("app")
    if not name and labels.get("pod"):
        name = _strip_pod_suffix(labels["pod"])
    return {
        "namespace": labels.get("namespace") or "default",
        "name": name,
        "kind": labels.get("workload_kind", "Deployment"),
    }


def _strip_pod_suffix(pod: str) -> str:
    parts = pod.split("-")
    if len(parts) >= 3 and re.fullmatch(r"[a-z0-9]{6,10}", parts[-2]) and re.fullmatch(r"[a-z0-9]{5}", parts[-1]):
        return "-".join(parts[:-2])      # Deployment -> ReplicaSet -> Pod
    if len(parts) >= 2 and parts[-1].isdigit():
        return "-".join(parts[:-1])      # StatefulSet ordinal
    if len(parts) >= 2:
        return "-".join(parts[:-1])
    return pod


def resolve_target(alert: dict) -> Optional[dict]:
    """Resolve (repo, path, provider) for the alerting workload.

    Chain: explicit labels -> ArgoCD -> Flux. Returns None when unresolved
    (the graph then escalates instead of guessing a repo).
    """
    labels = alert.get("labels", {}) or {}
    if labels.get("gitops_repo"):
        return _from_labels(labels)

    svc = derive_service_ref(alert)
    if not svc.get("name"):
        return None
    k = _k8s()
    if k is None:
        return None

    candidates = _argocd_candidates(k, svc) + _flux_candidates(k, svc)
    if not candidates:
        return None
    return _choose(candidates, alert)


def _k8s():
    try:
        from kubernetes import client, config

        try:
            config.load_incluster_config()
        except Exception:
            config.load_kube_config()
        return client
    except Exception as exc:
        logger.warning("kubernetes client unavailable: %s", exc)
        return None


def _make_target(repo_url: str, host: str, slug: str, path: str, revision: str, source: str) -> dict:
    prov = get_provider(host)
    segs = slug.split("/")
    return {
        "repo_url": repo_url,
        "host": host,
        "slug": slug,
        "owner": segs[0],
        "repo": segs[-1],
        "path": (path or "").lstrip("./"),
        "revision": revision or "",
        "provider": prov.type if prov else "github",
        "source": source,
    }


def _from_labels(labels: dict) -> dict:
    repo = labels["gitops_repo"]
    if "://" in repo or repo.startswith("git@"):
        ref = parse_repo_url(repo)
        if not ref:
            return None  # type: ignore[return-value]
        host, slug, url = ref.host, ref.slug, repo
    else:
        host = (labels.get("gitops_host") or "github.com").lower()
        slug = repo
        url = f"https://{host}/{slug}.git"
    return _make_target(url, host, slug, labels.get("gitops_path", ""), labels.get("gitops_revision", ""), "labels")


def _argocd_candidates(k, svc: dict) -> list:
    out: list = []
    try:
        api = k.CustomObjectsApi()
        items = api.list_cluster_custom_object("argoproj.io", "v1alpha1", "applications").get("items", [])
    except Exception as exc:
        logger.info("ArgoCD lookup skipped: %s", exc)
        return out
    for app in items:
        spec = app.get("spec", {}) or {}
        source = spec.get("source") or (spec.get("sources") or [{}])[0]
        repo_url = source.get("repoURL")
        if not repo_url:
            continue
        score = _argocd_score(app, svc)
        if score <= 0:
            continue
        ref = parse_repo_url(repo_url)
        if not ref:
            continue
        t = _make_target(repo_url, ref.host, ref.slug, source.get("path", ""), source.get("targetRevision", ""), "argocd")
        t["_score"] = score
        t["_app"] = (app.get("metadata", {}) or {}).get("name")
        out.append(t)
    return out


def _argocd_score(app: dict, svc: dict) -> int:
    resources = (app.get("status", {}) or {}).get("resources", []) or []
    for r in resources:
        if r.get("namespace") == svc["namespace"] and r.get("name") == svc["name"] and r.get("kind") in WORKLOAD_KINDS:
            return 3
    for r in resources:
        if r.get("namespace") == svc["namespace"] and r.get("name") == svc["name"]:
            return 2
    dest = (app.get("spec", {}) or {}).get("destination", {}) or {}
    return 1 if dest.get("namespace") == svc["namespace"] else 0


def _flux_candidates(k, svc: dict) -> list:
    out: list = []
    try:
        labels = (k.AppsV1Api().read_namespaced_deployment(svc["name"], svc["namespace"]).metadata.labels) or {}
    except Exception as exc:
        logger.info("Flux workload lookup skipped: %s", exc)
        return out
    ks_name = labels.get("kustomize.toolkit.fluxcd.io/name")
    ks_ns = labels.get("kustomize.toolkit.fluxcd.io/namespace")
    if not (ks_name and ks_ns):
        return out
    try:
        api = k.CustomObjectsApi()
        ks = api.get_namespaced_custom_object("kustomize.toolkit.fluxcd.io", "v1", ks_ns, "kustomizations", ks_name)
        src = (ks.get("spec", {}) or {}).get("sourceRef", {}) or {}
        if src.get("kind") != "GitRepository":
            return out
        gr = api.get_namespaced_custom_object(
            "source.toolkit.fluxcd.io", "v1", src.get("namespace", ks_ns), "gitrepositories", src["name"]
        )
        url = (gr.get("spec", {}) or {}).get("url")
        rev = ((gr.get("spec", {}) or {}).get("ref", {}) or {}).get("branch", "")
        ref = parse_repo_url(url)
        if not ref:
            return out
        t = _make_target(url, ref.host, ref.slug, (ks.get("spec", {}) or {}).get("path", ""), rev, "flux")
        t["_score"] = 3
        out.append(t)
    except Exception as exc:
        logger.info("Flux Kustomization resolve failed: %s", exc)
    return out


def _choose(candidates: list, alert: dict) -> dict:
    candidates.sort(key=lambda c: c.get("_score", 0), reverse=True)
    top_score = candidates[0].get("_score", 0)
    tied = [c for c in candidates if c.get("_score", 0) == top_score]
    if len(tied) == 1:
        return _clean(tied[0])
    chosen = _llm_pick(tied, alert) or tied[0]   # hybrid: LLM tiebreak, else best-ranked
    return _clean(chosen)


def _llm_pick(tied: list, alert: dict) -> Optional[dict]:
    from opsgentic.agents.llm import get_llm

    llm = get_llm()
    if llm is None:
        return None
    from langchain_core.messages import HumanMessage, SystemMessage

    listing = "\n".join(
        f"{i}. {c['repo_url']} (path={c.get('path')}, app={c.get('_app')})" for i, c in enumerate(tied)
    )
    prompt = (
        f"Alert: {alert.get('title')}\nLabels: {alert.get('labels')}\n\n"
        f"Candidate GitOps sources:\n{listing}\n\n"
        "Reply with ONLY the index number of the source that manages the alerting workload."
    )
    try:
        resp = llm.invoke([
            SystemMessage(content="Map a Kubernetes alert to its owning GitOps repo. Answer with a single index number."),
            HumanMessage(content=prompt),
        ])
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
        m = re.search(r"\d+", text)
        if m and 0 <= int(m.group()) < len(tied):
            return tied[int(m.group())]
    except Exception as exc:
        logger.warning("LLM disambiguation failed: %s", exc)
    return None


def _clean(target: dict) -> dict:
    return {k: v for k, v in target.items() if not k.startswith("_")}
