"""Cliente HTTP mínimo sobre urllib (sin dependencias)."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

USER_AGENT = "okhttp/4.12.0"


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: str):
        super().__init__(f"HTTP {status} en {url}: {body[:300]}")
        self.status = status
        self.url = url
        self.body = body


def _fetch(method: str, url: str, hdrs: dict[str, str], data: bytes | None, timeout: float, retries: int) -> str:
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            # 5xx y 429 se reintentan; el resto se propaga.
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2 * (attempt + 1))
                last_exc = HttpError(exc.code, url, body)
                continue
            raise HttpError(exc.code, url, body) from None
        except urllib.error.URLError as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            raise
    raise RuntimeError(f"Fallo tras reintentos: {last_exc}")


def request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    form: dict[str, str] | None = None,
    timeout: float = 30,
    retries: int = 2,
) -> Any:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    data = None
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    hdrs.update(headers or {})
    if json_body is not None:
        data = json.dumps(json_body).encode()
        hdrs["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    raw = _fetch(method, url, hdrs, data, timeout, retries)
    return json.loads(raw or "null")


def request_text(url: str, *, headers: dict[str, str] | None = None, timeout: float = 30, retries: int = 2) -> str:
    hdrs = {"User-Agent": USER_AGENT, "Accept": "text/html"}
    hdrs.update(headers or {})
    return _fetch("GET", url, hdrs, None, timeout, retries)
