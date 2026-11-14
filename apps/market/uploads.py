"""Picture uploads to Cloudinary (the server has no lasting disk on Vercel, so images live in Cloudinary).

Configure with ONE of:
    CLOUDINARY_URL=cloudinary://<api_key>:<api_secret>@<cloud_name>
or  CLOUDINARY_CLOUD_NAME / CLOUDINARY_API_KEY / CLOUDINARY_API_SECRET

Uses Cloudinary's signed upload REST API with ``requests`` – no extra package needed.
"""
from __future__ import annotations

import hashlib
import os
import time
from urllib.parse import urlparse

import requests
from django.conf import settings

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # Cloudinary free plan limit for images


class UploadError(Exception):
    pass


def _config() -> tuple[str, str, str] | None:
    url = getattr(settings, "CLOUDINARY_URL", "") or os.environ.get("CLOUDINARY_URL", "")
    if url:
        p = urlparse(url)
        if p.scheme == "cloudinary" and p.hostname and p.username and p.password:
            return p.hostname, p.username, p.password
    cloud = getattr(settings, "CLOUDINARY_CLOUD_NAME", "") or os.environ.get("CLOUDINARY_CLOUD_NAME", "")
    key = getattr(settings, "CLOUDINARY_API_KEY", "") or os.environ.get("CLOUDINARY_API_KEY", "")
    secret = getattr(settings, "CLOUDINARY_API_SECRET", "") or os.environ.get("CLOUDINARY_API_SECRET", "")
    return (cloud, key, secret) if cloud and key and secret else None


def is_configured() -> bool:
    return _config() is not None


def upload_image(file, folder: str = "nebret/listings") -> str:
    """Upload one image (a Django UploadedFile) and return its https URL."""
    cfg = _config()
    if not cfg:
        raise UploadError("Picture uploads are not set up yet. Add CLOUDINARY_URL to the server settings, "
                          "or paste a picture link instead.")
    cloud, key, secret = cfg
    if getattr(file, "size", 0) and file.size > MAX_UPLOAD_BYTES:
        raise UploadError(f"“{file.name}” is larger than 10 MB. Please choose a smaller picture.")
    params = {"folder": folder, "timestamp": int(time.time())}
    to_sign = "&".join(f"{k}={params[k]}" for k in sorted(params)) + secret
    signature = hashlib.sha1(to_sign.encode()).hexdigest()
    file.seek(0)
    try:
        resp = requests.post(
            f"https://api.cloudinary.com/v1_1/{cloud}/image/upload",
            data={**params, "api_key": key, "signature": signature},
            files={"file": (file.name, file.read(), getattr(file, "content_type", None) or "application/octet-stream")},
            timeout=60,
        )
    except requests.RequestException as exc:
        raise UploadError("Could not reach the picture service. Check the internet connection and try again.") from exc
    data = {}
    try:
        data = resp.json()
    except ValueError:
        pass
    if resp.status_code >= 400 or not data.get("secure_url"):
        msg = (data.get("error") or {}).get("message") if isinstance(data.get("error"), dict) else ""
        raise UploadError(f"The picture “{file.name}” could not be uploaded{': ' + msg if msg else '.'}")
    return data["secure_url"]
