"""Envío a Telegram (texto plano, troceado a 4000 caracteres)."""
from __future__ import annotations

from .config import Settings
from .http import request_json


def telegram_enabled(settings: Settings) -> bool:
    return bool(settings.telegram_token and settings.telegram_chat_id)


def send_telegram(settings: Settings, text: str) -> None:
    if not telegram_enabled(settings):
        raise RuntimeError("Configura TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID en el .env")
    url = f"https://api.telegram.org/bot{settings.telegram_token}/sendMessage"
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > 4000:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    for chunk in chunks:
        request_json("POST", url, json_body={
            "chat_id": settings.telegram_chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        })


def send_report(settings: Settings, sections: list[str]) -> None:
    """Manda cada especialidad (alineación, mercado, cláusulas...) como un mensaje aparte."""
    for section in sections:
        if section.strip():
            send_telegram(settings, section)
