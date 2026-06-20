from __future__ import annotations

import io
import re
from typing import Optional

from ruamel.yaml import YAML

# Round-trip YAML: preserves comments, key order, and formatting so edits are minimal.
_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.width = 4096
_yaml.indent(mapping=2, sequence=4, offset=2)   # common k8s style -> minimal diffs


def apply_ops(content: str, ops: list) -> Optional[str]:
    """Apply surgical field edits to YAML `content` and return the new text.

    Each op: {"yaml_path": "a.b.c[name=x].d", "value": ...}. Only the named fields
    change; everything else is preserved verbatim. Returns None if nothing applied.
    """
    try:
        docs = [d for d in _yaml.load_all(content) if d is not None]
    except Exception:
        return None
    if not docs:
        return None

    applied = 0
    for op in ops:
        path = op.get("yaml_path") or op.get("path")
        if not path or "value" not in op:
            continue
        if any(_set_by_path(d, path, op["value"]) for d in docs):
            applied += 1
    if not applied:
        return None

    buf = io.StringIO()
    if len(docs) == 1:
        _yaml.dump(docs[0], buf)
    else:
        _yaml.dump_all(docs, buf)
    return buf.getvalue()


def _tokens(path: str) -> list:
    toks: list = []
    for seg in path.split("."):
        m = re.match(r"^([^\[\]]+)((?:\[[^\]]+\])*)$", seg)
        if not m:
            toks.append(("key", seg))
            continue
        toks.append(("key", m.group(1)))
        for sel in re.findall(r"\[([^\]]+)\]", seg):
            if "=" in sel:
                field, val = sel.split("=", 1)
                toks.append(("sel", (field.strip(), val.strip())))
            else:
                toks.append(("idx", int(sel)))
    return toks


def _set_by_path(doc, path: str, value) -> bool:
    toks = _tokens(path)
    cur = doc
    for i, (typ, v) in enumerate(toks):
        last = i == len(toks) - 1
        try:
            if typ == "key":
                if last:
                    cur[v] = _coerce(value)
                    return True
                cur = cur[v]
            elif typ == "sel":
                field, val = v
                match = next((it for it in cur if str(it.get(field)) == str(val)), None)
                if match is None:
                    return False
                cur = match
            elif typ == "idx":
                cur = cur[v]
        except Exception:
            return False
    return False


def _coerce(value):
    if isinstance(value, (int, float, bool)):
        return value
    s = str(value)
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    return s
