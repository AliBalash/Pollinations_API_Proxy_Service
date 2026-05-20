from __future__ import annotations

import asyncio
import base64
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

from .config import Settings


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def mask_key(key: str) -> str:
    if len(key) < 12:
        return "***"
    return f"{key[:5]}...{key[-4:]}"


def normalize_proxy_url(url: str) -> str:
    candidate = (url or "").strip()
    if candidate.startswith("socks://"):
        candidate = "socks5://" + candidate[len("socks://") :]
    return candidate


@dataclass
class KeyState:
    slot: int
    key: str
    success_count: int = 0
    failure_count: int = 0
    last_status: Optional[int] = None
    last_error: str = ""
    cooldown_until_ts: float = 0.0
    last_used_at: Optional[str] = None
    last_latency_ms: Optional[float] = None


class APIKeyPool:
    def __init__(self, keys: List[str], cooldown_sec: float) -> None:
        self._states = [KeyState(slot=i, key=k) for i, k in enumerate(keys)]
        self._cooldown_sec = max(0.0, float(cooldown_sec))
        self._lock = asyncio.Lock()
        self._index = -1

    @property
    def size(self) -> int:
        return len(self._states)

    async def next_candidates(self) -> List[KeyState]:
        async with self._lock:
            size = len(self._states)
            if size == 0:
                return []
            self._index = (self._index + 1) % size
            ordered = [self._states[(self._index + i) % size] for i in range(size)]

        now = time.monotonic()
        available = [state for state in ordered if state.cooldown_until_ts <= now]
        return available if available else ordered

    def mark_success(self, state: KeyState, status: int, latency_ms: float) -> None:
        state.success_count += 1
        state.last_status = status
        state.last_error = ""
        state.cooldown_until_ts = 0.0
        state.last_used_at = now_iso()
        state.last_latency_ms = round(latency_ms, 2)

    def mark_failure(self, state: KeyState, status: Optional[int], error: str, latency_ms: float) -> None:
        state.failure_count += 1
        state.last_status = status
        state.last_error = error
        state.cooldown_until_ts = time.monotonic() + self._cooldown_sec
        state.last_used_at = now_iso()
        state.last_latency_ms = round(latency_ms, 2)

    def snapshot(self) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        now = time.monotonic()
        for state in self._states:
            cooldown_until = None
            if state.cooldown_until_ts > now:
                cooldown_until = datetime.fromtimestamp(
                    time.time() + (state.cooldown_until_ts - now), timezone.utc
                ).isoformat()
            items.append(
                {
                    "slot": state.slot,
                    "key_masked": mask_key(state.key),
                    "success_count": state.success_count,
                    "failure_count": state.failure_count,
                    "last_status": state.last_status,
                    "last_error": state.last_error,
                    "cooldown_until": cooldown_until,
                    "last_used_at": state.last_used_at,
                    "last_latency_ms": state.last_latency_ms,
                }
            )
        return items


class PollinationsProxyService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.key_pool = APIKeyPool(settings.pollinations.api_keys, cooldown_sec=settings.pollinations.cooldown_sec)

        timeout = httpx.Timeout(settings.app.request_timeout_sec)
        client_kwargs: Dict[str, Any] = {
            "timeout": timeout,
            "trust_env": bool(settings.pollinations.trust_env_proxy),
        }
        if settings.pollinations.use_proxy_2080:
            client_kwargs["proxy"] = normalize_proxy_url(settings.pollinations.proxy_2080_url)
            client_kwargs["trust_env"] = False
        self.client = httpx.AsyncClient(**client_kwargs)

        self.started_at = now_iso()
        self.last_rate_limit_snapshot: Dict[str, Any] = {}
        self._image_models_cache: Optional[List[Dict[str, Any]]] = None
        self._image_models_cache_ts: float = 0.0
        self._v1_models_cache: Optional[Dict[str, Any]] = None
        self._v1_models_cache_ts: float = 0.0

    async def close(self) -> None:
        await self.client.aclose()

    def _build_url(self, suffix: str) -> str:
        return self.settings.pollinations.base_url.rstrip("/") + "/" + suffix.lstrip("/")

    @staticmethod
    def _extract_rate_headers(headers: httpx.Headers) -> Dict[str, str]:
        keys = [
            "x-ratelimit-limit",
            "x-ratelimit-remaining",
            "x-ratelimit-reset",
            "x-ratelimit-limit-requests",
            "x-ratelimit-remaining-requests",
            "x-ratelimit-limit-tokens",
            "x-ratelimit-remaining-tokens",
            "retry-after",
        ]
        out: Dict[str, str] = {}
        for key in keys:
            value = headers.get(key)
            if value is not None:
                out[key] = value
        return out

    def _default_image_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(payload)
        out.setdefault("model", self.settings.pollinations.default_image_model)
        out.setdefault("n", self.settings.image_defaults.n)
        out.setdefault("size", self.settings.image_defaults.size)
        out.setdefault("quality", self.settings.image_defaults.quality)
        out.setdefault("response_format", self.settings.image_defaults.response_format)
        return out

    def _status_retryable(self, status_code: int) -> bool:
        return status_code in set(self.settings.pollinations.retry_status_codes)

    async def _send_with_key_rotation(
        self,
        method: str,
        path: str,
        *,
        json_payload: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        request_id: Optional[str] = None,
        content: Optional[bytes] = None,
        content_type: Optional[str] = None,
    ) -> Tuple[int, httpx.Response, Dict[str, str]]:
        max_attempts = max(
            1,
            min(self.settings.pollinations.max_attempts_per_request, self.key_pool.size * 3),
        )
        attempt = 0
        used_slots: set[int] = set()
        last_response: Optional[httpx.Response] = None
        last_status = 503
        extra_headers: Dict[str, str] = {}

        while attempt < max_attempts:
            candidates = await self.key_pool.next_candidates()
            if not candidates:
                break
            for state in candidates:
                if attempt >= max_attempts:
                    break
                attempt += 1
                used_slots.add(state.slot)

                headers = {"Authorization": f"Bearer {state.key}"}
                if request_id:
                    headers["x-request-id"] = request_id
                if content_type:
                    headers["Content-Type"] = content_type

                started = time.monotonic()
                try:
                    response = await self.client.request(
                        method=method,
                        url=self._build_url(path),
                        headers=headers,
                        json=json_payload,
                        params=params,
                        content=content,
                    )
                    latency_ms = (time.monotonic() - started) * 1000.0
                    last_response = response
                    last_status = response.status_code
                    rate_headers = self._extract_rate_headers(response.headers)
                    if rate_headers:
                        self.last_rate_limit_snapshot = {
                            "captured_at": now_iso(),
                            "path": path,
                            "key_slot": state.slot,
                            "status_code": response.status_code,
                            "headers": rate_headers,
                        }

                    extra_headers = {
                        **rate_headers,
                        "x-proxy-attempts": str(attempt),
                        "x-proxy-key-slot": str(state.slot),
                        "x-proxy-key-rotated": "true" if len(used_slots) > 1 else "false",
                    }

                    if 200 <= response.status_code < 300:
                        self.key_pool.mark_success(state, response.status_code, latency_ms)
                        return response.status_code, response, extra_headers

                    error_text = response.text.strip()[:300]
                    self.key_pool.mark_failure(state, response.status_code, error_text, latency_ms)

                    if self._status_retryable(response.status_code):
                        await asyncio.sleep(self.settings.pollinations.retry_backoff_sec)
                        continue
                    return response.status_code, response, extra_headers
                except Exception as exc:  # noqa: BLE001
                    latency_ms = (time.monotonic() - started) * 1000.0
                    self.key_pool.mark_failure(state, None, str(exc), latency_ms)
                    extra_headers = {
                        "x-proxy-attempts": str(attempt),
                        "x-proxy-key-slot": str(state.slot),
                        "x-proxy-key-rotated": "true" if len(used_slots) > 1 else "false",
                    }
                    await asyncio.sleep(self.settings.pollinations.retry_backoff_sec)

        if last_response is None:
            fallback = httpx.Response(503, json={"status": 503, "success": False, "error": {"message": "No upstream response"}})
            extra_headers.setdefault("x-proxy-attempts", str(max(1, attempt)))
            extra_headers.setdefault("x-proxy-key-rotated", "true" if len(used_slots) > 1 else "false")
            return 503, fallback, extra_headers

        extra_headers.setdefault("x-proxy-attempts", str(max(1, attempt)))
        extra_headers.setdefault("x-proxy-key-rotated", "true" if len(used_slots) > 1 else "false")
        return last_status, last_response, extra_headers

    async def generate_image(self, payload: Dict[str, Any], request_id: Optional[str] = None) -> Tuple[int, Dict[str, Any], Dict[str, str]]:
        body = self._default_image_payload(payload)
        status, response, headers = await self._send_with_key_rotation(
            "POST",
            "/v1/images/generations",
            json_payload=body,
            request_id=request_id,
        )

        try:
            parsed = response.json()
        except Exception:  # noqa: BLE001
            parsed = {"status": status, "success": False, "error": {"message": response.text or "Non-JSON upstream response"}}
        return status, parsed, headers

    async def edit_image(
        self,
        content: bytes,
        content_type: str,
        request_id: Optional[str] = None,
    ) -> Tuple[int, Any, Dict[str, str], str]:
        status, response, headers = await self._send_with_key_rotation(
            "POST",
            "/v1/images/edits",
            content=content,
            content_type=content_type,
            request_id=request_id,
        )

        output_type = response.headers.get("content-type", "application/json")
        if "application/json" in output_type:
            try:
                return status, response.json(), headers, output_type
            except Exception:  # noqa: BLE001
                return status, {"status": status, "success": False, "error": {"message": response.text}}, headers, "application/json"

        return status, response.content, headers, output_type

    async def generate_image_get(
        self,
        prompt: str,
        params: Dict[str, Any],
        request_id: Optional[str] = None,
    ) -> Tuple[int, Any, Dict[str, str], str]:
        clean_params = {k: v for k, v in params.items() if v is not None}
        path = f"/image/{quote(prompt, safe='')}"
        status, response, headers = await self._send_with_key_rotation(
            "GET",
            path,
            params=clean_params,
            request_id=request_id,
        )

        content_type = response.headers.get("content-type", "application/octet-stream")
        if "application/json" in content_type or status >= 400:
            try:
                return status, response.json(), headers, "application/json"
            except Exception:  # noqa: BLE001
                return status, {"status": status, "success": False, "error": {"message": response.text}}, headers, "application/json"
        return status, response.content, headers, content_type

    async def list_image_models(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        now_ts = time.monotonic()
        ttl = self.settings.admin.models_cache_ttl_sec
        if (
            not force_refresh
            and self._image_models_cache is not None
            and (now_ts - self._image_models_cache_ts) <= ttl
        ):
            return self._image_models_cache

        response = await self.client.get(self._build_url("/image/models"))
        response.raise_for_status()
        parsed = response.json()
        if not isinstance(parsed, list):
            raise RuntimeError(f"Unexpected /image/models response: {parsed}")

        self._image_models_cache = parsed
        self._image_models_cache_ts = now_ts
        return parsed

    async def list_v1_models(self, force_refresh: bool = False) -> Dict[str, Any]:
        now_ts = time.monotonic()
        ttl = self.settings.admin.models_cache_ttl_sec
        if (
            not force_refresh
            and self._v1_models_cache is not None
            and (now_ts - self._v1_models_cache_ts) <= ttl
        ):
            return self._v1_models_cache

        response = await self.client.get(self._build_url("/v1/models"))
        response.raise_for_status()
        parsed = response.json()
        if not isinstance(parsed, dict):
            raise RuntimeError(f"Unexpected /v1/models response: {parsed}")

        self._v1_models_cache = parsed
        self._v1_models_cache_ts = now_ts
        return parsed

    async def list_free_image_models(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        models = await self.list_image_models(force_refresh=force_refresh)
        free: List[Dict[str, Any]] = []
        for model in models:
            if bool((model or {}).get("paid_only", False)):
                continue
            outputs = model.get("output_modalities") or []
            if "image" not in outputs:
                continue
            free.append(model)
        return free

    async def save_b64_image(self, b64_payload: str, target_path: str) -> None:
        raw = base64.b64decode(b64_payload)
        with open(target_path, "wb") as f:
            f.write(raw)
