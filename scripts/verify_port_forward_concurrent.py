"""Concurrent end-to-end check for the port forwarder.

Spins up a sandbox with TWO HTTP servers on different ports (each serving
a distinguishable file), binds TWO forwards, and fires N concurrent
requests across both. Verifies every response is correct (no cross-talk,
no truncation, no drops).

Run with: .venv/bin/python scripts/verify_port_forward_concurrent.py
"""

from __future__ import annotations

import asyncio
import urllib.request

import modal
from modal.stream_type import StreamType

from modal_sprite import sandbox_manager as sm
from modal_sprite.config import SpriteConfig
from modal_sprite.port_forward import Forward, PortForwarder

NUM_REQUESTS_PER_PORT = 20


async def main() -> None:
    print("Looking up Modal app...")
    app = await modal.App.lookup.aio("modal-sprite-fwd-test", create_if_missing=True)
    cfg = SpriteConfig(timeout=600, idle_timeout=120, cpu=1.0, memory=1024)

    print("Creating sandbox...")
    sandbox = await sm.create_sandbox(app, cfg, sprite_name="fwd-concurrent")
    print(f"  sandbox: {sandbox.object_id}")

    try:
        # Create two distinct files in two directories so each server returns
        # a uniquely identifiable response.
        print("Setting up two directories with distinguishable content...")
        setup = await sandbox.exec.aio(
            "bash",
            "-c",
            (
                "mkdir -p /srv/a /srv/b && "
                "echo 'I AM SERVER A' > /srv/a/marker.txt && "
                "echo 'I AM SERVER B' > /srv/b/marker.txt"
            ),
        )
        await setup.wait.aio()

        print("Starting two http.servers (port 8000 -> /srv/a, port 8001 -> /srv/b)...")
        await sandbox.exec.aio(
            "bash",
            "-c",
            "cd /srv/a && nohup python3 -m http.server 8000 >/tmp/a.log 2>&1 &",
            stdout=StreamType.DEVNULL,
            stderr=StreamType.DEVNULL,
        )
        await sandbox.exec.aio(
            "bash",
            "-c",
            "cd /srv/b && nohup python3 -m http.server 8001 >/tmp/b.log 2>&1 &",
            stdout=StreamType.DEVNULL,
            stderr=StreamType.DEVNULL,
        )
        await asyncio.sleep(2.0)

        # Sanity check from inside the sandbox
        for port, expected in [(8000, b"I AM SERVER A"), (8001, b"I AM SERVER B")]:
            p = await sandbox.exec.aio(
                "curl", "-sS", f"http://localhost:{port}/marker.txt"
            )
            body = await p.stdout.read.aio()
            assert expected in body.encode() if isinstance(body, str) else expected in body, (
                f"Sandbox-internal sanity check failed for port {port}: {body!r}"
            )
        print("  in-sandbox sanity checks pass")

        print("Binding two forwards (localhost:0 -> sandbox:8000, localhost:0 -> sandbox:8001)...")
        forwarder = PortForwarder(
            sandbox=sandbox,
            forwards=[Forward(0, 8000), Forward(0, 8001)],
        )
        await forwarder.start()
        port_a = forwarder._servers[0].sockets[0].getsockname()[1]
        port_b = forwarder._servers[1].sockets[0].getsockname()[1]
        print(f"  forward A: 127.0.0.1:{port_a} -> sandbox:8000")
        print(f"  forward B: 127.0.0.1:{port_b} -> sandbox:8001")

        loop = asyncio.get_running_loop()

        def _fetch(local_port: int) -> tuple[int, bytes]:
            req = urllib.request.Request(f"http://127.0.0.1:{local_port}/marker.txt")
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, resp.read()

        async def _hit(local_port: int, expected: bytes, idx: int) -> tuple[int, bool, str]:
            try:
                status, body = await loop.run_in_executor(None, _fetch, local_port)
                ok = status == 200 and expected in body
                detail = "" if ok else f"status={status} body={body[:60]!r}"
                return idx, ok, detail
            except Exception as e:
                return idx, False, f"exception: {type(e).__name__}: {e}"

        print(
            f"\nFiring {NUM_REQUESTS_PER_PORT} concurrent requests against EACH "
            f"forward ({2 * NUM_REQUESTS_PER_PORT} total)..."
        )
        tasks: list[asyncio.Task[tuple[int, bool, str]]] = []
        for i in range(NUM_REQUESTS_PER_PORT):
            tasks.append(
                asyncio.create_task(_hit(port_a, b"I AM SERVER A", i)),
            )
            tasks.append(
                asyncio.create_task(_hit(port_b, b"I AM SERVER B", i + 1000)),
            )

        results = await asyncio.gather(*tasks)
        ok_count = sum(1 for _, ok, _ in results if ok)
        fail_count = len(results) - ok_count

        print(f"\nResults: {ok_count} OK / {fail_count} FAIL out of {len(results)}")
        if fail_count:
            print("Failures:")
            for idx, ok, detail in results:
                if not ok:
                    print(f"  req #{idx}: {detail}")
            raise SystemExit(1)

        print("\n[OK] Concurrent multi-forward end-to-end works.")

        await forwarder.stop()
    finally:
        print("\nTerminating sandbox...")
        await sm.terminate_sandbox(sandbox)


if __name__ == "__main__":
    asyncio.run(main())
