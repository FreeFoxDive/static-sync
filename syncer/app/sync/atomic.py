"""Write a complete file then replace the destination."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable
from pathlib import Path


class DownloadTooLarge(ValueError):
    pass


class EmptyDownload(ValueError):
    pass


def atomic_write(dest: Path, chunks: Iterable[bytes], max_bytes: int) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".part", dir=dest.parent)
    tmp = Path(tmp_name)
    total = 0
    try:
        with os.fdopen(fd, "wb") as handle:
            for chunk in chunks:
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise DownloadTooLarge(f"object exceeds {max_bytes} bytes")
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if total == 0:
            raise EmptyDownload("download was empty")
        os.replace(tmp, dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return total
