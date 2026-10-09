from typing import Protocol


class SystemBackend(Protocol):
    async def health(self) -> dict: ...
