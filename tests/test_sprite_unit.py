"""Unit tests for config, state, errors -- no Modal connection required."""

from __future__ import annotations

from modal_sprite.config import SpriteConfig
from modal_sprite.errors import SpriteNotFoundError, SpriteStateError
from modal_sprite.state import SpriteMetadata, SpriteState


# -- SpriteConfig ----------------------------------------------------------

class TestSpriteConfig:
    def test_defaults(self) -> None:
        cfg = SpriteConfig()
        assert cfg.cpu == 2.0
        assert cfg.memory == 2048
        assert cfg.gpu is None
        assert cfg.timeout == 3600
        assert cfg.volumes == {}

    def test_merge_simple(self) -> None:
        cfg = SpriteConfig(cpu=1, memory=1024)
        merged = cfg.merge({"cpu": 4, "gpu": "A10G"})
        assert merged.cpu == 4
        assert merged.gpu == "A10G"
        assert merged.memory == 1024

    def test_merge_volumes_additive(self) -> None:
        cfg = SpriteConfig(volumes={"/data": "vol-a"})
        merged = cfg.merge({"volumes": {"/models": "vol-b"}})
        assert merged.volumes == {"/data": "vol-a", "/models": "vol-b"}

    def test_merge_volumes_replace(self) -> None:
        cfg = SpriteConfig(volumes={"/data": "vol-a"})
        merged = cfg.merge({"volumes": {"/new": "vol-c"}}, replace_volumes=True)
        assert merged.volumes == {"/new": "vol-c"}

    def test_roundtrip_serialization(self) -> None:
        cfg = SpriteConfig(cpu=4, gpu="T4", volumes={"/mnt": "v1"}, env_variables={"FOO": "bar"})
        dumped = cfg.model_dump()
        restored = SpriteConfig.model_validate(dumped)
        assert restored == cfg


# -- SpriteState / SpriteMetadata -------------------------------------------

class TestSpriteState:
    def test_enum_values(self) -> None:
        assert SpriteState.RUNNING == "running"
        assert SpriteState.SLEEPING == "sleeping"
        assert SpriteState.DESTROYED == "destroyed"


class TestSpriteMetadata:
    def test_defaults(self) -> None:
        meta = SpriteMetadata(name="test", state=SpriteState.RUNNING)
        assert meta.sandbox_id is None
        assert meta.checkpoints == {}
        assert meta.pending_action is None
        assert meta.config == SpriteConfig()

    def test_roundtrip(self) -> None:
        meta = SpriteMetadata(
            name="test",
            state=SpriteState.SLEEPING,
            latest_snapshot_image_id="img-123",
            checkpoints={"v1": "img-100"},
            pending_action="reconnect",
            config=SpriteConfig(gpu="A100", memory=16384),
            created_at="2026-04-06T00:00:00Z",
            last_activity_at="2026-04-06T01:00:00Z",
        )
        dumped = meta.model_dump()
        restored = SpriteMetadata.model_validate(dumped)
        assert restored == meta
        assert restored.config.gpu == "A100"
        assert restored.pending_action == "reconnect"

    def test_checkpoints_mutable(self) -> None:
        meta = SpriteMetadata(name="x", state=SpriteState.RUNNING)
        meta.checkpoints["v1"] = "img-abc"
        assert "v1" in meta.checkpoints


# -- Errors -----------------------------------------------------------------

class TestErrors:
    def test_sprite_not_found(self) -> None:
        err = SpriteNotFoundError("foo")
        assert "foo" in str(err)
        assert err.name == "foo"

    def test_sprite_state_error(self) -> None:
        err = SpriteStateError("bar", "sleeping", "exec")
        assert "bar" in str(err)
        assert "sleeping" in str(err)
        assert "exec" in str(err)
