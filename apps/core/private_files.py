"""
Private files: bank receipts, agreements, chat attachments. They are never public.

* With Cloudinary configured they are uploaded as ``type=authenticated`` resources (``raw`` for PDFs) and handed out
  only as short-lived signed download links (Cloudinary's private-download API).
* Without Cloudinary (local development and tests) they are written under ``PRIVATE_MEDIA_ROOT`` – outside
  ``MEDIA_ROOT``, so nothing serves them directly – and handed out through ``/api/files/<signed token>/``.

Callers decide *who* may get a link (buyer, the order's company staff, Nebret staff); this module only stores files
and signs links. Store the returned key (a dict) on the model, never a URL.
"""
from __future__ import annotations

import hashlib
import os
import time
import uuid
from pathlib import Path

import requests
from django.conf import settings
from django.core import signing
from django.urls import reverse

from apps.core.exceptions import BusinessRuleError

TYPES = {"image/jpeg": "jpg", "image/png": "png", "application/pdf": "pdf"}
LINK_MINUTES = 5
_SALT = "nebret.private-file"


def _root() -> Path:
    return Path(getattr(settings, "PRIVATE_MEDIA_ROOT", Path(settings.BASE_DIR) / "private_media"))


def sniff(head: bytes) -> str | None:
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    return None


def validate(file, *, max_mb: int | None = None, allowed=tuple(TYPES)) -> tuple[bytes, str]:
    """Read and check an upload: real JPG / PNG / PDF (by its bytes, not its name) and not too big."""
    if file is None:
        raise BusinessRuleError("Attach the receipt (photo or PDF).")
    max_mb = max_mb or settings.NEBRET.get("RECEIPT_MAX_MB", 10)
    size = getattr(file, "size", None)
    if size and size > max_mb * 1024 * 1024:
        raise BusinessRuleError(f"The file is larger than {max_mb} MB.")
    file.seek(0)
    data = file.read()
    if len(data) > max_mb * 1024 * 1024:
        raise BusinessRuleError(f"The file is larger than {max_mb} MB.")
    kind = sniff(data[:16])
    if kind not in allowed:
        raise BusinessRuleError("Only JPG, PNG or PDF files can be attached.")
    return data, kind


def _cloudinary():
    from apps.market.uploads import _config

    return _config()


def _sign(params: dict, secret: str) -> str:
    to_sign = "&".join(f"{k}={params[k]}" for k in sorted(params) if params[k] not in (None, "")) + secret
    return hashlib.sha1(to_sign.encode()).hexdigest()


def store(data: bytes, content_type: str, *, folder: str, name: str = "") -> dict:
    ext = TYPES.get(content_type, "bin")
    stem = uuid.uuid4().hex
    cfg = _cloudinary()
    if cfg:
        cloud, key, secret = cfg
        resource_type = "raw" if ext == "pdf" else "image"
        params = {"folder": f"nebret/private/{folder}", "timestamp": int(time.time()), "type": "authenticated",
                  "public_id": stem + (".pdf" if resource_type == "raw" else "")}
        try:
            resp = requests.post(f"https://api.cloudinary.com/v1_1/{cloud}/{resource_type}/upload",
                                 data={**params, "api_key": key, "signature": _sign(params, secret)},
                                 files={"file": (name or f"{stem}.{ext}", data, content_type)}, timeout=60)
            body = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise BusinessRuleError("The file could not be uploaded. Try again.") from exc
        if resp.status_code >= 400 or not body.get("public_id"):
            raise BusinessRuleError("The file could not be uploaded. Try again.")
        return {"storage": "cloudinary", "public_id": body["public_id"], "resource_type": resource_type,
                "format": body.get("format", ""), "content_type": content_type, "name": name[:120],
                "size": len(data)}
    path = _root() / folder / f"{stem}.{ext}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"storage": "local", "path": str(path.relative_to(_root())), "content_type": content_type,
            "name": name[:120], "size": len(data)}


def signed_url(key: dict, request=None, *, minutes: int = LINK_MINUTES) -> str:
    """A link that works for ``minutes`` only."""
    if not key:
        return ""
    if key.get("storage") == "cloudinary":
        cfg = _cloudinary()
        if not cfg:
            return ""
        cloud, api_key, secret = cfg
        params = {"public_id": key["public_id"], "type": "authenticated", "timestamp": int(time.time()),
                  "expires_at": int(time.time()) + minutes * 60}
        if key.get("format"):
            params["format"] = key["format"]
        sig = _sign(params, secret)
        from urllib.parse import urlencode

        return (f"https://api.cloudinary.com/v1_1/{cloud}/{key['resource_type']}/download?"
                + urlencode({**params, "api_key": api_key, "signature": sig}))
    token = signing.dumps({"p": key["path"], "t": key.get("content_type", ""), "m": minutes}, salt=_SALT)
    url = reverse("private-file", args=[token])
    return request.build_absolute_uri(url) if request is not None else url


def open_local(token: str) -> tuple[Path, str] | None:
    """For the ``/api/files/<token>/`` view: the file behind a valid, unexpired token."""
    try:
        data = signing.loads(token, salt=_SALT, max_age=60 * 60)
        data = signing.loads(token, salt=_SALT, max_age=int(data.get("m", LINK_MINUTES)) * 60)
    except signing.BadSignature:
        return None
    path = (_root() / data["p"]).resolve()
    if not str(path).startswith(str(_root().resolve()) + os.sep) or not path.exists():
        return None
    return path, data.get("t") or "application/octet-stream"


def read(key: dict) -> bytes:
    """The file's bytes (used for the agreement and for tests)."""
    if key.get("storage") == "local":
        return (_root() / key["path"]).read_bytes()
    resp = requests.get(signed_url(key), timeout=60)
    resp.raise_for_status()
    return resp.content
