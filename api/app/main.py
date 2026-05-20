from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from .config import get_settings
from .schemas import (
    HealthResponse,
    ImageGenerationRequest,
    ImageModelsResponse,
    KeyStatusResponse,
    RateLimitSnapshotResponse,
)
from .services import PollinationsProxyService

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    svc = PollinationsProxyService(settings)
    app.state.service = svc
    yield
    await svc.close()


app = FastAPI(
    title="Pollinations API Proxy Service",
    version="2.0.0",
    docs_url="/docs" if settings.app.docs_enabled else None,
    redoc_url="/redoc" if settings.app.docs_enabled else None,
    openapi_url="/openapi.json" if settings.app.docs_enabled else None,
    lifespan=lifespan,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mask_admin_token(value: str) -> str:
    if not value:
        return ""
    if len(value) < 8:
        return "***"
    return f"{value[:3]}***{value[-2:]}"


async def require_admin(
    request: Request,
    admin_token: Optional[str] = Header(default=None, alias=settings.admin.header_name),
) -> None:
    if not settings.admin.enabled:
        raise HTTPException(status_code=404, detail="Admin endpoints are disabled")
    if not settings.admin.require_token:
        return
    if not settings.admin.token or admin_token != settings.admin.token:
        raise HTTPException(status_code=401, detail="Invalid admin token")


@app.get("/health", response_model=HealthResponse)
async def health(request: Request) -> Dict[str, Any]:
    svc: PollinationsProxyService = request.app.state.service
    return {
        "status": "ok",
        "default_image_model": settings.pollinations.default_image_model,
        "key_pool_size": svc.key_pool.size,
        "proxy_enabled": settings.pollinations.use_proxy_2080,
    }


@app.get("/image/models", response_model=ImageModelsResponse)
async def image_models(request: Request, refresh: bool = False):
    svc: PollinationsProxyService = request.app.state.service
    models = await svc.list_image_models(force_refresh=refresh)
    free = await svc.list_free_image_models(force_refresh=refresh)
    return {
        "generated_at": now_iso(),
        "count": len(models),
        "free_count": len(free),
        "models": models,
    }


@app.get("/image/models/free")
async def image_models_free(request: Request, refresh: bool = False):
    svc: PollinationsProxyService = request.app.state.service
    free = await svc.list_free_image_models(force_refresh=refresh)
    return {
        "generated_at": now_iso(),
        "count": len(free),
        "models": free,
    }


@app.get("/v1/models")
async def v1_models(request: Request, refresh: bool = False):
    svc: PollinationsProxyService = request.app.state.service
    payload = await svc.list_v1_models(force_refresh=refresh)
    payload["_proxied_at"] = now_iso()
    return payload


@app.post("/v1/images/generations")
async def create_image(request: Request, body: ImageGenerationRequest):
    svc: PollinationsProxyService = request.app.state.service
    request_id = request.headers.get("x-request-id") or uuid4().hex
    status_code, payload, extra_headers = await svc.generate_image(
        body.model_dump(exclude_none=True),
        request_id=request_id,
    )
    response = JSONResponse(status_code=status_code, content=payload)
    response.headers["x-request-id"] = request_id
    response.headers["x-proxy-served-by"] = "pollinations-image-proxy"
    for key, value in extra_headers.items():
        response.headers[key] = value
    return response


@app.post("/v1/images/edits")
async def edit_image(request: Request):
    svc: PollinationsProxyService = request.app.state.service
    request_id = request.headers.get("x-request-id") or uuid4().hex
    raw = await request.body()
    content_type = request.headers.get("content-type", "application/json")
    status_code, payload, extra_headers, out_content_type = await svc.edit_image(
        content=raw,
        content_type=content_type,
        request_id=request_id,
    )

    if out_content_type.startswith("application/json"):
        response = JSONResponse(status_code=status_code, content=payload)
    else:
        response = Response(status_code=status_code, content=payload, media_type=out_content_type)

    response.headers["x-request-id"] = request_id
    response.headers["x-proxy-served-by"] = "pollinations-image-proxy"
    for key, value in extra_headers.items():
        response.headers[key] = value
    return response


@app.get("/image/{prompt}")
async def generate_image_url(
    request: Request,
    prompt: str,
    model: Optional[str] = Query(default=None),
    width: Optional[int] = Query(default=None),
    height: Optional[int] = Query(default=None),
    seed: Optional[int] = Query(default=None),
    enhance: Optional[bool] = Query(default=None),
    negative_prompt: Optional[str] = Query(default=None),
    safe: Optional[str] = Query(default=None),
    quality: Optional[str] = Query(default=None),
    image: Optional[str] = Query(default=None),
    transparent: Optional[bool] = Query(default=None),
):
    svc: PollinationsProxyService = request.app.state.service
    request_id = request.headers.get("x-request-id") or uuid4().hex

    params = {
        "model": model,
        "width": width,
        "height": height,
        "seed": seed,
        "enhance": enhance,
        "negative_prompt": negative_prompt,
        "safe": safe,
        "quality": quality,
        "image": image,
        "transparent": transparent,
    }

    status_code, payload, extra_headers, out_content_type = await svc.generate_image_get(
        prompt=prompt,
        params=params,
        request_id=request_id,
    )

    if out_content_type.startswith("application/json"):
        response = JSONResponse(status_code=status_code, content=payload)
    else:
        response = Response(status_code=status_code, content=payload, media_type=out_content_type)

    response.headers["x-request-id"] = request_id
    response.headers["x-proxy-served-by"] = "pollinations-image-proxy"
    for key, value in extra_headers.items():
        response.headers[key] = value
    return response


@app.get("/v1/probe/free-image")
async def probe_free_image(request: Request):
    svc: PollinationsProxyService = request.app.state.service
    free_models = await svc.list_free_image_models(force_refresh=False)
    if not free_models:
        raise HTTPException(status_code=500, detail="No free model found from /image/models")

    free_names = [str(m.get("name") or "") for m in free_models]
    if settings.pollinations.default_image_model in free_names:
        model_name = settings.pollinations.default_image_model
    elif "flux" in free_names:
        model_name = "flux"
    else:
        model_name = free_names[0] or "flux"

    payload = {
        "model": model_name,
        "prompt": "a small test icon of a robot",
        "size": "512x512",
        "quality": "low",
        "response_format": "url",
    }
    status_code, data, headers = await svc.generate_image(payload, request_id=uuid4().hex)
    return {
        "generated_at": now_iso(),
        "model": model_name,
        "status_code": status_code,
        "proxy_headers": headers,
        "response_excerpt": data,
    }


@app.get("/v1/keys/status", response_model=KeyStatusResponse, dependencies=[Depends(require_admin)])
async def keys_status(request: Request):
    svc: PollinationsProxyService = request.app.state.service
    return {"generated_at": now_iso(), "items": svc.key_pool.snapshot()}


@app.get(
    "/v1/rate-limit/last",
    response_model=RateLimitSnapshotResponse,
    dependencies=[Depends(require_admin)],
)
async def rate_limit_snapshot(request: Request):
    svc: PollinationsProxyService = request.app.state.service
    return {
        "generated_at": now_iso(),
        "latest": svc.last_rate_limit_snapshot,
    }


@app.get("/v1/config/effective", dependencies=[Depends(require_admin)])
async def effective_config() -> Dict[str, Any]:
    return {
        "generated_at": now_iso(),
        "app": settings.app.model_dump(),
        "pollinations": {
            "base_url": settings.pollinations.base_url,
            "default_image_model": settings.pollinations.default_image_model,
            "use_proxy_2080": settings.pollinations.use_proxy_2080,
            "proxy_2080_url": settings.pollinations.proxy_2080_url if settings.pollinations.use_proxy_2080 else "",
            "trust_env_proxy": settings.pollinations.trust_env_proxy,
            "api_keys_count": len(settings.pollinations.api_keys),
            "api_keys_masked": ["***" for _ in settings.pollinations.api_keys],
            "max_attempts_per_request": settings.pollinations.max_attempts_per_request,
            "retry_status_codes": settings.pollinations.retry_status_codes,
            "retry_backoff_sec": settings.pollinations.retry_backoff_sec,
            "cooldown_sec": settings.pollinations.cooldown_sec,
        },
        "image_defaults": settings.image_defaults.model_dump(),
        "admin": {
            "enabled": settings.admin.enabled,
            "require_token": settings.admin.require_token,
            "token_masked": _mask_admin_token(settings.admin.token),
            "header_name": settings.admin.header_name,
            "models_cache_ttl_sec": settings.admin.models_cache_ttl_sec,
        },
    }
