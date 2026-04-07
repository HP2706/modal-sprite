"""Unit tests for findings 1-5 fixes.

These tests use mocking to avoid needing a live Modal connection.
"""

from __future__ import annotations

import asyncio
import time
import threading
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

from modal_sprite.config import SpriteConfig
from modal_sprite.monitor import SpriteMonitor, SNAPSHOT_SECONDS_BEFORE_TIMEOUT
from modal_sprite.state import SpriteMetadata, SpriteState


# ---------------------------------------------------------------------------
# Finding 2: Monitor timing uses started_at, not creation time
# ---------------------------------------------------------------------------


class TestMonitorTiming:
    """Verify that SpriteMonitor uses `started_at` for elapsed time calculation."""

    def test_default_started_at_is_now(self) -> None:
        """When started_at is not provided, it defaults to current time."""
        before = time.time()
        monitor = SpriteMonitor(
            sandbox=MagicMock(),
            timeout=3600,
            on_snapshot=MagicMock(),
            on_expiry=MagicMock(),
        )
        after = time.time()
        assert before <= monitor._started_at <= after

    def test_custom_started_at_is_respected(self) -> None:
        """When started_at is provided, the monitor uses that instead of now."""
        past = time.time() - 1000
        monitor = SpriteMonitor(
            sandbox=MagicMock(),
            timeout=3600,
            on_snapshot=MagicMock(),
            on_expiry=MagicMock(),
            started_at=past,
        )
        assert monitor._started_at == past

    def test_monitor_expires_immediately_when_started_at_is_old(self) -> None:
        """If started_at is far in the past, the monitor should fire expiry quickly."""
        on_expiry = MagicMock()
        on_snapshot = MagicMock()
        # Sandbox started 3601 seconds ago with a 3600s timeout => already expired
        past = time.time() - 3601
        monitor = SpriteMonitor(
            sandbox=MagicMock(),
            timeout=3600,
            on_snapshot=on_snapshot,
            on_expiry=on_expiry,
            started_at=past,
        )
        monitor.start()
        # Give the thread a moment to run
        time.sleep(1.5)
        monitor.stop()

        on_expiry.assert_called_once()
        # Snapshot should NOT have been taken (we passed the window entirely)
        on_snapshot.assert_not_called()

    def test_monitor_snapshots_in_window(self) -> None:
        """Monitor takes a snapshot when remaining time is ~SNAPSHOT_SECONDS_BEFORE_TIMEOUT."""
        fake_image = MagicMock()
        fake_image.object_id = "img-test-123"
        fake_sandbox = MagicMock()
        fake_sandbox.snapshot_filesystem.return_value = fake_image

        on_snapshot = MagicMock()
        on_expiry = MagicMock()

        # Set started_at so that remaining time is exactly in the snapshot window
        # timeout=100, we want remaining=30 => elapsed=70 => started_at = now - 70
        started_at = time.time() - 70
        monitor = SpriteMonitor(
            sandbox=fake_sandbox,
            timeout=100,
            on_snapshot=on_snapshot,
            on_expiry=on_expiry,
            started_at=started_at,
        )
        monitor.start()
        time.sleep(1.5)
        monitor.stop()

        on_snapshot.assert_called_once_with(fake_image)
        assert monitor.snapshot_taken

    def test_monitor_no_snapshot_when_too_early(self) -> None:
        """Monitor should not snapshot when there's plenty of time remaining."""
        on_snapshot = MagicMock()
        on_expiry = MagicMock()

        # Just started, 3600s remaining — well outside snapshot window
        monitor = SpriteMonitor(
            sandbox=MagicMock(),
            timeout=3600,
            on_snapshot=on_snapshot,
            on_expiry=on_expiry,
        )
        monitor.start()
        time.sleep(1.5)
        monitor.stop()

        on_snapshot.assert_not_called()
        on_expiry.assert_not_called()


# ---------------------------------------------------------------------------
# Finding 4: clone() starts a monitor
# ---------------------------------------------------------------------------


class TestCloneStartsMonitor:
    """Verify that Sprite.clone() starts a monitor on the new sprite."""

    @pytest.mark.asyncio
    async def test_clone_has_monitor(self) -> None:
        """After cloning, the new Sprite should have a running monitor."""
        from modal_sprite.sprite import Sprite

        fake_sandbox = MagicMock()
        fake_sandbox.object_id = "sb-clone-123"

        fake_image = MagicMock()

        parent_metadata = SpriteMetadata(
            name="parent",
            state=SpriteState.RUNNING,
            sandbox_id="sb-parent-123",
            sandbox_started_at=time.time(),
            latest_snapshot_image_id="img-snap-123",
            config=SpriteConfig(timeout=300),
        )

        parent = Sprite(
            _name="parent",
            _metadata=parent_metadata,
            _registry=MagicMock(),
            _app=MagicMock(),
            _sandbox=MagicMock(),
        )

        with (
            patch("modal_sprite.sprite.sm.create_sandbox", new_callable=AsyncMock, return_value=fake_sandbox),
            patch("modal_sprite.sprite.Image.from_id", return_value=fake_image),
            patch("modal_sprite.sprite.SpriteRegistry") as MockRegistry,
        ):
            mock_registry_instance = MockRegistry.return_value
            mock_registry_instance.exists = AsyncMock(return_value=False)
            mock_registry_instance.put = AsyncMock()

            clone = await parent.clone("child")

            assert clone._monitor is not None
            assert clone._sandbox is fake_sandbox
            # Clean up
            clone._stop_monitor()


# ---------------------------------------------------------------------------
# Finding 3: idle_timeout passed through to Sandbox.create
# ---------------------------------------------------------------------------


class TestIdleTimeoutPassthrough:
    """Verify idle_timeout from config is forwarded to Modal's Sandbox.create."""

    @pytest.mark.asyncio
    async def test_idle_timeout_forwarded(self) -> None:
        """create_sandbox should pass idle_timeout to Sandbox.create.aio."""
        from modal_sprite.sandbox_manager import create_sandbox

        config = SpriteConfig(timeout=600, idle_timeout=120)

        fake_sandbox = MagicMock()
        fake_sandbox.object_id = "sb-test"
        fake_sandbox.filesystem.write_text = AsyncMock()
        fake_proc = MagicMock()
        fake_proc.wait = AsyncMock()
        fake_sandbox.exec = AsyncMock(return_value=fake_proc)

        with (
            patch("modal_sprite.sandbox_manager.Sandbox.create") as mock_create,
            patch("modal_sprite.sandbox_manager._get_base_image", return_value=MagicMock()),
            patch("modal_sprite.sandbox_manager.modal_config") as mock_modal_config,
            patch("modal_sprite.sandbox_manager.Secret.from_dict", return_value=MagicMock()),
        ):
            mock_create.aio = AsyncMock(return_value=fake_sandbox)
            mock_modal_config.get.return_value = "fake-token"

            await create_sandbox(MagicMock(), config)

            call_kwargs = mock_create.aio.call_args.kwargs
            assert "idle_timeout" in call_kwargs
            assert call_kwargs["idle_timeout"] == 120


# ---------------------------------------------------------------------------
# Finding 1: Shell monitor runs during interactive session
# ---------------------------------------------------------------------------


class TestShellMonitor:
    """Verify _make_shell_monitor creates a working snapshot-only monitor."""

    def test_make_shell_monitor_creates_monitor(self) -> None:
        """_make_shell_monitor returns a SpriteMonitor that can start/stop."""
        from modal_sprite.terminal import _make_shell_monitor

        mock_registry = MagicMock()
        mock_registry.get_sync.return_value = SpriteMetadata(
            name="test",
            state=SpriteState.RUNNING,
            sandbox_id="sb-1",
        )

        monitor = _make_shell_monitor(
            sandbox=MagicMock(),
            timeout=3600,
            registry=mock_registry,
            sprite_name="test",
            started_at=time.time(),
        )

        assert isinstance(monitor, SpriteMonitor)
        monitor.start()
        time.sleep(0.2)
        monitor.stop()

    def test_shell_monitor_snapshots_update_registry(self) -> None:
        """When shell monitor takes a snapshot, it updates the registry."""
        from modal_sprite.terminal import _make_shell_monitor

        fake_image = MagicMock()
        fake_image.object_id = "img-shell-snap"
        fake_sandbox = MagicMock()
        fake_sandbox.snapshot_filesystem.return_value = fake_image

        mock_registry = MagicMock()
        existing_meta = SpriteMetadata(
            name="test",
            state=SpriteState.RUNNING,
            sandbox_id="sb-1",
        )
        mock_registry.get_sync.return_value = existing_meta

        # Position time so snapshot fires immediately (remaining ~30s)
        started_at = time.time() - 70  # 100s timeout, 70s elapsed => 30s remaining
        monitor = _make_shell_monitor(
            sandbox=fake_sandbox,
            timeout=100,
            registry=mock_registry,
            sprite_name="test",
            started_at=started_at,
        )
        monitor.start()
        time.sleep(1.5)
        monitor.stop()

        # Verify registry was updated with the snapshot
        mock_registry.put_sync.assert_called_once()
        saved_meta = mock_registry.put_sync.call_args[0][1]
        assert saved_meta.latest_snapshot_image_id == "img-shell-snap"

    def test_shell_monitor_expiry_is_noop(self) -> None:
        """Shell monitor's on_expiry should not update registry state."""
        from modal_sprite.terminal import _make_shell_monitor

        mock_registry = MagicMock()

        # Already expired
        started_at = time.time() - 3601
        monitor = _make_shell_monitor(
            sandbox=MagicMock(),
            timeout=3600,
            registry=mock_registry,
            sprite_name="test",
            started_at=started_at,
        )
        monitor.start()
        time.sleep(1.5)
        monitor.stop()

        # Registry should NOT have been updated for expiry
        # (get_sync might be called by snapshot path, but put_sync should not
        # be called since on_expiry is a no-op logger call)
        mock_registry.put_sync.assert_not_called()


# ---------------------------------------------------------------------------
# Finding 5: Terminal checks sandbox liveness before reporting
# ---------------------------------------------------------------------------


class TestTerminalStateReporting:
    """Verify run_shell_loop checks sandbox liveness on normal exit."""

    @pytest.mark.asyncio
    async def test_reports_terminated_when_sandbox_dead(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When sandbox.poll() returns non-None (dead), terminal should report termination."""
        from modal_sprite.terminal import run_shell_loop

        meta = SpriteMetadata(
            name="test-sprite",
            state=SpriteState.RUNNING,
            sandbox_id="sb-dead",
            sandbox_started_at=time.time(),
            latest_snapshot_image_id="img-snap",
            config=SpriteConfig(timeout=3600),
        )

        mock_registry = MagicMock()
        mock_registry.get = AsyncMock(return_value=meta)
        mock_registry.put = AsyncMock()

        fake_sandbox = MagicMock()
        fake_sandbox.object_id = "sb-dead"
        # poll.aio returns non-None => sandbox is dead
        fake_sandbox.poll.aio = AsyncMock(return_value=0)

        fake_process = MagicMock()
        fake_process.attach = AsyncMock()
        fake_sandbox.exec = AsyncMock(return_value=fake_process)

        with (
            patch("modal_sprite.terminal.sm.reconnect_sandbox", new_callable=AsyncMock, return_value=fake_sandbox),
            patch("modal_sprite.terminal.SpriteMonitor") as MockMonitor,
        ):
            mock_monitor_instance = MagicMock()
            MockMonitor.return_value = mock_monitor_instance

            await run_shell_loop("test-sprite", mock_registry, MagicMock())

        captured = capsys.readouterr()
        assert "terminated" in captured.out.lower()
        assert "still running" not in captured.out.lower()
        # Should have updated registry to SLEEPING
        mock_registry.put.assert_called()
        last_put_meta = mock_registry.put.call_args[0][1]
        assert last_put_meta.state == SpriteState.SLEEPING

    @pytest.mark.asyncio
    async def test_reports_running_when_sandbox_alive(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When sandbox.poll() returns None (alive), terminal should report still running."""
        from modal_sprite.terminal import run_shell_loop

        meta = SpriteMetadata(
            name="test-sprite",
            state=SpriteState.RUNNING,
            sandbox_id="sb-alive",
            sandbox_started_at=time.time(),
            config=SpriteConfig(timeout=3600),
        )

        mock_registry = MagicMock()
        mock_registry.get = AsyncMock(return_value=meta)
        mock_registry.put = AsyncMock()

        fake_sandbox = MagicMock()
        fake_sandbox.object_id = "sb-alive"
        # poll.aio returns None => sandbox is still alive
        fake_sandbox.poll.aio = AsyncMock(return_value=None)

        fake_process = MagicMock()
        fake_process.attach = AsyncMock()
        fake_sandbox.exec = AsyncMock(return_value=fake_process)

        with (
            patch("modal_sprite.terminal.sm.reconnect_sandbox", new_callable=AsyncMock, return_value=fake_sandbox),
            patch("modal_sprite.terminal.SpriteMonitor") as MockMonitor,
        ):
            mock_monitor_instance = MagicMock()
            MockMonitor.return_value = mock_monitor_instance

            await run_shell_loop("test-sprite", mock_registry, MagicMock())

        captured = capsys.readouterr()
        assert "still running" in captured.out.lower()


# ---------------------------------------------------------------------------
# SpriteMetadata: sandbox_started_at field
# ---------------------------------------------------------------------------


class TestMetadataSandboxStartedAt:
    """Verify the new sandbox_started_at field round-trips correctly."""

    def test_default_is_none(self) -> None:
        meta = SpriteMetadata(name="test", state=SpriteState.RUNNING)
        assert meta.sandbox_started_at is None

    def test_roundtrip(self) -> None:
        ts = time.time()
        meta = SpriteMetadata(
            name="test",
            state=SpriteState.RUNNING,
            sandbox_started_at=ts,
        )
        dumped = meta.model_dump(mode="json")
        restored = SpriteMetadata.model_validate(dumped)
        assert restored.sandbox_started_at == ts

    def test_backwards_compatible_without_field(self) -> None:
        """Old metadata without sandbox_started_at should still parse."""
        raw = {
            "name": "old-sprite",
            "state": "running",
            "sandbox_id": "sb-old",
        }
        meta = SpriteMetadata.model_validate(raw)
        assert meta.sandbox_started_at is None
