"""Resource ownership and readiness, independent of application behavior."""

import asyncio
import re
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncContextManager, Awaitable, Callable


@dataclass(frozen=True)
class ManagedResource:
    name: str
    factory: Callable[[], AsyncContextManager[Any]]
    check_ready: Callable[[Any], Awaitable[bool]] | None = None
    required: bool = True

    def __post_init__(self):
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", self.name):
            raise ValueError("Resource names must be safe operational identifiers.")


class ApplicationRuntime:
    def __init__(self, resources, timeout):
        self.specifications = tuple(resources)
        if len({item.name for item in self.specifications}) != len(self.specifications):
            raise ValueError("Resource names must be unique per app.")
        self.timeout = timeout
        self.running = False
        self.active = False
        self.values = {}

    @asynccontextmanager
    async def lifespan(self, app):
        if self.active:
            raise RuntimeError("The application lifespan is already active.")
        self.active = True
        try:
            async with AsyncExitStack() as stack:
                try:
                    for resource in self.specifications:
                        self.values[resource.name] = await stack.enter_async_context(resource.factory())
                except Exception:
                    raise RuntimeError("An application resource could not initialize.") from None
                self.running = True
                try:
                    yield
                finally:
                    self.running = False
        finally:
            self.values.clear()
            self.active = False

    async def readiness(self):
        async def check(resource):
            ready = self.running
            if ready and resource.check_ready is not None:
                try:
                    ready = (await asyncio.wait_for(resource.check_ready(self.values[resource.name]), self.timeout)) is True
                except Exception:
                    ready = False
            return {"name": resource.name, "required": resource.required, "ready": ready}

        checks = list(await asyncio.gather(*(check(resource) for resource in self.specifications)))
        return self.running and all(check["ready"] for check in checks if check["required"]), checks
