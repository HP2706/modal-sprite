"""Integration tests that require a live Modal connection.

Run with: uv run pytest tests/integration/ --run-integration -v
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from modal_sprite import sandbox_manager as sm
from modal_sprite.config import SpriteConfig
from modal_sprite.registry import SpriteRegistry
from modal_sprite.sprite import Sprite
from modal_sprite.state import SpriteState

pytestmark = pytest.mark.integration


def _unique_name(prefix: str = "test") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# -- Basic lifecycle --------------------------------------------------------

@pytest.mark.asyncio
async def test_create_and_destroy() -> None:
    """Create a sprite, verify it's running, destroy it."""
    name = _unique_name()
    sprite = await Sprite.create(name, config=SpriteConfig(timeout=300))
    assert sprite.status == SpriteState.RUNNING
    assert sprite._sandbox is not None

    await sprite.destroy()
    assert sprite.status == SpriteState.DESTROYED


@pytest.mark.asyncio
async def test_sleep_wake_persistence() -> None:
    """Write a file via sandbox exec, sleep, wake, verify file persists."""
    name = _unique_name()
    sprite = await Sprite.create(name, config=SpriteConfig(timeout=300))

    # Write a file directly via sandbox exec (internal, for testing)
    proc = await sprite._sandbox.exec.aio("bash", "-c", "echo 'persisted' > /root/test.txt")
    await proc.wait.aio()

    await sprite.sleep()
    assert sprite.status == SpriteState.SLEEPING

    await sprite.wake()
    assert sprite.status == SpriteState.RUNNING

    # Read back
    proc = await sprite._sandbox.exec.aio("cat", "/root/test.txt")
    stdout = await proc.stdout.read.aio()
    assert "persisted" in stdout

    await sprite.destroy()


# -- Push / Pull -----------------------------------------------------------

@pytest.mark.asyncio
async def test_push_pull(tmp_path: Path) -> None:
    """Push a local file in, pull it back out."""
    name = _unique_name()
    sprite = await Sprite.create(name, config=SpriteConfig(timeout=300))

    # Write a local file
    local_file = tmp_path / "hello.txt"
    local_file.write_text("push pull test\n")

    # Push into sandbox
    await sprite.push(str(local_file), "/root/hello.txt")

    # Verify it's there
    proc = await sprite._sandbox.exec.aio("cat", "/root/hello.txt")
    stdout = await proc.stdout.read.aio()
    assert "push pull test" in stdout

    # Pull it back
    pulled = tmp_path / "pulled.txt"
    await sprite.pull("/root/hello.txt", str(pulled))
    assert pulled.read_text() == "push pull test\n"

    await sprite.destroy()


# -- Clone -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_clone() -> None:
    """Clone a sprite from a checkpoint, verify filesystem is shared."""
    name = _unique_name()
    sprite = await Sprite.create(name, config=SpriteConfig(timeout=300))

    # Write a marker file and checkpoint
    proc = await sprite._sandbox.exec.aio("bash", "-c", "echo 'cloned!' > /root/marker.txt")
    await proc.wait.aio()
    image = await sm.snapshot_sandbox(sprite._sandbox)
    sprite._metadata.latest_snapshot_image_id = image.object_id
    sprite._metadata.checkpoints["v1"] = image.object_id
    await sprite._registry.put(name, sprite._metadata)

    # Clone
    clone_name = _unique_name("clone")
    clone = await sprite.clone(clone_name)
    assert clone.status == SpriteState.RUNNING

    # Verify cloned file exists
    proc = await clone._sandbox.exec.aio("cat", "/root/marker.txt")
    stdout = await proc.stdout.read.aio()
    assert "cloned!" in stdout

    await clone.destroy()
    await sprite.destroy()


# -- modal-sprite-ctl injection --------------------------------------------------

@pytest.mark.asyncio
async def test_sprite_ctl_exists() -> None:
    """Verify modal-sprite-ctl is injected and executable."""
    name = _unique_name()
    sprite = await Sprite.create(name, config=SpriteConfig(timeout=300))

    proc = await sprite._sandbox.exec.aio("which", "modal-sprite-ctl")
    stdout = await proc.stdout.read.aio()
    assert "/usr/local/bin/modal-sprite-ctl" in stdout

    # Verify it runs (--help equivalent)
    proc = await sprite._sandbox.exec.aio(
        "modal-sprite-ctl",
        env={"SPRITE_NAME": name, "SPRITE_SANDBOX_ID": sprite._sandbox.object_id},
    )
    stdout = await proc.stdout.read.aio()
    assert "checkpoint" in stdout

    await sprite.destroy()


@pytest.mark.asyncio
async def test_sprite_ctl_checkpoint() -> None:
    """modal-sprite-ctl checkpoint from inside the sandbox."""
    name = _unique_name()
    sprite = await Sprite.create(name, config=SpriteConfig(timeout=300))

    # Write a file
    proc = await sprite._sandbox.exec.aio("bash", "-c", "echo 'ctl-test' > /root/ctl.txt")
    await proc.wait.aio()

    # Run modal-sprite-ctl checkpoint
    proc = await sprite._sandbox.exec.aio(
        "modal-sprite-ctl", "checkpoint", "v1",
        env={"SPRITE_NAME": name, "SPRITE_SANDBOX_ID": sprite._sandbox.object_id},
    )
    stdout = await proc.stdout.read.aio()
    exit_code = await proc.wait.aio()
    assert exit_code == 0
    assert "v1" in stdout

    # Verify checkpoint is in registry
    meta = await sprite._registry.get(name)
    assert "v1" in meta.checkpoints

    await sprite.destroy()


@pytest.mark.asyncio
async def test_sprite_ctl_sleep() -> None:
    """modal-sprite-ctl sleep from inside terminates the sandbox and marks sleeping."""
    name = _unique_name()
    sprite = await Sprite.create(name, config=SpriteConfig(timeout=300))

    # Run modal-sprite-ctl sleep
    proc = await sprite._sandbox.exec.aio(
        "modal-sprite-ctl", "sleep",
        env={"SPRITE_NAME": name, "SPRITE_SANDBOX_ID": sprite._sandbox.object_id},
    )
    # This will terminate the sandbox, so stdout may be partial
    # Just wait for it to finish
    await proc.wait.aio()

    # Verify registry state
    meta = await sprite._registry.get(name)
    assert meta.state == SpriteState.SLEEPING
    assert meta.latest_snapshot_image_id is not None

    # Clean up: delete from registry directly
    await sprite._registry.delete(name)


@pytest.mark.asyncio
async def test_sprite_ctl_upgrade_sets_reconnect() -> None:
    """modal-sprite-ctl upgrade sets pending_action=reconnect and updates config."""
    name = _unique_name()
    sprite = await Sprite.create(name, config=SpriteConfig(timeout=300, memory=512))

    proc = await sprite._sandbox.exec.aio(
        "modal-sprite-ctl", "upgrade", "--memory=2048",
        env={"SPRITE_NAME": name, "SPRITE_SANDBOX_ID": sprite._sandbox.object_id},
    )
    await proc.wait.aio()

    meta = await sprite._registry.get(name)
    assert meta.pending_action == "reconnect"
    assert meta.config.memory == 2048
    assert meta.latest_snapshot_image_id is not None

    # Clean up
    await sprite._registry.delete(name)


@pytest.mark.asyncio
async def test_sprite_ctl_restore_sets_reconnect() -> None:
    """modal-sprite-ctl restore sets the checkpoint image and pending_action=reconnect."""
    name = _unique_name()
    sprite = await Sprite.create(name, config=SpriteConfig(timeout=300))

    # Create a checkpoint first
    proc = await sprite._sandbox.exec.aio(
        "modal-sprite-ctl", "checkpoint", "v1",
        env={"SPRITE_NAME": name, "SPRITE_SANDBOX_ID": sprite._sandbox.object_id},
    )
    await proc.wait.aio()

    meta = await sprite._registry.get(name)
    v1_image_id = meta.checkpoints["v1"]

    # Write something new after checkpoint
    proc = await sprite._sandbox.exec.aio("bash", "-c", "echo 'new' > /root/new.txt")
    await proc.wait.aio()

    # Restore to v1
    proc = await sprite._sandbox.exec.aio(
        "modal-sprite-ctl", "restore", "v1",
        env={"SPRITE_NAME": name, "SPRITE_SANDBOX_ID": sprite._sandbox.object_id},
    )
    await proc.wait.aio()

    meta = await sprite._registry.get(name)
    assert meta.pending_action == "reconnect"
    assert meta.latest_snapshot_image_id == v1_image_id

    # Clean up
    await sprite._registry.delete(name)


# -- Reconnect loop --------------------------------------------------------

@pytest.mark.asyncio
async def test_reconnect_after_upgrade() -> None:
    """Simulate the reconnect loop: upgrade sets reconnect, terminal creates new sandbox."""
    name = _unique_name()
    sprite = await Sprite.create(name, config=SpriteConfig(timeout=300, memory=512))

    # Write marker
    proc = await sprite._sandbox.exec.aio("bash", "-c", "echo 'survive' > /root/marker.txt")
    await proc.wait.aio()

    # Simulate modal-sprite-ctl upgrade from inside
    proc = await sprite._sandbox.exec.aio(
        "modal-sprite-ctl", "upgrade", "--memory=2048",
        env={"SPRITE_NAME": name, "SPRITE_SANDBOX_ID": sprite._sandbox.object_id},
    )
    await proc.wait.aio()

    # Now simulate what the terminal reconnect loop does:
    # read registry, see pending_action=reconnect, create new sandbox
    from modal import Image
    import modal

    meta = await sprite._registry.get(name)
    assert meta.pending_action == "reconnect"
    assert meta.latest_snapshot_image_id is not None

    app = await modal.App.lookup.aio("modal-sprite", create_if_missing=True)
    image = Image.from_id(meta.latest_snapshot_image_id)
    new_sandbox = await sm.create_sandbox(app, meta.config, image=image, sprite_name=name)

    meta.state = SpriteState.RUNNING
    meta.sandbox_id = new_sandbox.object_id
    meta.pending_action = None
    await sprite._registry.put(name, meta)

    # Verify marker persisted and new memory config
    proc = await new_sandbox.exec.aio("cat", "/root/marker.txt")
    stdout = await proc.stdout.read.aio()
    assert "survive" in stdout
    assert meta.config.memory == 2048

    # Clean up
    await new_sandbox.terminate.aio()
    await sprite._registry.delete(name)


@pytest.mark.asyncio
async def test_list_all() -> None:
    """list_all should include a freshly created sprite."""
    name = _unique_name()
    sprite = await Sprite.create(name, config=SpriteConfig(timeout=300))

    all_sprites = await Sprite.list_all()
    assert name in all_sprites

    await sprite.destroy()
