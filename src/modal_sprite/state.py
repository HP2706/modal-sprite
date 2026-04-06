from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from modal_sprite.config import SpriteConfig


class SpriteState(StrEnum):
    RUNNING = "running"
    SLEEPING = "sleeping"
    DESTROYED = "destroyed"


class SpriteMetadata(BaseModel):
    """Persisted in ``modal.Dict`` under the sprite's name key."""

    name: str
    state: SpriteState
    sandbox_id: str | None = None
    base_image_id: str | None = None
    latest_snapshot_image_id: str | None = None
    checkpoints: dict[str, str] = Field(default_factory=dict)
    config: SpriteConfig = Field(default_factory=SpriteConfig)
    pending_action: str | None = None
    created_at: str = ""
    last_activity_at: str = ""
