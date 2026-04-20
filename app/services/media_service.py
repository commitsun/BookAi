"""
Download media from Meta WhatsApp Cloud API and store it.

Meta flow:
1. GET graph.facebook.com/{media_id} → returns a temporary download URL
2. GET {download_url} with Bearer token → returns the binary data
"""

import logging

import httpx

from app.services.media_storage import MediaStorage

log = logging.getLogger("media_service")

GRAPH_API_BASE = "https://graph.facebook.com/v20.0"


async def download_and_store(
    http: httpx.AsyncClient,
    access_token: str,
    wa_media_id: str,
    media_type: str,
    storage: MediaStorage,
    mime_type: str | None = None,
    filename: str | None = None,
) -> tuple[str, int, str | None]:
    """Download media from Meta and store it.

    Returns (storage_key, size_bytes, resolved_mime_type).
    """
    headers = {"Authorization": f"Bearer {access_token}"}

    # Step 1: Get download URL from Meta
    r = await http.get(
        f"{GRAPH_API_BASE}/{wa_media_id}",
        headers=headers, timeout=10,
    )
    if r.status_code != 200:
        raise MediaDownloadError(f"Failed to get media URL: {r.status_code} {r.text[:200]}")

    media_info = r.json()
    download_url = media_info.get("url")
    resolved_mime = media_info.get("mime_type") or mime_type
    file_size = media_info.get("file_size")

    if not download_url:
        raise MediaDownloadError("No download URL in Meta response")

    # Step 2: Download the actual file
    r2 = await http.get(download_url, headers=headers, timeout=30)
    if r2.status_code != 200:
        raise MediaDownloadError(f"Failed to download media: {r2.status_code}")

    data = r2.content
    size = file_size or len(data)

    # Step 3: Store
    key = await storage.save(data, resolved_mime or "application/octet-stream", filename)

    log.info(
        "Media stored: type=%s mime=%s size=%d key=%s",
        media_type, resolved_mime, size, key,
    )

    return key, size, resolved_mime


class MediaDownloadError(Exception):
    pass
