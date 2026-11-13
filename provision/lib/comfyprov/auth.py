"""Civitai token handling.

The token never appears in a command line and is stored with mode 600. The
shell downloader reads it from this file when it builds the temporary curl
config, so no token is ever visible in ``ps``.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from pathlib import Path

VALIDATE_URL = "https://civitai.com/api/v1/me"


def read_token(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def save_token(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, token.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def validate_token(token: str, opener=None, timeout: float = 20.0) -> tuple[int | None, str]:
    """Return (http_status, detail); status None means the request failed."""
    fetch = opener or urllib.request.urlopen
    request = urllib.request.Request(VALIDATE_URL, headers={"Authorization": f"Bearer {token}"})
    try:
        with fetch(request, timeout=timeout) as response:
            return int(getattr(response, "status", 200)), ""
    except urllib.error.HTTPError as exc:
        return int(exc.code), str(exc)
    except Exception as exc:
        return None, str(exc)
