"""Unit tests for the port forwarder.

The relay itself is exercised end-to-end against a fake sandbox that
simulates ``socat - TCP:localhost:PORT`` with a pair of in-process queues.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import pytest

from modal_sprite.port_forward import (
    CHUNK_SIZE,
    Forward,
    PortForwarder,
    parse_forward,
)


# ---------------------------------------------------------------------------
# parse_forward
# ---------------------------------------------------------------------------


class TestParseForward:
    def test_single_port(self) -> None:
        assert parse_forward("8000") == Forward(local_port=8000, remote_port=8000)

    def test_local_remote(self) -> None:
        assert parse_forward("8000:3000") == Forward(local_port=8000, remote_port=3000)

    def test_invalid_too_many_parts(self) -> None:
        with pytest.raises(ValueError, match="Invalid forward spec"):
            parse_forward("1:2:3")

    def test_invalid_non_numeric(self) -> None:
        with pytest.raises(ValueError):
            parse_forward("abc")


# ---------------------------------------------------------------------------
# Relay end-to-end (with a fake sandbox.exec)
# ---------------------------------------------------------------------------


class _FakeStdout:
    """Async-iterable stdout backed by a queue of byte chunks."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    def push(self, chunk: bytes) -> None:
        self._queue.put_nowait(chunk)

    def close(self) -> None:
        self._queue.put_nowait(None)

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        return item


class _FakeStdin:
    """Stdin sink that mirrors Modal's StreamWriter: sync write/write_eof, async drain.

    Echoes the buffered bytes back through the paired stdout when EOF is drained,
    so the relay's bidirectional pumps can be exercised end-to-end.
    """

    def __init__(self, paired_stdout: _FakeStdout) -> None:
        self._paired = paired_stdout
        self._buffer: list[bytes] = []
        self.received: list[bytes] = []
        self.eof = False
        # drain is async-via-synchronicity (.aio()); write/write_eof are sync.
        self.drain = MagicMock(aio=self._drain_aio)

    def write(self, data: bytes) -> None:
        self._buffer.append(bytes(data))

    def write_eof(self) -> None:
        self.eof = True

    async def _drain_aio(self) -> None:
        # Flush buffered writes
        for chunk in self._buffer:
            self.received.append(chunk)
        self._buffer.clear()
        # On EOF drain, echo received bytes back and close stdout
        if self.eof:
            for chunk in self.received:
                self._paired.push(chunk)
            self._paired.close()


class _FakeProc:
    def __init__(self) -> None:
        self.stdout = _FakeStdout()
        self.stdin = _FakeStdin(self.stdout)
        self.wait = MagicMock(aio=self._wait)

    async def _wait(self) -> int:
        return 0


class _FakeSandbox:
    def __init__(self) -> None:
        self.exec_calls: list[tuple[str, ...]] = []
        self.exec = MagicMock(aio=self._exec)

    async def _exec(self, *args: str, **_kwargs: object) -> _FakeProc:
        self.exec_calls.append(args)
        return _FakeProc()


@pytest.mark.asyncio
async def test_relay_echoes_bytes_through_fake_sandbox() -> None:
    """A connection on the local port should round-trip bytes through the fake exec."""
    sandbox = _FakeSandbox()
    fwd = Forward(local_port=0, remote_port=8000)  # 0 = let OS pick
    forwarder = PortForwarder(sandbox=sandbox, forwards=[fwd])  # type: ignore[arg-type]

    # Bind on an OS-assigned port so tests don't collide
    await forwarder.start()
    try:
        bound_port = forwarder._servers[0].sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", bound_port)
        writer.write(b"hello world")
        await writer.drain()
        writer.write_eof()  # signal local-side EOF -> triggers stdin EOF -> fake echoes back
        echoed = await reader.read()
        assert echoed == b"hello world"
        writer.close()
        await writer.wait_closed()
    finally:
        await forwarder.stop()

    assert sandbox.exec_calls == [("socat", "-", "TCP:localhost:8000")]


@pytest.mark.asyncio
async def test_start_rolls_back_on_partial_failure() -> None:
    """If the second listener can't bind, the first one should be cleaned up."""
    sandbox = _FakeSandbox()

    # Bind a real socket on an OS-assigned port to create a guaranteed conflict.
    blocker = await asyncio.start_server(lambda r, w: None, host="127.0.0.1", port=0)
    blocked_port = blocker.sockets[0].getsockname()[1]

    forwarder = PortForwarder(
        sandbox=sandbox,  # type: ignore[arg-type]
        forwards=[Forward(local_port=0, remote_port=8000), Forward(local_port=blocked_port, remote_port=9000)],
    )

    try:
        with pytest.raises(RuntimeError, match="Failed to bind"):
            await forwarder.start()
        # Cleanup should have closed any partially-bound listeners
        assert forwarder._servers == []
    finally:
        blocker.close()
        await blocker.wait_closed()


@pytest.mark.asyncio
async def test_stop_is_idempotent_with_no_forwards() -> None:
    sandbox = _FakeSandbox()
    forwarder = PortForwarder(sandbox=sandbox, forwards=[])  # type: ignore[arg-type]
    await forwarder.start()
    await forwarder.stop()
    await forwarder.stop()  # second call should not raise


def test_chunk_size_is_reasonable() -> None:
    assert CHUNK_SIZE >= 4096
