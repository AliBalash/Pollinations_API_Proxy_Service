from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ImageGenerationRequest(BaseModel):
    prompt: str
    model: Optional[str] = None
    n: Optional[int] = None
    size: Optional[str] = None
    quality: Optional[str] = None
    response_format: Optional[str] = None
    user: Optional[str] = None
    image: Optional[Any] = None
    safe: Optional[str | bool] = None

    class Config:
        extra = "allow"


class HealthResponse(BaseModel):
    status: str
    default_image_model: str
    key_pool_size: int
    proxy_enabled: bool


class KeyRuntimeItem(BaseModel):
    slot: int
    key_masked: str
    success_count: int
    failure_count: int
    last_status: Optional[int] = None
    last_error: str = ""
    cooldown_until: Optional[str] = None
    last_used_at: Optional[str] = None
    last_latency_ms: Optional[float] = None


class KeyStatusResponse(BaseModel):
    generated_at: str
    items: List[KeyRuntimeItem]


class ImageModelItem(BaseModel):
    name: str
    aliases: List[str] = Field(default_factory=list)
    description: Optional[str] = None
    paid_only: Optional[bool] = None
    input_modalities: List[str] = Field(default_factory=list)
    output_modalities: List[str] = Field(default_factory=list)
    pricing: Dict[str, Any] = Field(default_factory=dict)


class ImageModelsResponse(BaseModel):
    generated_at: str
    count: int
    free_count: int
    models: List[ImageModelItem]


class RateLimitSnapshotResponse(BaseModel):
    generated_at: str
    latest: Dict[str, Any]
