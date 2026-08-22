"""Discovery over the AgentCash catalog: search by intent, list an origin's
endpoints. Thin wrappers over the CLI's `search` / `discover`."""
from __future__ import annotations

import json
import shutil
import subprocess


def _cmd(binary):
    if binary:
        return binary.split()
    if shutil.which("agentcash"):
        return ["agentcash"]
    return ["npx", "agentcash@latest"]


def _run_json(args):
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "command failed")
    txt = proc.stdout.strip()
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        obj, _ = json.JSONDecoder().raw_decode(txt)
        return obj


def _find(obj, key):
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if key in cur:
                return cur[key]
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def search(query, binary=None, limit=10):
    data = _run_json(_cmd(binary) + ["search", query, "--format", "json"])
    results = _find(data, "results")
    if isinstance(results, dict):
        results = _find(results, "results") or []
    rows = []
    for r in (results or [])[:limit]:
        if not isinstance(r, dict):
            continue
        og = r.get("origin")
        origin = og.get("url", "") if isinstance(og, dict) else (og or "")
        rows.append({"path": r.get("path"), "method": r.get("method", "POST"),
                     "price": r.get("price"), "summary": r.get("summary", ""), "origin": origin})
    return rows


def discover(origin, binary=None):
    data = _run_json(_cmd(binary) + ["discover", origin, "--format", "json"])
    rows = []
    for e in (_find(data, "endpoints") or []):
        if not isinstance(e, dict):
            continue
        rows.append({"path": e.get("path"), "method": e.get("method", "POST"),
                     "price": e.get("price"), "summary": e.get("summary", "")})
    return rows
