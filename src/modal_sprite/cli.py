"""CLI for modal-sprite -- shell-first persistent cloud computers."""

import asyncio
import json
from typing import Optional

import typer

from modal_sprite.config import SpriteConfig
from modal_sprite.sprite import Sprite

app = typer.Typer(
    name="modal-sprite",
    help="Persistent, durable cloud computers on Modal.",
    no_args_is_help=True,
)


def _run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


@app.command()
def create(
    name: str = typer.Argument(help="Name for the new sprite"),
    cpu: float = typer.Option(2.0, help="CPU cores"),
    memory: int = typer.Option(2048, help="Memory in MB"),
    gpu: Optional[str] = typer.Option(None, help="GPU type (e.g. T4, A10G, A100)"),
    timeout: int = typer.Option(3600, help="Max lifetime in seconds"),
    idle_timeout: int = typer.Option(300, help="Idle timeout in seconds"),
    workdir: str = typer.Option("/root", help="Working directory"),
    base_image_id: Optional[str] = typer.Option(None, help="Base image ID to start from"),
    detach: bool = typer.Option(False, help="Create without opening a shell"),
) -> None:
    """Create a new sprite and drop into an interactive shell."""
    cfg = SpriteConfig(
        cpu=cpu,
        memory=memory,
        gpu=gpu,
        timeout=timeout,
        idle_timeout=idle_timeout,
        workdir=workdir,
    )

    async def _do() -> None:
        sprite = await Sprite.create(name, config=cfg, base_image_id=base_image_id)
        if detach:
            typer.echo(f"Sprite '{name}' created (sandbox: {sprite._metadata.sandbox_id})")
            return
        typer.echo(f"Sprite '{name}' created. Connecting...")
        await sprite.shell()

    _run(_do())


@app.command()
def shell(name: str = typer.Argument(help="Sprite name")) -> None:
    """Open an interactive shell. Wakes the sprite if sleeping."""

    async def _do() -> None:
        sprite = await Sprite.get(name)
        if sprite.status == "sleeping":
            typer.echo(f"Waking sprite '{name}'...")
            await sprite.wake()
        await sprite.shell()

    _run(_do())


@app.command()
def sleep(name: str = typer.Argument(help="Sprite name")) -> None:
    """Snapshot and put a sprite to sleep."""

    async def _do() -> None:
        sprite = await Sprite.get(name)
        await sprite.sleep()

    _run(_do())
    typer.echo(f"Sprite '{name}' is now sleeping.")


@app.command()
def wake(name: str = typer.Argument(help="Sprite name")) -> None:
    """Wake a sleeping sprite without opening a shell."""

    async def _do() -> None:
        sprite = await Sprite.get(name)
        await sprite.wake()

    _run(_do())
    typer.echo(f"Sprite '{name}' is awake.")


@app.command()
def push(
    name: str = typer.Argument(help="Sprite name"),
    local_path: str = typer.Argument(help="Local file path"),
    remote_path: str = typer.Argument(help="Destination path in sandbox"),
) -> None:
    """Upload a local file into a sprite."""

    async def _do() -> None:
        sprite = await Sprite.get(name)
        if sprite.status == "sleeping":
            typer.echo(f"Waking sprite '{name}'...")
            await sprite.wake()
        await sprite.push(local_path, remote_path)

    _run(_do())
    typer.echo(f"Pushed {local_path} -> {remote_path}")


@app.command()
def pull(
    name: str = typer.Argument(help="Sprite name"),
    remote_path: str = typer.Argument(help="File path in sandbox"),
    local_path: str = typer.Argument(help="Local destination path"),
) -> None:
    """Download a file from a sprite."""

    async def _do() -> None:
        sprite = await Sprite.get(name)
        if sprite.status == "sleeping":
            typer.echo(f"Waking sprite '{name}'...")
            await sprite.wake()
        await sprite.pull(remote_path, local_path)

    _run(_do())
    typer.echo(f"Pulled {remote_path} -> {local_path}")


@app.command()
def clone(
    name: str = typer.Argument(help="Source sprite name"),
    new_name: str = typer.Argument(help="Name for the clone"),
    detach: bool = typer.Option(False, help="Clone without opening a shell"),
) -> None:
    """Fork a sprite from its latest snapshot."""

    async def _do() -> None:
        sprite = await Sprite.get(name)
        new_sprite = await sprite.clone(new_name)
        if detach:
            typer.echo(
                f"Cloned '{name}' -> '{new_name}' (sandbox: {new_sprite._metadata.sandbox_id})"
            )
            return
        typer.echo(f"Cloned '{name}' -> '{new_name}'. Connecting...")
        await new_sprite.shell()

    _run(_do())


@app.command()
def checkpoint(
    name: str = typer.Argument(help="Sprite name"),
    label: str = typer.Argument(help="Checkpoint label"),
) -> None:
    """Create a named checkpoint (prefer modal-sprite-ctl inside the shell)."""

    async def _do() -> None:
        from modal_sprite import sandbox_manager as sm

        sprite = await Sprite.get(name)
        assert sprite._sandbox is not None, "Sprite must be running"
        image = await sm.snapshot_sandbox(sprite._sandbox)
        sprite._metadata.checkpoints[label] = image.object_id
        sprite._metadata.latest_snapshot_image_id = image.object_id
        await sprite._registry.put(name, sprite._metadata)

    _run(_do())
    typer.echo(f"Checkpoint '{label}' created for sprite '{name}'.")


@app.command("list")
def list_sprites() -> None:
    """List all sprites."""
    sprites = Sprite.list_all_sync()
    if not sprites:
        typer.echo("No sprites found.")
        return
    for sname, meta in sprites.items():
        typer.echo(f"  {sname:20s}  {meta.state:10s}  created={meta.created_at}")


@app.command()
def status(name: str = typer.Argument(help="Sprite name")) -> None:
    """Show detailed status for a sprite."""
    from modal_sprite.registry import SpriteRegistry

    meta = SpriteRegistry().get_sync(name)
    if meta is None:
        typer.echo(f"Sprite '{name}' not found.", err=True)
        raise typer.Exit(1)
    typer.echo(json.dumps(meta.model_dump(), indent=2))


@app.command()
def destroy(name: str = typer.Argument(help="Sprite name")) -> None:
    """Permanently destroy a sprite."""

    async def _do() -> None:
        sprite = await Sprite.get(name)
        await sprite.destroy()

    _run(_do())
    typer.echo(f"Sprite '{name}' destroyed.")


def main() -> None:
    app()
