from __future__ import annotations

import argparse
import base64
import json
import time
from pathlib import Path
from typing import Any, Dict, List

import httpx


PROMPTS = [
    "a cyberpunk cat in Tehran",
    "spider man inside iran flag",
]


def slugify(value: str) -> str:
    safe = "".join(ch.lower() if ch.isalnum() else "-" for ch in value)
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe.strip("-")[:100]


def get_free_models(client: httpx.Client, base_url: str) -> List[str]:
    resp = client.get(f"{base_url.rstrip('/')}/image/models/free", timeout=60)
    resp.raise_for_status()
    data = resp.json()
    return [str(m.get("name")) for m in data.get("models", []) if isinstance(m, dict) and m.get("name")]


def pick_models(free_models: List[str], preferred: List[str]) -> List[str]:
    picked: List[str] = []
    free_set = set(free_models)
    for model in preferred:
        if model in free_set and model not in picked:
            picked.append(model)
    for model in free_models:
        if model not in picked:
            picked.append(model)
        if len(picked) >= 2:
            break
    return picked[:2]


def fetch_image_from_url(client: httpx.Client, url: str) -> bytes:
    resp = client.get(url, timeout=120)
    resp.raise_for_status()
    return resp.content


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark free Pollinations image models via local proxy")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--models", default="flux,gptimage", help="Preferred models (comma-separated)")
    args = parser.parse_args()

    out_dir = Path("artifacts/images")
    out_dir.mkdir(parents=True, exist_ok=True)
    report_dir = Path("artifacts/benchmarks")
    report_dir.mkdir(parents=True, exist_ok=True)

    preferred = [m.strip() for m in args.models.split(",") if m.strip()]
    run_ts = int(time.time())

    with httpx.Client(timeout=200, trust_env=False) as client:
        free_models = get_free_models(client, args.base_url)
        models = pick_models(free_models, preferred)
        if len(models) < 2:
            raise RuntimeError("Could not find at least two free image models from /image/models/free")

        print("Using models:", ", ".join(models))

        results: List[Dict[str, Any]] = []

        for model in models:
            for prompt in PROMPTS:
                payload = {
                    "model": model,
                    "prompt": prompt,
                    "n": 1,
                    "size": "1024x1024",
                    "quality": "medium",
                    "response_format": "b64_json",
                }
                started = time.perf_counter()
                response = client.post(f"{args.base_url.rstrip('/')}/v1/images/generations", json=payload)
                latency_ms = round((time.perf_counter() - started) * 1000.0, 2)

                record: Dict[str, Any] = {
                    "model": model,
                    "prompt": prompt,
                    "status_code": response.status_code,
                    "latency_ms": latency_ms,
                    "proxy_headers": {
                        "x-proxy-attempts": response.headers.get("x-proxy-attempts"),
                        "x-proxy-key-slot": response.headers.get("x-proxy-key-slot"),
                        "x-proxy-key-rotated": response.headers.get("x-proxy-key-rotated"),
                    },
                }

                try:
                    body = response.json()
                except Exception:  # noqa: BLE001
                    body = {"error": {"message": response.text}}

                record["id"] = body.get("id") if isinstance(body, dict) else None

                if response.status_code == 200 and isinstance(body, dict):
                    data = body.get("data") or []
                    first = data[0] if data else {}
                    b64_img = first.get("b64_json") if isinstance(first, dict) else None
                    url_img = first.get("url") if isinstance(first, dict) else None

                    try:
                        image_bytes: bytes
                        if b64_img:
                            image_bytes = base64.b64decode(b64_img)
                        elif url_img:
                            image_bytes = fetch_image_from_url(client, url_img)
                        else:
                            raise RuntimeError("No b64_json or url in data[0]")

                        img_name = f"{run_ts}_{slugify(model)}_{slugify(prompt)}.png"
                        img_path = out_dir / img_name
                        img_path.write_bytes(image_bytes)

                        record["ok"] = True
                        record["image_path"] = str(img_path)
                    except Exception as exc:  # noqa: BLE001
                        record["ok"] = False
                        record["error"] = f"Decode/save error: {exc}"
                else:
                    err_msg = "Unknown error"
                    if isinstance(body, dict):
                        err_msg = str((body.get("error") or {}).get("message") or body)
                    record["ok"] = False
                    record["error"] = err_msg

                results.append(record)
                status_label = "OK" if record["ok"] else "ERR"
                print(f"[{status_label}] {model} | {prompt} | status={response.status_code} | latency={latency_ms}ms")

    report = {
        "generated_at_unix": run_ts,
        "preferred_models": preferred,
        "used_models": models,
        "prompts": PROMPTS,
        "results": results,
    }
    report_path = report_dir / f"benchmark_{run_ts}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved report: {report_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
