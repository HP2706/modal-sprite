"""Interactive shell session with reconnect-on-pending-action loop.

The host-side terminal handler:
1. Opens an interactive PTY shell on the sandbox via ``process.attach()``
2. When the shell exits or the sandbox dies, checks the registry
3. If ``pending_action == "reconnect"``, creates a new sandbox from the
   latest snapshot + updated config and loops back to step 1
4. Otherwise exits cleanly
"""

from __future__ import annotations

import logging
import sys
import time

from modal import Image

from modal_sprite import sandbox_manager as sm
from modal_sprite.monitor import SpriteMonitor
from modal_sprite.registry import SpriteRegistry
from modal_sprite.state import SpriteState

logger = logging.getLogger(__name__)


def _make_shell_monitor(
    sandbox: object,
    timeout: int,
    registry: SpriteRegistry,
    sprite_name: str,
    started_at: float,
) -> SpriteMonitor:
    """Create a snapshot-only monitor for use during an interactive shell.

    The on_expiry callback is a no-op because the shell loop handles
    sandbox death and reconnection itself.
    """

    def _on_snapshot(image: Image) -> None:
        meta = registry.get_sync(sprite_name)
        if meta is not None:
            meta.latest_snapshot_image_id = image.object_id
            registry.put_sync(sprite_name, meta)

    def _on_expiry() -> None:
        logger.info("[shell-monitor] Sandbox timeout reached for '%s'", sprite_name)

    return SpriteMonitor(
        sandbox=sandbox,
        timeout=timeout,
        on_snapshot=_on_snapshot,
        on_expiry=_on_expiry,
        started_at=started_at,
    )


async def run_shell_loop(
    sprite_name: str,
    registry: SpriteRegistry,
    app: object,
) -> None:
    """Run the interactive shell with automatic reconnection.

    This is the core loop that ``sprite create``, ``sprite shell``, etc. call.
    """
    while True:
        metadata = await registry.get(sprite_name)
        assert metadata is not None, f"Sprite '{sprite_name}' not found"

        # If sleeping, wake it
        if metadata.state == SpriteState.SLEEPING:
            assert metadata.latest_snapshot_image_id is not None
            image = Image.from_id(metadata.latest_snapshot_image_id)
            sandbox_started_at = time.time()
            sandbox = await sm.create_sandbox(app, metadata.config, image=image)
            metadata.state = SpriteState.RUNNING
            metadata.sandbox_id = sandbox.object_id
            metadata.sandbox_started_at = sandbox_started_at
            metadata.pending_action = None
            await registry.put(sprite_name, metadata)
        else:
            # RUNNING -- reconnect to existing sandbox
            assert metadata.sandbox_id is not None
            sb = await sm.reconnect_sandbox(metadata.sandbox_id)
            if sb is None:
                # Sandbox died unexpectedly, try to wake from snapshot
                if metadata.latest_snapshot_image_id:
                    print("Sandbox died, restoring from last snapshot...")
                    image = Image.from_id(metadata.latest_snapshot_image_id)
                    sandbox_started_at = time.time()
                    sandbox = await sm.create_sandbox(app, metadata.config, image=image)
                    metadata.state = SpriteState.RUNNING
                    metadata.sandbox_id = sandbox.object_id
                    metadata.sandbox_started_at = sandbox_started_at
                    metadata.pending_action = None
                    await registry.put(sprite_name, metadata)
                else:
                    print("Sandbox died and no snapshot available.", file=sys.stderr)
                    return
            else:
                sandbox = sb

        # Update env vars so modal-sprite-ctl inside knows its identity
        env = {
            "SPRITE_NAME": sprite_name,
            "SPRITE_SANDBOX_ID": sandbox.object_id,
        }

        # Start a snapshot-only monitor during the interactive session
        meta_for_monitor = await registry.get(sprite_name)
        monitor = _make_shell_monitor(
            sandbox=sandbox,
            timeout=meta_for_monitor.config.timeout if meta_for_monitor else 3600,
            registry=registry,
            sprite_name=sprite_name,
            started_at=meta_for_monitor.sandbox_started_at or time.time(),
        )
        monitor.start()

        # Open interactive PTY shell
        process = await sandbox.exec.aio(
            "bash",
            pty=True,
            env=env,
        )
        await process.attach.aio()

        # Stop the monitor now that the shell has exited
        monitor.stop()

        # Shell exited -- check registry for pending action
        metadata = await registry.get(sprite_name)
        if metadata is None:
            # Sprite was destroyed from another terminal
            print("Sprite was destroyed.")
            return

        if metadata.pending_action == "reconnect":
            # modal-sprite-ctl requested a reconnect (upgrade/restore)
            # The old sandbox was terminated by modal-sprite-ctl.
            # Create a new one from the latest snapshot + updated config.
            assert metadata.latest_snapshot_image_id is not None
            image = Image.from_id(metadata.latest_snapshot_image_id)
            sandbox_started_at = time.time()
            sandbox = await sm.create_sandbox(app, metadata.config, image=image)

            metadata.state = SpriteState.RUNNING
            metadata.sandbox_id = sandbox.object_id
            metadata.sandbox_started_at = sandbox_started_at
            metadata.pending_action = None
            await registry.put(sprite_name, metadata)

            print("Reconnected.")
            continue

        if metadata.state == SpriteState.SLEEPING:
            # modal-sprite-ctl sleep was called
            print("Sprite is sleeping.")
            return

        # Normal exit (user typed 'exit' or Ctrl-D).
        # Verify the sandbox is actually still alive before claiming it's running.
        still_alive = await sandbox.poll.aio() is None
        if still_alive:
            print(f"Disconnected. Sprite '{sprite_name}' is still running.")
            print(f"  Reconnect:  modal-sprite shell {sprite_name}")
            print(f"  Sleep:      modal-sprite sleep {sprite_name}")
        else:
            # Sandbox died while we were attached
            if metadata.latest_snapshot_image_id:
                print(f"Sandbox for '{sprite_name}' terminated. Last snapshot is preserved.")
                metadata.state = SpriteState.SLEEPING
                metadata.sandbox_id = None
                await registry.put(sprite_name, metadata)
            else:
                print(f"Sandbox for '{sprite_name}' terminated with no snapshot.", file=sys.stderr)
                metadata.state = SpriteState.SLEEPING
                metadata.sandbox_id = None
                await registry.put(sprite_name, metadata)
        return
