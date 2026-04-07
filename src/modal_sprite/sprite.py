"""Core Sprite class -- the user-facing API for persistent cloud computers."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import modal
from modal import Image, Sandbox

from modal_sprite import sandbox_manager as sm
from modal_sprite.config import SpriteConfig
from modal_sprite.errors import SpriteNotFoundError, SpriteStateError
from modal_sprite.monitor import SpriteMonitor
from modal_sprite.registry import SpriteRegistry
from modal_sprite.state import CheckpointInfo, SpriteMetadata, SpriteState
from modal_sprite.terminal import run_shell_loop

logger = logging.getLogger(__name__)

APP_NAME = "modal-sprite"


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


@dataclass
class Sprite:
    """A persistent, durable cloud computer backed by a Modal sandbox."""

    _name: str
    _metadata: SpriteMetadata
    _registry: SpriteRegistry
    _app: modal.App
    _sandbox: Sandbox | None = field(default=None, repr=False)
    _monitor: SpriteMonitor | None = field(default=None, repr=False)

    # ------------------------------------------------------------------
    # Factory class methods
    # ------------------------------------------------------------------

    @classmethod
    async def create(
        cls,
        name: str,
        config: SpriteConfig | None = None,
        *,
        base_image_id: str | None = None,
    ) -> Sprite:
        """Create a brand-new sprite and start its sandbox."""
        registry = SpriteRegistry()
        assert not await registry.exists(name), f"Sprite '{name}' already exists"

        cfg = config or SpriteConfig()
        app = await modal.App.lookup.aio(APP_NAME, create_if_missing=True)

        image: Image | None = None
        if base_image_id:
            image = Image.from_id(base_image_id)

        sandbox_started_at = time.time()
        sandbox = await sm.create_sandbox(app, cfg, image=image, sprite_name=name)

        now = _now()
        metadata = SpriteMetadata(
            name=name,
            state=SpriteState.RUNNING,
            sandbox_id=sandbox.object_id,
            sandbox_started_at=sandbox_started_at,
            base_image_id=base_image_id,
            config=cfg,
            created_at=now,
            last_activity_at=now,
        )
        await registry.put(name, metadata)

        sprite = cls(
            _name=name,
            _metadata=metadata,
            _registry=registry,
            _app=app,
            _sandbox=sandbox,
        )
        sprite._start_monitor()
        return sprite

    @classmethod
    async def get(cls, name: str) -> Sprite:
        """Retrieve an existing sprite by name."""
        registry = SpriteRegistry()
        metadata = await registry.get(name)
        if metadata is None:
            raise SpriteNotFoundError(name)

        app = await modal.App.lookup.aio(APP_NAME, create_if_missing=True)

        sprite = cls(
            _name=name,
            _metadata=metadata,
            _registry=registry,
            _app=app,
        )

        if metadata.state == SpriteState.RUNNING and metadata.sandbox_id:
            sb = await sm.reconnect_sandbox(metadata.sandbox_id)
            if sb is not None:
                sprite._sandbox = sb
                sprite._start_monitor()
            else:
                logger.info("Sandbox for '%s' is dead, marking as sleeping", name)
                metadata.state = SpriteState.SLEEPING
                metadata.sandbox_id = None
                await registry.put(name, metadata)
                sprite._metadata = metadata

        return sprite

    @classmethod
    def list_all_sync(cls) -> dict[str, SpriteMetadata]:
        """Synchronous list for CLI use."""
        return SpriteRegistry().list_all_sync()

    @classmethod
    async def list_all(cls) -> dict[str, SpriteMetadata]:
        """Return metadata for every registered sprite."""
        return await SpriteRegistry().list_all()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    @property
    def status(self) -> SpriteState:
        return self._metadata.state

    @property
    def config(self) -> SpriteConfig:
        return self._metadata.config

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    async def shell(self) -> None:
        """Open an interactive shell with reconnect-on-pending-action loop."""
        self._stop_monitor()
        await run_shell_loop(self._name, self._registry, self._app)
        # Refresh metadata after shell exits
        meta = await self._registry.get(self._name)
        if meta is not None:
            self._metadata = meta

    async def sleep(self) -> None:
        """Snapshot the filesystem, terminate the sandbox, and go to sleep."""
        assert self._metadata.state == SpriteState.RUNNING, SpriteStateError(
            self._name, self._metadata.state, "sleep"
        )
        assert self._sandbox is not None

        self._stop_monitor()

        image = await sm.snapshot_sandbox(self._sandbox)
        await sm.terminate_sandbox(self._sandbox)

        self._metadata.state = SpriteState.SLEEPING
        self._metadata.sandbox_id = None
        self._metadata.latest_snapshot_image_id = image.object_id
        self._metadata.last_activity_at = _now()
        await self._registry.put(self._name, self._metadata)

        self._sandbox = None
        logger.info("Sprite '%s' is now sleeping (snapshot: %s)", self._name, image.object_id)

    async def wake(self) -> None:
        """Restore from the latest snapshot and start a new sandbox."""
        assert self._metadata.state == SpriteState.SLEEPING, SpriteStateError(
            self._name, self._metadata.state, "wake"
        )
        assert self._metadata.latest_snapshot_image_id is not None, (
            f"Sprite '{self._name}' has no snapshot to restore from"
        )

        image = Image.from_id(self._metadata.latest_snapshot_image_id)
        sandbox_started_at = time.time()
        sandbox = await sm.create_sandbox(
            self._app, self._metadata.config, image=image, sprite_name=self._name,
        )

        self._sandbox = sandbox
        self._metadata.state = SpriteState.RUNNING
        self._metadata.sandbox_id = sandbox.object_id
        self._metadata.sandbox_started_at = sandbox_started_at
        self._metadata.last_activity_at = _now()
        await self._registry.put(self._name, self._metadata)

        self._start_monitor()
        logger.info("Sprite '%s' is now awake (sandbox: %s)", self._name, sandbox.object_id)

    async def restore(self, label: str) -> None:
        """Restore to a named checkpoint version."""
        checkpoint = self._metadata.checkpoints.get(label)
        if checkpoint is None:
            available = ", ".join(self._metadata.checkpoints.keys()) or "(none)"
            raise ValueError(f"Checkpoint '{label}' not found. Available: {available}")

        # Terminate current sandbox if running
        if self._sandbox is not None:
            self._stop_monitor()
            await sm.terminate_sandbox(self._sandbox)
            self._sandbox = None

        # Set checkpoint as restore target and wake from it
        self._metadata.latest_snapshot_image_id = checkpoint.image_id
        self._metadata.state = SpriteState.SLEEPING
        self._metadata.sandbox_id = None
        await self._registry.put(self._name, self._metadata)

        await self.wake()

    async def push(self, local_path: str, remote_path: str) -> None:
        """Upload a local file into the sprite's sandbox."""
        assert self._sandbox is not None, "Sprite must be running to push files"
        await self._sandbox.filesystem.copy_from_local.aio(local_path, remote_path)

    async def pull(self, remote_path: str, local_path: str) -> None:
        """Download a file from the sprite's sandbox."""
        assert self._sandbox is not None, "Sprite must be running to pull files"
        await self._sandbox.filesystem.copy_to_local.aio(remote_path, local_path)

    async def clone(self, new_name: str) -> Sprite:
        """Fork this sprite into a new one from its latest snapshot."""
        assert self._metadata.latest_snapshot_image_id is not None, (
            f"Sprite '{self._name}' has no snapshot to clone from. "
            "Run a checkpoint first or sleep/wake to create a snapshot."
        )

        registry = SpriteRegistry()
        assert not await registry.exists(new_name), f"Sprite '{new_name}' already exists"

        image = Image.from_id(self._metadata.latest_snapshot_image_id)
        sandbox_started_at = time.time()
        sandbox = await sm.create_sandbox(
            self._app, self._metadata.config, image=image, sprite_name=new_name,
        )

        now = _now()
        new_metadata = SpriteMetadata(
            name=new_name,
            state=SpriteState.RUNNING,
            sandbox_id=sandbox.object_id,
            sandbox_started_at=sandbox_started_at,
            base_image_id=self._metadata.base_image_id,
            latest_snapshot_image_id=self._metadata.latest_snapshot_image_id,
            config=self._metadata.config.model_copy(),
            created_at=now,
            last_activity_at=now,
        )
        await registry.put(new_name, new_metadata)

        clone = Sprite(
            _name=new_name,
            _metadata=new_metadata,
            _registry=registry,
            _app=self._app,
            _sandbox=sandbox,
        )
        clone._start_monitor()
        return clone

    async def destroy(self) -> None:
        """Permanently destroy the sprite and all its state."""
        if self._sandbox is not None:
            self._stop_monitor()
            await sm.terminate_sandbox(self._sandbox)
            self._sandbox = None

        await self._registry.delete(self._name)
        self._metadata.state = SpriteState.DESTROYED
        self._metadata.sandbox_id = None
        logger.info("Sprite '%s' destroyed", self._name)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _start_monitor(self) -> None:
        if self._sandbox is None:
            return
        self._monitor = SpriteMonitor(
            sandbox=self._sandbox,
            timeout=self._metadata.config.timeout,
            on_snapshot=self._on_monitor_snapshot,
            on_expiry=self._on_monitor_expiry,
            started_at=self._metadata.sandbox_started_at,
        )
        self._monitor.start()

    def _stop_monitor(self) -> None:
        if self._monitor is not None:
            self._monitor.stop()
            self._monitor = None

    def _on_monitor_snapshot(self, image: Image) -> None:
        """Called from the monitor thread (sync)."""
        self._metadata.latest_snapshot_image_id = image.object_id
        self._metadata.last_activity_at = _now()
        self._registry.put_sync(self._name, self._metadata)

    def _on_monitor_expiry(self) -> None:
        """Called from the monitor thread (sync)."""
        self._sandbox = None
        self._metadata.state = SpriteState.SLEEPING
        self._metadata.sandbox_id = None
        self._metadata.last_activity_at = _now()
        self._registry.put_sync(self._name, self._metadata)
        logger.info("Sprite '%s' auto-slept on timeout expiry", self._name)
