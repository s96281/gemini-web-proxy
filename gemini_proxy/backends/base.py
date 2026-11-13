from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Model:
    id: str
    label: str


class Backend(Protocol):
    name: str

    async def models(self) -> list[Model]: ...
    async def prepare(self, model: str, state: dict | None = None) -> None: ...
    def stream(self, prompt: str, model: str, state: dict | None = None) -> AsyncIterator[str]: ...
    async def abort(self) -> None: ...
    async def close(self) -> None: ...
