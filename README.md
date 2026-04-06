# modal-sprite

Persistent, durable cloud computers on [Modal](https://modal.com) -- inspired by [Fly.io Sprites](https://fly.io/blog/code-and-let-live/).

Sprites are long-lived development environments that **sleep when idle** (stop billing), **wake in seconds** with full filesystem state restored, and support **named checkpoints** -- like git for your entire machine.

## How it works

Modal sandboxes are ephemeral by default. modal-sprite makes them persistent by combining three Modal primitives:

- **`Sandbox.create()`** for compute
- **`sandbox.snapshot_filesystem()`** to capture the full filesystem as a `modal.Image`
- **`modal.Dict`** as a cloud-native metadata registry

### Sleep / Wake

```
Sleep:  snapshot_filesystem() -> store image_id -> terminate sandbox
Wake:   Image.from_id(image_id) -> Sandbox.create(image=...) -> new sandbox, same filesystem
```

When you sleep a sprite, its entire filesystem is captured as an image. When you wake it, a new sandbox boots from that image. Your files, installed packages, running configs -- everything is restored.

### Checkpoints

Checkpoints are named snapshots. Take a checkpoint before a risky change, restore to it if things go wrong:

```
checkpoint "before-refactor" -> snapshot + store as {label: image_id}
restore "before-refactor"    -> boot new sandbox from that image
```

### Live upgrades

Need more GPU or memory? `modal-sprite-ctl upgrade --gpu=A10G` snapshots the current state, updates the config, and seamlessly reconnects you to a new sandbox with the upgraded resources. No manual save/restart cycle.

### Architecture

```
                create()
  [nothing] ─────────────> RUNNING
                              |
                sleep()       |     checkpoint(), upgrade()
               ┌──────────────┤     (stay RUNNING)
               |              |
               v              |
            SLEEPING ─────────┘  wake()
               |
               |  destroy()
               v
            DESTROYED  <──── (also from RUNNING)
```

The `Sprite` class manages state transitions. A background monitor thread auto-snapshots ~30s before timeout to prevent state loss. An interactive terminal loop handles reconnection when `modal-sprite-ctl` triggers upgrades or restores from inside the shell.

## Installation

```bash
uv add modal-sprite
```

Requires a [Modal](https://modal.com) account with `modal token set` configured.

## Usage

### CLI

```bash
# Create a sprite and drop into a shell
modal-sprite create my-dev-box

# Reconnect to a running sprite
modal-sprite shell my-dev-box

# Sleep (stops billing, preserves state)
modal-sprite sleep my-dev-box

# Wake it back up
modal-sprite wake my-dev-box

# Fork a sprite from its latest snapshot
modal-sprite clone my-dev-box my-dev-box-2

# Upload / download files
modal-sprite push my-dev-box ./local-file.txt /root/file.txt
modal-sprite pull my-dev-box /root/file.txt ./local-file.txt

# List all sprites
modal-sprite list

# Destroy permanently
modal-sprite destroy my-dev-box
```

### From inside the shell (`modal-sprite-ctl`)

Once you're in a sprite shell, `modal-sprite-ctl` is available for self-service operations:

```bash
# Save a named checkpoint
modal-sprite-ctl checkpoint before-changes

# List checkpoints
modal-sprite-ctl checkpoints

# Upgrade resources (snapshots, reconnects with new config)
modal-sprite-ctl upgrade --gpu=A10G --memory=16384

# Restore to a checkpoint
modal-sprite-ctl restore before-changes

# Put yourself to sleep
modal-sprite-ctl sleep

# View sprite metadata
modal-sprite-ctl status
```

### Python API

```python
from modal_sprite import Sprite, SpriteConfig

# Create
sprite = await Sprite.create("my-box", config=SpriteConfig(
    cpu=4, memory=8192, gpu="T4", timeout=7200
))

# Interactive shell
await sprite.shell()

# Sleep / wake
await sprite.sleep()
await sprite.wake()

# File transfer
await sprite.push("./data.csv", "/root/data.csv")
await sprite.pull("/root/results.json", "./results.json")

# Clone from snapshot
clone = await sprite.clone("my-box-copy")

# Clean up
await sprite.destroy()
```

## Base image

The default sandbox image includes:

- Python + [Modal](https://modal.com) SDK
- Node.js 22
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (`@anthropic-ai/claude-code`)
- [Codex](https://github.com/openai/codex) (`@openai/codex`)
- git, curl

You can also start from a custom base image via `--base-image-id`.

## How it compares to Fly.io Sprites

| Feature | Fly.io Sprites | modal-sprite |
|---------|---------------|--------------|
| Sleep / wake | First-class, ~1s wake | Via `snapshot_filesystem()`, ~5-10s wake |
| Checkpoints | Built-in | Named snapshots stored in `modal.Dict` |
| Live upgrade | Not yet | `modal-sprite-ctl upgrade` |
| Interactive shell | `fly sprite shell` | `modal-sprite shell` (PTY via `process.attach()`) |
| Billing | Paused when sleeping | Zero cost when sleeping (sandbox terminated) |
| GPU support | Limited | Full Modal GPU catalog (T4, A10G, A100, H100, ...) |
| In-sandbox control | `sprite-ctl` | `modal-sprite-ctl` |

## Development

```bash
git clone https://github.com/HP2706/modal-sprite.git
cd modal-sprite
uv sync

# Unit tests (no Modal connection needed)
uv run pytest tests/test_sprite_unit.py -v

# Integration tests (requires Modal credentials)
uv run pytest tests/integration/ --run-integration -v
```

## License

MIT
