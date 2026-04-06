#!/usr/bin/env python3
"""In-sandbox helper for managing the sprite from within an interactive shell.

This script is injected into the sandbox at ``/usr/local/bin/modal-sprite-ctl``.
It reads ``SPRITE_NAME`` and ``SPRITE_SANDBOX_ID`` from the environment and
communicates with the Modal API + the sprite registry (``modal.Dict``) to
perform lifecycle operations.
"""

from __future__ import annotations

SCRIPT_SOURCE = r'''#!/usr/bin/env python3
"""modal-sprite-ctl: manage this sprite from within the shell."""

import json
import os
import sys
from datetime import datetime, timezone
from typing import Optional

import modal
import typer

REGISTRY_NAME = "modal-sprite-registry"

app = typer.Typer(
    name="modal-sprite-ctl",
    help="Manage this sprite from within the interactive shell.",
    no_args_is_help=True,
)


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _get_env() -> tuple[str, str]:
    name = os.environ.get("SPRITE_NAME", "")
    sandbox_id = os.environ.get("SPRITE_SANDBOX_ID", "")
    if not name or not sandbox_id:
        typer.echo("Error: SPRITE_NAME and SPRITE_SANDBOX_ID must be set", err=True)
        raise typer.Exit(1)
    return name, sandbox_id


def _registry() -> modal.Dict:
    return modal.Dict.from_name(REGISTRY_NAME, create_if_missing=True)


def _get_metadata(reg: modal.Dict, name: str) -> dict:
    meta = reg.get(name)
    if meta is None:
        typer.echo(f"Error: sprite '{name}' not found in registry", err=True)
        raise typer.Exit(1)
    return meta


def _save_metadata(reg: modal.Dict, name: str, meta: dict) -> None:
    reg[name] = meta


@app.command()
def checkpoint(label: str = typer.Argument(help="Name for the checkpoint")) -> None:
    """Create a named snapshot of the filesystem."""
    name, sandbox_id = _get_env()
    reg = _registry()
    meta = _get_metadata(reg, name)

    typer.echo("Snapshotting filesystem...")
    sb = modal.Sandbox.from_id(sandbox_id)
    image = sb.snapshot_filesystem(timeout=120)

    meta["checkpoints"][label] = image.object_id
    meta["latest_snapshot_image_id"] = image.object_id
    meta["last_activity_at"] = _now()
    _save_metadata(reg, name, meta)

    typer.echo(f"Checkpoint '{label}' saved. (image: {image.object_id})")


@app.command()
def checkpoints() -> None:
    """List all named checkpoints."""
    name, _ = _get_env()
    reg = _registry()
    meta = _get_metadata(reg, name)

    cps = meta.get("checkpoints", {})
    if not cps:
        typer.echo("No checkpoints.")
        return
    for cp_label, image_id in cps.items():
        typer.echo(f"  {cp_label:20s}  {image_id}")


@app.command()
def sleep() -> None:
    """Snapshot the filesystem and put the sprite to sleep."""
    name, sandbox_id = _get_env()
    reg = _registry()
    meta = _get_metadata(reg, name)

    typer.echo("Snapshotting filesystem...")
    sb = modal.Sandbox.from_id(sandbox_id)
    image = sb.snapshot_filesystem(timeout=120)

    meta["state"] = "sleeping"
    meta["sandbox_id"] = None
    meta["latest_snapshot_image_id"] = image.object_id
    meta["pending_action"] = None
    meta["last_activity_at"] = _now()
    _save_metadata(reg, name, meta)

    typer.echo("Sprite is now sleeping. Goodbye.")
    sb.terminate()


@app.command()
def upgrade(
    gpu: Optional[str] = typer.Option(None, help="GPU type (e.g. T4, A10G, A100)"),
    memory: Optional[int] = typer.Option(None, help="Memory in MB"),
    cpu: Optional[float] = typer.Option(None, help="CPU cores"),
    timeout: Optional[int] = typer.Option(None, help="Max lifetime in seconds"),
    idle_timeout: Optional[int] = typer.Option(None, help="Idle timeout in seconds"),
    add_volume: Optional[str] = typer.Option(None, help="Add volume as path:name"),
) -> None:
    """Change resource allocation. Snapshots and reconnects with new config."""
    name, sandbox_id = _get_env()
    reg = _registry()
    meta = _get_metadata(reg, name)

    overrides: dict = {}
    if gpu is not None:
        overrides["gpu"] = gpu if gpu.lower() != "none" else None
    if memory is not None:
        overrides["memory"] = memory
    if cpu is not None:
        overrides["cpu"] = cpu
    if timeout is not None:
        overrides["timeout"] = timeout
    if idle_timeout is not None:
        overrides["idle_timeout"] = idle_timeout
    if add_volume is not None:
        path, vol_name = add_volume.split(":", 1)
        overrides["volumes"] = {path: vol_name}

    if not overrides:
        typer.echo("No changes specified. Use --help to see available options.", err=True)
        raise typer.Exit(1)

    # Merge overrides into config
    config = meta["config"]
    for key, value in overrides.items():
        if key == "volumes":
            config["volumes"] = {**config.get("volumes", {}), **value}
        else:
            config[key] = value
    meta["config"] = config

    typer.echo("Snapshotting filesystem...")
    sb = modal.Sandbox.from_id(sandbox_id)
    image = sb.snapshot_filesystem(timeout=120)

    meta["latest_snapshot_image_id"] = image.object_id
    meta["pending_action"] = "reconnect"
    meta["last_activity_at"] = _now()
    _save_metadata(reg, name, meta)

    summary = ", ".join(f"{k}={v}" for k, v in overrides.items())
    typer.echo(f"Upgrading ({summary}) and reconnecting...")
    sb.terminate()


@app.command()
def restore(label: str = typer.Argument(help="Checkpoint label to restore")) -> None:
    """Restore to a named checkpoint. Reconnects with that checkpoint's filesystem."""
    name, sandbox_id = _get_env()
    reg = _registry()
    meta = _get_metadata(reg, name)

    cps = meta.get("checkpoints", {})
    if label not in cps:
        available = ", ".join(cps.keys()) if cps else "(none)"
        typer.echo(f"Error: checkpoint '{label}' not found. Available: {available}", err=True)
        raise typer.Exit(1)

    meta["latest_snapshot_image_id"] = cps[label]
    meta["pending_action"] = "reconnect"
    meta["last_activity_at"] = _now()
    _save_metadata(reg, name, meta)

    typer.echo(f"Restoring to checkpoint '{label}' and reconnecting...")
    sb = modal.Sandbox.from_id(sandbox_id)
    sb.terminate()


@app.command()
def status() -> None:
    """Show current sprite metadata."""
    name, _ = _get_env()
    reg = _registry()
    meta = _get_metadata(reg, name)
    typer.echo(json.dumps(meta, indent=2))


if __name__ == "__main__":
    app()
'''


def get_sprite_ctl_source() -> str:
    """Return the source code of the modal-sprite-ctl script."""
    return SCRIPT_SOURCE
