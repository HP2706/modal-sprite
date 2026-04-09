"""TCP port forwarding from host to a running sprite sandbox.

SSH ``-L``-style local port forwarding with no public internet exposure.
Each accepted local connection spawns a ``socat - TCP:localhost:PORT``
process inside the sandbox and pipes bytes bidirectionally through the
exec stdin/stdout streams.

The forwarder is tied to the lifetime of an attach session: ``start()``
binds local listeners, ``stop()`` closes them and cancels in-flight
relays.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass

from modal import Sandbox
from modal.stream_type import StreamType

logger = logging.getLogger(__name__)

CHUNK_SIZE = 65536


@dataclass(frozen=True)
class Forward:
    """A single local→remote TCP port forward."""

    local_port: int
    remote_port: int


def parse_forward(spec: str) -> Forward:
    """Parse a ``-L`` spec: ``LOCAL`` or ``LOCAL:REMOTE``."""
    parts = spec.split(":")
    if len(parts) == 1:
        port = int(parts[0])
        return Forward(local_port=port, remote_port=port)
    if len(parts) == 2:
        return Forward(local_port=int(parts[0]), remote_port=int(parts[1]))
    raise ValueError(
        f"Invalid forward spec '{spec}'. Use LOCAL or LOCAL:REMOTE (e.g. 8000 or 8000:3000)."
    )


class PortForwarder:
    """Runs a set of TCP forwards for the lifetime of an attach session.

    Usage::

        forwarder = PortForwarder(sandbox, [Forward(8000, 8000)])
        await forwarder.start()
        try:
            ...  # interactive session
        finally:
            await forwarder.stop()
    """

    def __init__(self, sandbox: Sandbox, forwards: list[Forward]) -> None:
        self._sandbox = sandbox
        self._forwards = forwards
        self._servers: list[asyncio.base_events.Server] = []
        self._active_tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        """Bind local listeners for every forward. Rolls back on partial failure."""
        try:
            for fwd in self._forwards:
                server = await asyncio.start_server(
                    lambda r, w, rp=fwd.remote_port: self._handle_connection(r, w, rp),
                    host="127.0.0.1",
                    port=fwd.local_port,
                )
                self._servers.append(server)
                print(
                    f"Forwarding localhost:{fwd.local_port} -> sandbox:{fwd.remote_port}"
                )
        except OSError as e:
            await self.stop()
            raise RuntimeError(
                f"Failed to bind local port for forward: {e}. "
                "Is the port already in use?"
            ) from e

    async def stop(self) -> None:
        """Close listeners and cancel any in-flight relays."""
        for server in self._servers:
            server.close()
        for server in self._servers:
            with contextlib.suppress(Exception):
                await server.wait_closed()
        self._servers.clear()

        for task in list(self._active_tasks):
            task.cancel()
        if self._active_tasks:
            await asyncio.gather(*self._active_tasks, return_exceptions=True)
        self._active_tasks.clear()

    async def _handle_connection(
        self,
        local_reader: asyncio.StreamReader,
        local_writer: asyncio.StreamWriter,
        remote_port: int,
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._active_tasks.add(task)
        try:
            await self._relay(local_reader, local_writer, remote_port)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Port forward relay error (remote :%d): %s", remote_port, e)
        finally:
            if task is not None:
                self._active_tasks.discard(task)

    async def _relay(
        self,
        local_reader: asyncio.StreamReader,
        local_writer: asyncio.StreamWriter,
        remote_port: int,
    ) -> None:
        """Pump bytes between a local socket and an in-sandbox socat process."""
        proc = await self._sandbox.exec.aio(
            "socat",
            "-",
            f"TCP:localhost:{remote_port}",
            text=False,
            stderr=StreamType.DEVNULL,
        )

        async def pump_to_remote() -> None:
            try:
                while True:
                    data = await local_reader.read(CHUNK_SIZE)
                    if not data:
                        break
                    # write() and write_eof() are sync (buffer ops); only drain() flushes.
                    proc.stdin.write(data)
                    await proc.stdin.drain.aio()
            except (ConnectionResetError, BrokenPipeError):
                pass
            finally:
                with contextlib.suppress(Exception):
                    proc.stdin.write_eof()
                    await proc.stdin.drain.aio()

        async def pump_to_local() -> None:
            try:
                async for chunk in proc.stdout:
                    local_writer.write(chunk)
                    await local_writer.drain()
            except (ConnectionResetError, BrokenPipeError):
                pass

        try:
            await asyncio.gather(pump_to_remote(), pump_to_local())
        finally:
            with contextlib.suppress(Exception):
                local_writer.close()
                await local_writer.wait_closed()
            with contextlib.suppress(Exception):
                await proc.wait.aio()
