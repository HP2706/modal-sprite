from __future__ import annotations

from pydantic import BaseModel, Field


class SpriteConfig(BaseModel):
    """Configuration for a Sprite's sandbox resources."""

    cpu: float | tuple[float, float] = 2.0
    memory: int | tuple[int, int] = 2048
    gpu: str | None = None
    timeout: int = 3600
    idle_timeout: int = 300
    volumes: dict[str, str] = Field(default_factory=dict)
    encrypted_ports: list[int] = Field(default_factory=list)
    unencrypted_ports: list[int] = Field(default_factory=list)
    env_variables: dict[str, str] = Field(default_factory=dict)
    workdir: str = "/root"

    def merge(self, overrides: dict[str, object], *, replace_volumes: bool = False) -> SpriteConfig:
        """Return a new config with *overrides* applied.

        For ``volumes``, the default behaviour is an additive merge (new mount
        paths are added, existing ones preserved).  Pass ``replace_volumes=True``
        to replace the volumes dict entirely.
        """
        data = self.model_dump()
        for key, value in overrides.items():
            if key == "volumes" and not replace_volumes:
                data["volumes"] = {**data["volumes"], **value}
            else:
                data[key] = value
        return SpriteConfig.model_validate(data)
