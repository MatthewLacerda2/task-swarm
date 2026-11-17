"""Reading files from folders an agent can write to.

An agent can plant symlinks in its own folder. The orchestrator runs with the
user's full permissions, so following one could read a hidden file (the API key)
and hand it back. Every read of agent-controlled paths goes through here: each
path component is opened with O_NOFOLLOW, and only regular files are read.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


class UnsafePath(Exception):
    pass


def open_beneath(root: Path, rel: str | Path) -> int:
    """Open root/rel for reading without following symlinks anywhere below root."""
    parts = Path(rel).parts
    if not parts or any(p in ("..", "/") for p in parts) or Path(rel).is_absolute():
        raise UnsafePath(f"bad relative path: {rel}")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            nfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nfd
        ffd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except OSError as e:
        raise UnsafePath(str(e)) from e
    finally:
        os.close(fd)
    st = os.fstat(ffd)
    if not stat.S_ISREG(st.st_mode):
        os.close(ffd)
        raise UnsafePath(f"not a regular file: {rel}")
    return ffd


def read_beneath(root: Path, rel: str | Path, max_bytes: int) -> bytes:
    """Read at most max_bytes; raises UnsafePath if the file is bigger."""
    fd = open_beneath(root, rel)
    try:
        if os.fstat(fd).st_size > max_bytes:
            raise UnsafePath(f"file too large: {rel}")
        chunks = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > max_bytes:
            raise UnsafePath(f"file too large: {rel}")
        return data
    finally:
        os.close(fd)
