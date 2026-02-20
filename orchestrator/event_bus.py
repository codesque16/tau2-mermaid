import asyncio
from dataclasses import dataclass
from enum import Enum


class Role(Enum):
    USER = "user"
    ASSISTANT = "assistant"


@dataclass
class Message:
    role: Role
    content: str


class EventBus:
    """Two async queues connecting user and assistant agents."""

    def __init__(self) -> None:
        self.to_assistant: asyncio.Queue[str] = asyncio.Queue()
        self.to_user: asyncio.Queue[str] = asyncio.Queue()

    async def send_to_assistant(self, content: str) -> None:
        await self.to_assistant.put(content)

    async def send_to_user(self, content: str) -> None:
        await self.to_user.put(content)
