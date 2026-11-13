"""HTTP helpers and response parsing for ComfyUI.

No side effects: the caller starts and stops the server, this module only
talks HTTP and interprets the answers. Tests inject a fake client, so no
running ComfyUI is required to exercise the logic.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class HttpError(RuntimeError):
    pass


class HttpClient:
    """Tiny stdlib HTTP client with injectable opener."""

    def __init__(self, opener: Callable[..., Any] | None = None, timeout: float = 30.0) -> None:
        self._opener = opener or urllib.request.urlopen
        self.timeout = timeout

    def _request(self, url: str, data: bytes | None, headers: dict[str, str], timeout: float | None) -> str:
        request = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method="POST" if data is not None else "GET",
        )
        try:
            with self._opener(request, timeout=timeout or self.timeout) as response:
                return response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500] if hasattr(exc, "read") else ""
            raise HttpError(f"HTTP {exc.code} for {url}: {detail}") from exc
        except Exception as exc:
            raise HttpError(f"{url}: {exc}") from exc

    def get_text(self, url: str, timeout: float | None = None) -> str:
        return self._request(url, None, {"Accept": "*/*"}, timeout)

    def get_json(self, url: str, timeout: float | None = None) -> dict:
        body = self._request(url, None, {"Accept": "application/json"}, timeout)
        try:
            return json.loads(body) if body else {}
        except json.JSONDecodeError as exc:
            raise HttpError(f"{url}: invalid JSON: {exc}") from exc

    def post_json(self, url: str, payload: dict, timeout: float | None = None) -> dict:
        data = json.dumps(payload).encode("utf-8")
        body = self._request(url, data, {"Accept": "application/json", "Content-Type": "application/json"}, timeout)
        try:
            return json.loads(body) if body else {}
        except json.JSONDecodeError as exc:
            raise HttpError(f"{url}: invalid JSON: {exc}") from exc


def parse_stats(data: dict) -> dict:
    """Flatten the /system_stats fields that matter for reporting."""
    system = data.get("system") or {}
    devices = data.get("devices") or []
    device = devices[0] if devices else {}
    vram = device.get("vram_total")
    return {
        "comfyui_version": str(system.get("comfyui_version") or ""),
        "pytorch_version": str(system.get("pytorch_version") or ""),
        "python_version": str(system.get("python_version") or "").split(" ")[0],
        "device_name": str(device.get("name") or ""),
        "device_type": str(device.get("type") or ""),
        "vram_total": vram if isinstance(vram, int) else None,
    }


def manager_version(
    client: HttpClient, url: str, attempts: int = 30, sleep: Callable[[float], None] = time.sleep
) -> str:
    """Manager answers /manager/version with plain text, sometimes late."""
    base = url.rstrip("/")
    for attempt in range(attempts):
        try:
            text = client.get_text(f"{base}/manager/version", timeout=3).strip()
        except HttpError:
            text = ""
        first = text.splitlines()[0].strip() if text else ""
        if first:
            return first
        if attempt + 1 < attempts:
            sleep(1.0)
    return ""


def wait_ready(
    client: HttpClient,
    url: str,
    timeout: float = 240.0,
    interval: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    alive: Callable[[], bool] | None = None,
) -> bool:
    """Poll /system_stats until the server answers or the deadline passes."""
    deadline = clock() + timeout
    target = f"{url.rstrip('/')}/system_stats"
    while clock() < deadline:
        if alive is not None and not alive():
            return False
        try:
            client.get_json(target, timeout=2)
            return True
        except HttpError:
            sleep(interval)
    return False


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) != 0


def first_free_port(ports, host: str = "127.0.0.1") -> int | None:
    for port in ports:
        if port_free(port, host):
            return port
    return None


@dataclass(frozen=True)
class HistoryResult:
    ok: bool
    images: tuple[dict, ...]
    detail: str


def interpret_history(entry: dict | None) -> HistoryResult:
    """Interpret a /history/<id> entry for one queued prompt."""
    if not entry:
        return HistoryResult(False, (), "no history entry")
    status = entry.get("status") or {}
    state = str(status.get("status_str") or "")
    completed = status.get("completed")
    if state != "success" or completed is False:
        return HistoryResult(False, (), f"execution status {state or 'unknown'} (completed={completed})")
    images: list[dict] = []
    for node_output in (entry.get("outputs") or {}).values():
        images.extend(node_output.get("images") or [])
    if not images:
        return HistoryResult(False, (), "no images in history output")
    return HistoryResult(True, tuple(images), f"{len(images)} image(s)")
