"""End-to-end verification of the port forwarder against a real Modal sandbox.

Spins up a sandbox, starts a python http.server on port 8000 inside it,
binds a local forward, fetches via the forward, and tears everything down.

Run with: .venv/bin/python scripts/verify_port_forward.py
"""

from __future__ import annotations

import asyncio
import urllib.request

import modal
from modal.stream_type import StreamType

from modal_sprite import sandbox_manager as sm
from modal_sprite.config import SpriteConfig
from modal_sprite.port_forward import Forward, PortForwarder


async def main() -> None:
    print("Looking up Modal app...")
    app = await modal.App.lookup.aio("modal-sprite-fwd-test", create_if_missing=True)
    cfg = SpriteConfig(timeout=600, idle_timeout=120, cpu=1.0, memory=1024)

    print("Creating sandbox (first run rebuilds the image with socat)...")
    sandbox = await sm.create_sandbox(app, cfg, sprite_name="fwd-test")
    print(f"  sandbox: {sandbox.object_id}")

    try:
        print("Starting python http.server on port 8000 inside the sandbox...")
        await sandbox.exec.aio(
            "python3",
            "-m",
            "http.server",
            "8000",
            stdout=StreamType.DEVNULL,
            stderr=StreamType.DEVNULL,
        )
        # Give the server a moment to bind
        await asyncio.sleep(2.0)

        print("Starting PortForwarder localhost:0 -> sandbox:8000 ...")
        forwarder = PortForwarder(sandbox=sandbox, forwards=[Forward(0, 8000)])
        await forwarder.start()
        bound_port = forwarder._servers[0].sockets[0].getsockname()[1]
        print(f"  bound on 127.0.0.1:{bound_port}")

        loop = asyncio.get_running_loop()

        def _fetch() -> tuple[int, bytes]:
            req = urllib.request.Request(f"http://127.0.0.1:{bound_port}/")
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, resp.read()

        print("Fetching http://127.0.0.1:%d/ ..." % bound_port)
        status, body = await loop.run_in_executor(None, _fetch)
        print(f"  HTTP {status}, {len(body)} bytes")
        preview = body[:200].decode("utf-8", errors="replace").replace("\n", " ")
        print(f"  preview: {preview!r}")

        assert status == 200, f"Expected 200, got {status}"
        assert b"Directory listing" in body, "Body did not look like an http.server index"

        print("\n[OK] Port forward end-to-end works.")

        await forwarder.stop()
    finally:
        print("Terminating sandbox...")
        await sm.terminate_sandbox(sandbox)


if __name__ == "__main__":
    asyncio.run(main())
