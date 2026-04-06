class SpriteNotFoundError(RuntimeError):
    """Raised when a sprite with the given name does not exist in the registry."""

    def __init__(self, name: str) -> None:
        super().__init__(f"Sprite '{name}' not found")
        self.name = name


class SpriteStateError(RuntimeError):
    """Raised when an operation is invalid for the sprite's current state."""

    def __init__(self, name: str, current_state: str, operation: str) -> None:
        super().__init__(
            f"Cannot {operation} sprite '{name}' in state '{current_state}'"
        )
        self.name = name
        self.current_state = current_state
        self.operation = operation
