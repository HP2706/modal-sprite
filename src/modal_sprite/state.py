from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from modal_sprite.config import SpriteConfig


class SpriteState(StrEnum):
    RUNNING = "running"
    SLEEPING = "sleeping"
    DESTROYED = "destroyed"


class CheckpointInfo(BaseModel):
    """A named snapshot with metadata."""

    image_id: str
    created_at: str = ""


class SpriteMetadata(BaseModel):
    """Persisted in ``modal.Dict`` under the sprite's name key."""

    name: str
    state: SpriteState
    sandbox_id: str | None = None
    base_image_id: str | None = None
    latest_snapshot_image_id: str | None = None
    checkpoints: dict[str, CheckpointInfo] = Field(default_factory=dict)
    config: SpriteConfig = Field(default_factory=SpriteConfig)
    pending_action: str | None = None
    created_at: str = ""
    last_activity_at: str = ""

    @field_validator("checkpoints", mode="before")
    @classmethod
    def _migrate_legacy_checkpoints(cls, v: dict[str, object]) -> dict[str, object]:
        """Handle old format where checkpoints were {label: image_id_str}."""
        result: dict[str, object] = {}
        for label, value in v.items():
            if isinstance(value, str):
                result[label] = {"image_id": value}
            else:
                result[label] = value
        return result
