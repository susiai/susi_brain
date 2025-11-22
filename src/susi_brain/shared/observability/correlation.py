"""Task-local transport correlation, separate from future inference IDs."""

from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

CURRENT = ContextVar("susi_correlation_id", default=None)


def outbound_headers():
    return {"X-Request-ID": CURRENT.get() or str(uuid4())}


@contextmanager
def correlation_scope(value):
    token = CURRENT.set(value)
    try:
        yield
    finally:
        CURRENT.reset(token)
