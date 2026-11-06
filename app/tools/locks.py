"""Cross-process locks so two senders (timer + manual run) can never send the same email twice."""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Iterator


class AlreadyRunning(RuntimeError):
    pass


@contextlib.contextmanager
def exclusive(lock_path: str | os.PathLike) -> Iterator[None]:
    """Non-blocking exclusive lock on a file. Raises AlreadyRunning if another process holds it."""
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        try:
            import fcntl

            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as e:
                raise AlreadyRunning(f"another process holds {path}") from e
        except ImportError:  # Windows
            import msvcrt

            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as e:
                raise AlreadyRunning(f"another process holds {path}") from e
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        yield
    finally:
        fh.close()  # releases the lock
