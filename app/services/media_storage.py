"""
Media storage abstraction. Local filesystem for dev, swappable to S3/R2.

Usage:
    storage = LocalStorage("/app/media")
    key = await storage.save(data, "image/jpeg", "photo.jpg")
    url = storage.get_url(key)
"""

import os
import uuid
from datetime import datetime
from typing import Protocol


class MediaStorage(Protocol):
    async def save(self, data: bytes, mime_type: str, filename: str | None = None) -> str:
        """Save media and return a storage key."""
        ...

    def get_url(self, key: str) -> str:
        """Return a URL or path to access the stored media."""
        ...


class LocalStorage:
    """Store media on local filesystem. For development only."""

    def __init__(self, base_path: str = "/app/media"):
        self._base_path = base_path

    async def save(self, data: bytes, mime_type: str, filename: str | None = None) -> str:
        ext = _ext_from_mime(mime_type) or _ext_from_filename(filename) or ""
        date_prefix = datetime.utcnow().strftime("%Y/%m/%d")
        unique = uuid.uuid4().hex[:12]
        key = f"{date_prefix}/{unique}{ext}"

        full_path = os.path.join(self._base_path, key)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)

        with open(full_path, "wb") as f:
            f.write(data)

        return key

    def get_url(self, key: str) -> str:
        return f"/media/{key}"


def _ext_from_mime(mime_type: str) -> str:
    mapping = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "audio/ogg": ".ogg",
        "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a",
        "video/mp4": ".mp4",
        "application/pdf": ".pdf",
    }
    return mapping.get(mime_type, "")


def _ext_from_filename(filename: str | None) -> str:
    if not filename or "." not in filename:
        return ""
    return "." + filename.rsplit(".", 1)[-1].lower()
