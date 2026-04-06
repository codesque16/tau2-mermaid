#!/usr/bin/env python3
"""
Resolve Vertex warmup target from YAML for vertex_endpoint_warm.sh.

Supports:
  - Registry file with ``endpoints:`` and optional ``ref: { yaml, run_id }`` to reuse
    tau2 ``runs`` (including ``base_run`` merge).
  - Tau2-style file with top-level ``runs:`` only: pass the run id as the key.

Predict URL logic matches ``tau2.utils.vertex_endpoint_chat.build_vertex_predict_url``;
keep the two in sync if that function changes.
"""
from __future__ import annotations

import argparse
import os
import shlex
from pathlib import Path
from typing import Any

import yaml

WARM_KEYS = frozenset({"interval_sec", "predict_timeout", "project_id"})


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if k == "base_run":
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def run_chain_root_to_leaf(runs: dict[str, Any], run_id: str) -> list[str]:
    chain: list[str] = []
    seen: set[str] = set()
    cur: str | None = run_id
    while cur:
        if cur in seen:
            raise SystemExit(f"runs: cycle involving {run_id!r}")
        seen.add(cur)
        if cur not in runs:
            raise SystemExit(f"runs: unknown run_id {cur!r}")
        chain.append(cur)
        nxt = runs[cur].get("base_run")
        cur = str(nxt).strip() if nxt else None
    return list(reversed(chain))


def merge_run(runs: dict[str, Any], run_id: str) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for rid in run_chain_root_to_leaf(runs, run_id):
        merged = deep_merge(merged, runs[rid])
    return merged


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: root must be a mapping")
    return data


def resolve_llm_args(config_path: Path, key: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Returns (llm_args_for_predict_url, warm_settings).
    warm_settings: interval_sec, predict_timeout, project_id (optional each).
    """
    root = load_yaml(config_path)
    defaults = root.get("defaults") if isinstance(root.get("defaults"), dict) else {}
    warm_defaults = {k: defaults[k] for k in WARM_KEYS if k in defaults}

    endpoints = root.get("endpoints")
    runs = root.get("runs")

    if isinstance(endpoints, dict) and key in endpoints:
        entry = endpoints[key]
        if not isinstance(entry, dict):
            raise SystemExit(f"endpoints[{key!r}] must be a mapping")
        ref = entry.get("ref")
        warm_local = {k: entry[k] for k in WARM_KEYS if k in entry}
        overrides = {k: v for k, v in entry.items() if k not in ("ref", *WARM_KEYS)}

        if ref is not None:
            if not isinstance(ref, dict):
                raise SystemExit(f"endpoints[{key!r}].ref must be a mapping")
            rel = ref.get("yaml") or ref.get("file")
            if not rel:
                raise SystemExit(f"endpoints[{key!r}].ref needs yaml: or file:")
            run_yaml = (config_path.parent / str(rel)).resolve()
            run_id = str(ref.get("run_id") or key).strip()
            ext = load_yaml(run_yaml)
            ext_runs = ext.get("runs")
            if not isinstance(ext_runs, dict):
                raise SystemExit(f"{run_yaml}: expected top-level runs:")
            merged_run = merge_run(ext_runs, run_id)
            llm = dict(merged_run.get("agent_llm_args") or {})
            llm = deep_merge(llm, overrides)
        else:
            llm = dict(overrides)

        warm = {**warm_defaults, **warm_local}
        return llm, warm

    if isinstance(runs, dict) and key in runs:
        merged_run = merge_run(runs, key)
        llm = dict(merged_run.get("agent_llm_args") or {})
        warm = dict(warm_defaults)
        return llm, warm

    raise SystemExit(
        f"Unknown key {key!r}: no endpoints[{key!r}] or runs[{key!r}] in {config_path}"
    )


def build_vertex_predict_url(llm_args: dict[str, Any]) -> str:
    endpoint_id = str(llm_args.get("vertex_endpoint_id") or "").strip()
    if not endpoint_id:
        raise ValueError("vertex_endpoint_id is required for dedicated Vertex endpoint mode.")
    project = (
        str(llm_args.get("vertex_project") or "").strip()
        or (os.environ.get("VERTEXAI_PROJECT") or "").strip()
        or (os.environ.get("GOOGLE_CLOUD_PROJECT") or "").strip()
    )
    if not project:
        raise ValueError(
            "Set vertex_project in llm_args or VERTEXAI_PROJECT / GOOGLE_CLOUD_PROJECT "
            "for dedicated Vertex endpoint mode."
        )
    location = str(llm_args.get("vertex_location") or "").strip() or "us-central1"
    base = str(llm_args.get("vertex_http_predict_base") or "").strip().rstrip("/")
    if not base:
        dedicated_domain = (os.environ.get("DEDICATED_ENDPOINT_DOMAIN") or "").strip()
        if dedicated_domain:
            base = f"https://{dedicated_domain}"
    if not base:
        base = f"https://{endpoint_id}.{location}-{project}.prediction.vertexai.goog"
    api_ver = str(llm_args.get("vertex_http_predict_api_version") or "v1").strip().strip("/") or "v1"
    return (
        f"{base}/{api_ver}/projects/{project}/locations/{location}/endpoints/"
        f"{endpoint_id}:predict"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Emit shell assignments for vertex_endpoint_warm.sh")
    ap.add_argument("config", type=Path, help="YAML config path")
    ap.add_argument("key", help="endpoints key or runs run_id")
    ap.add_argument(
        "--interval-override",
        type=int,
        default=None,
        help="Override interval_sec (CLI wins over YAML)",
    )
    args = ap.parse_args()
    config_path = args.config.expanduser().resolve()
    if not config_path.is_file():
        raise SystemExit(f"Config not found: {config_path}")

    llm_args, warm = resolve_llm_args(config_path, args.key)
    try:
        url = build_vertex_predict_url(llm_args)
    except ValueError as e:
        raise SystemExit(str(e)) from e

    interval = args.interval_override
    if interval is None:
        interval = warm.get("interval_sec", 120)
    predict_timeout = warm.get("predict_timeout", 120)
    project_id = str(warm.get("project_id") or os.environ.get("PROJECT_ID") or "gemini-1xn")

    try:
        interval = int(interval)
        predict_timeout = int(predict_timeout)
    except (TypeError, ValueError):
        raise SystemExit("interval_sec and predict_timeout must be integers")

    print(f"PREDICT_URL={shlex.quote(url)}")
    print(f"INTERVAL_SEC={shlex.quote(str(interval))}")
    print(f"PREDICT_TIMEOUT={shlex.quote(str(predict_timeout))}")
    print(f"PROJECT_ID={shlex.quote(project_id)}")
    print(f"CONFIG_KEY={shlex.quote(args.key)}")


if __name__ == "__main__":
    main()
