"""On-disk cover cache shared by the Inducks and GCD code paths.

Files are stored at ``COVER_DIR/<aa>/<bb>/<full-md5>.<ext>`` where ``<aa>``
is the first two hex chars of ``md5(key)`` and ``<bb>`` is the next two.
This keeps any single directory at ~256 entries instead of letting tens of
thousands of files pile up in a single folder.

For backwards compatibility, lookups also check the legacy flat layout at
``COVER_DIR/<full-md5>.<ext>``; if a file is found there it is moved into
the sharded layout on the spot, so subsequent requests resolve directly.
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from .config import COVER_DIR

_EXTS = (".jpg", ".gif", ".png")
_MIME = {".jpg": "image/jpeg", ".gif": "image/gif", ".png": "image/png"}


def _hash(key: str) -> str:
    return hashlib.md5(key.encode()).hexdigest()


def _sharded(h: str, ext: str) -> Path:
    return COVER_DIR / h[:2] / h[2:4] / (h + ext)


def _flat(h: str, ext: str) -> Path:
    return COVER_DIR / (h + ext)


def cache_path_for(key: str) -> Path | None:
    """Return the on-disk path for a cached cover, or ``None``.

    A file found at the legacy flat path is migrated into the sharded
    location so the next call finds it without touching the flat layout.
    """
    h = _hash(key)
    for ext in _EXTS:
        sharded = _sharded(h, ext)
        if sharded.exists():
            return sharded
        flat = _flat(h, ext)
        if flat.exists():
            sharded.parent.mkdir(parents=True, exist_ok=True)
            try:
                flat.rename(sharded)            # atomic when on one fs
            except OSError:
                shutil.move(str(flat), str(sharded))
            return sharded
    return None


def cache_write(key: str, content: bytes, content_type: str) -> Path:
    """Write ``content`` for ``key`` into the sharded cache and return its path."""
    h = _hash(key)
    if "gif" in content_type:
        ext = ".gif"
    elif "png" in content_type:
        ext = ".png"
    else:
        ext = ".jpg"
    path = _sharded(h, ext)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def mime_from_ext(p: Path) -> str:
    return _MIME.get(p.suffix, "image/jpeg")
