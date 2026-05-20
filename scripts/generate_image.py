from __future__ import annotations

import argparse
import base64
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import httpx


def slugify(value: str, max_len: int = 80) -> str:
    safe = "".join(ch.lower() if ch.isalnum() else "-" for ch in value)
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe.strip("-")[:max_len] or "prompt"


def parse_size(size: str) -> tuple[int, int] | None:
    try:
        w, h = size.lower().split("x", 1)
        return int(w), int(h)
    except Exception:  # noqa: BLE001
        return None


def save_error_payload(out_dir: Path, stem: str, payload: Dict[str, Any]) -> Path:
    path = out_dir / f"{stem}.error.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate one image via local proxy and save safely")
    parser.add_argument("--base-url", default="http://127.0.0.1:8011")
    parser.add_argument("--model", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--size", default="1024x1024")
    parser.add_argument("--n", type=int, default=1)
    parser.add_argument("--response-format", default="b64_json", choices=["b64_json", "url"])
    parser.add_argument("--quality", default="medium")
    args = parser.parse_args()

    out_dir = Path("artifacts/images")
    out_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = f"{now}_{slugify(args.model, 40)}_{slugify(args.prompt, 60)}"

    payload = {
        "model": args.model,
        "prompt": args.prompt,
        "n": args.n,
        "size": args.size,
        "quality": args.quality,
        "response_format": args.response_format,
    }

    size_tuple = parse_size(args.size)
    if size_tuple and args.response_format == "b64_json":
        payload["width"], payload["height"] = size_tuple

    with httpx.Client(timeout=180, trust_env=False) as client:
        resp = client.post(f"{args.base_url.rstrip('/')}/v1/images/generations", json=payload)

    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = {"status": resp.status_code, "error": {"message": resp.text or "Non-JSON upstream response"}}

    if resp.status_code != 200:
        err_path = save_error_payload(out_dir, stem, body if isinstance(body, dict) else {"raw": body})
        print(f"[ERR] status={resp.status_code}")
        print(f"Error saved: {err_path}")
        if isinstance(body, dict):
            msg = (body.get("error") or {}).get("message")
            if msg:
                print(f"Message: {msg}")
        return 1

    if not isinstance(body, dict):
        err_path = save_error_payload(out_dir, stem, {"status": resp.status_code, "error": {"message": "Invalid JSON body"}})
        print(f"[ERR] Invalid body. Saved: {err_path}")
        return 1

    data = body.get("data") or []
    if not data or not isinstance(data[0], dict):
        err_path = save_error_payload(out_dir, stem, body)
        print(f"[ERR] Missing data[0]. Saved: {err_path}")
        return 1

    first = data[0]
    out_path = out_dir / f"{stem}.png"

    if first.get("b64_json"):
        try:
            out_path.write_bytes(base64.b64decode(first["b64_json"]))
        except Exception as exc:  # noqa: BLE001
            err_path = save_error_payload(out_dir, stem, {"status": 500, "error": {"message": f"Decode failed: {exc}"}, "body": body})
            print(f"[ERR] Decode failed. Saved: {err_path}")
            return 1
    elif first.get("url"):
        url = first["url"]
        try:
            with httpx.Client(timeout=180, trust_env=False) as client:
                img_resp = client.get(url)
                img_resp.raise_for_status()
                out_path.write_bytes(img_resp.content)
        except Exception as exc:  # noqa: BLE001
            err_path = save_error_payload(out_dir, stem, {"status": 500, "error": {"message": f"Download failed: {exc}"}, "body": body})
            print(f"[ERR] Download failed. Saved: {err_path}")
            return 1
    else:
        err_path = save_error_payload(out_dir, stem, body)
        print(f"[ERR] No b64_json/url in data[0]. Saved: {err_path}")
        return 1

    meta_path = out_dir / f"{stem}.json"
    meta = {
        "generated_at": datetime.now().isoformat(),
        "request": payload,
        "status_code": resp.status_code,
        "response_headers": dict(resp.headers),
        "saved_image": str(out_path),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[OK] Image saved: {out_path}")
    print(f"[OK] Meta saved: {meta_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
