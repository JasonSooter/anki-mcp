"""Single-threaded ownership of the Anki collection.

Two facts about the `anki` package drive this whole module (both verified
against anki 26.8.1):

1. Opening ``collection.anki2`` takes an exclusive lock. A second open raises
   ``DBError`` -- from another process *and* from the same process.
2. The Rust backend is not safe to drive concurrently from an async server.

So the collection is opened once, on one dedicated thread, and every access is
funnelled through it. Handlers submit a callable and await the result. This
makes the lock story correct by construction: there is nowhere else in the
process that could touch the file.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import TypeVar

from anki.collection import Collection

from .errors import CollectionLockedError, describe_open_failure, translate

log = logging.getLogger(__name__)

T = TypeVar("T")


class CollectionWorker:
    """Owns the one ``Collection`` instance and the one thread allowed to use it."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._col: Collection | None = None
        self._thread_id: int | None = None
        # max_workers=1 gives us a single persistent thread and a work queue
        # for free. The thread identity is asserted on every call so a future
        # refactor can't quietly introduce a second one.
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="anki-col")

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        """Open the collection on the worker thread, or raise.

        Failure here is fatal by design: the caller exits the process rather
        than serving requests against a collection that isn't there.
        """
        try:
            self._pool.submit(self._open_on_thread).result()
        except Exception as exc:  # noqa: BLE001 -- re-raised with context
            raise CollectionLockedError(
                describe_open_failure(str(self._path), exc)
            ) from exc
        log.info("opened collection at %s", self._path)

    def _open_on_thread(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._thread_id = threading.get_ident()
        # Collection() creates the file if absent, which is what we want for a
        # first run that will then full-download from AnkiWeb.
        self._col = Collection(str(self._path))

    def close(self) -> None:
        if self._col is not None:
            self._pool.submit(self._close_on_thread).result()
        self._pool.shutdown(wait=True)
        log.info("closed collection")

    def _close_on_thread(self) -> None:
        assert self._col is not None
        self._col.close()
        self._col = None

    @property
    def is_open(self) -> bool:
        return self._col is not None

    @property
    def path(self) -> Path:
        return self._path

    # -- access ------------------------------------------------------------

    def _call(self, fn: Callable[[Collection], T]) -> T:
        if self._col is None:
            raise RuntimeError("collection is not open")
        assert threading.get_ident() == self._thread_id, (
            "collection touched from the wrong thread -- this is a bug; the "
            "anki backend must only be driven from its owning thread"
        )
        try:
            return fn(self._col)
        except Exception as exc:
            # Translate anki's own exceptions once, here, so every tool gets
            # actionable messages without repeating the mapping. Anything we
            # don't recognise propagates untouched and is reported as a crash.
            translated = translate(exc)
            if translated is not None:
                raise translated from exc
            raise

    async def run(self, fn: Callable[[Collection], T]) -> T:
        """Run ``fn(collection)`` on the worker thread and await the result."""
        future: Future[T] = self._pool.submit(self._call, fn)
        return await asyncio.wrap_future(future)

    def run_sync(self, fn: Callable[[Collection], T]) -> T:
        """Synchronous variant, for startup work before the loop is running."""
        return self._pool.submit(self._call, fn).result()
