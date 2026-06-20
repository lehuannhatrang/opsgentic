from __future__ import annotations

from typing import Any


def from_grafana(payload: dict[str, Any]) -> dict:
    """Normalize a Grafana alerting webhook into an alert_payload."""
    alerts = payload.get("alerts") or []
    first = alerts[0] if alerts else {}
    labels = first.get("labels") or payload.get("commonLabels") or {}
    annotations = first.get("annotations") or payload.get("commonAnnotations") or {}
    return {
        "source": "grafana",
        "title": payload.get("title") or labels.get("alertname", "grafana-alert"),
        "description": annotations.get("description") or annotations.get("summary", ""),
        "severity": labels.get("severity", "unknown"),
        "labels": labels,
        "raw": payload,
    }


def from_chat(payload: dict[str, Any]) -> dict:
    """Normalize user chat input (error context) into an alert_payload."""
    return {
        "source": "chat",
        "title": payload.get("title") or "user-reported issue",
        "description": payload.get("message", ""),
        "severity": payload.get("severity", "unknown"),
        "labels": payload.get("labels") or {},
        "raw": payload,
    }
