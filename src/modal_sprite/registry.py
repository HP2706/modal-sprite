from __future__ import annotations

import modal

from modal_sprite.state import SpriteMetadata

REGISTRY_NAME = "modal-sprite-registry"


class SpriteRegistry:
    """Thin typed wrapper around a ``modal.Dict`` that stores sprite metadata.

    Provides both async and sync methods.  The async variants should be used in
    ``Sprite`` methods; the sync variants exist for the background monitor
    thread and the CLI.
    """

    def __init__(self) -> None:
        self._dict = modal.Dict.from_name(REGISTRY_NAME, create_if_missing=True)

    # ── async ─────────────────────────────────────────────────────────

    async def get(self, name: str) -> SpriteMetadata | None:
        raw = await self._dict.get.aio(name)
        if raw is None:
            return None
        return SpriteMetadata.model_validate(raw)

    async def put(self, name: str, metadata: SpriteMetadata) -> None:
        await self._dict.__setitem__.aio(name, metadata.model_dump(mode="json"))

    async def delete(self, name: str) -> None:
        await self._dict.pop.aio(name)

    async def exists(self, name: str) -> bool:
        return await self._dict.get.aio(name) is not None

    async def list_all(self) -> dict[str, SpriteMetadata]:
        result: dict[str, SpriteMetadata] = {}
        async for key, value in self._dict.items.aio():
            result[key] = SpriteMetadata.model_validate(value)
        return result

    # ── sync (for background threads & CLI) ───────────────────────────

    def get_sync(self, name: str) -> SpriteMetadata | None:
        raw = self._dict.get(name)
        if raw is None:
            return None
        return SpriteMetadata.model_validate(raw)

    def put_sync(self, name: str, metadata: SpriteMetadata) -> None:
        self._dict[name] = metadata.model_dump(mode="json")

    def delete_sync(self, name: str) -> None:
        self._dict.pop(name)

    def list_all_sync(self) -> dict[str, SpriteMetadata]:
        result: dict[str, SpriteMetadata] = {}
        for key, value in self._dict.items():
            result[key] = SpriteMetadata.model_validate(value)
        return result
