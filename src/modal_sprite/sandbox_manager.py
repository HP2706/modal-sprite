"""Low-level Modal sandbox operations.

Stateless helpers that translate :class:`SpriteConfig` into Modal API calls.
The :class:`Sprite` class orchestrates state transitions on top of these.
"""

from __future__ import annotations

import modal
from modal import Image, Sandbox, Volume
from modal.config import config as modal_config
from modal.secret import Secret

from modal_sprite.config import SpriteConfig
from modal_sprite.sprite_ctl import get_sprite_ctl_source

SPRITE_CTL_PATH = "/usr/local/bin/modal-sprite-ctl"

# Base image with modal, Node.js, Claude Code, and Codex pre-installed
_BASE_IMAGE: Image | None = None


def _get_base_image() -> Image:
    global _BASE_IMAGE
    if _BASE_IMAGE is None:
        _BASE_IMAGE = (
            Image.debian_slim()
            .apt_install("curl", "git")
            .run_commands(
                "curl -fsSL https://deb.nodesource.com/setup_22.x | bash -",
                "apt-get install -y nodejs",
                "npm install -g @anthropic-ai/claude-code @openai/codex",
            )
            .uv_pip_install("modal")
            .env({"IS_SANDBOX": "1"})
        )
    return _BASE_IMAGE


async def create_sandbox(
    app: modal.App,
    config: SpriteConfig,
    *,
    image: Image | None = None,
    sprite_name: str = "",
) -> Sandbox:
    """Create a new Modal sandbox from *config*.

    If *image* is provided (e.g. a snapshot image), it overrides the default
    base.  Injects the ``modal-sprite-ctl`` helper script.
    """
    base_image = image or _get_base_image()

    volumes: dict[str, Volume] = {
        path: Volume.from_name(vol_name, create_if_missing=True)
        for path, vol_name in config.volumes.items()
    }

    # Always inject Modal credentials so modal-sprite-ctl can call the Modal API
    modal_creds = {
        "MODAL_TOKEN_ID": modal_config.get("token_id"),
        "MODAL_TOKEN_SECRET": modal_config.get("token_secret"),
    }

    secrets: list[Secret] = [Secret.from_dict(modal_creds)]
    if config.env_variables:
        secrets.append(Secret.from_dict(config.env_variables))

    sandbox = await Sandbox.create.aio(
        app=app,
        image=base_image,
        timeout=config.timeout,
        idle_timeout=config.idle_timeout,
        cpu=config.cpu,
        memory=config.memory,
        gpu=config.gpu,
        volumes=volumes,
        encrypted_ports=config.encrypted_ports,
        unencrypted_ports=config.unencrypted_ports,
        secrets=secrets if secrets else None,
        workdir=config.workdir,
    )

    # Inject modal-sprite-ctl helper into the sandbox
    await sandbox.filesystem.write_text.aio(get_sprite_ctl_source(), SPRITE_CTL_PATH)
    proc = await sandbox.exec.aio("chmod", "+x", SPRITE_CTL_PATH)
    await proc.wait.aio()

    return sandbox


async def reconnect_sandbox(sandbox_id: str) -> Sandbox | None:
    """Try to reconnect to a running sandbox by *sandbox_id*.

    Returns ``None`` when the sandbox is no longer alive.
    """
    sb = await Sandbox.from_id.aio(sandbox_id)
    if await sb.poll.aio() is None:
        return sb
    return None


async def snapshot_sandbox(sandbox: Sandbox, timeout: int = 120) -> Image:
    """Capture the sandbox's filesystem and return the snapshot image."""
    return await sandbox.snapshot_filesystem.aio(timeout=timeout)


async def terminate_sandbox(sandbox: Sandbox) -> None:
    await sandbox.terminate.aio()
