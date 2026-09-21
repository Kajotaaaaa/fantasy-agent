"""Envío a Telegram (HTML ligero — negrita/cursiva —, troceado a 4000 caracteres)."""
from __future__ import annotations

from .config import Settings
from .http import request_json


def telegram_enabled(settings: Settings) -> bool:
    return bool(settings.telegram_token and settings.telegram_chat_id)


def send_telegram(settings: Settings, text: str, buttons: dict | None = None) -> None:
    """`buttons` es un teclado inline de Telegram ya en su forma cruda
    (`{"inline_keyboard": [[{"text": ..., "callback_data": ...}]]}`). Si el texto se trocea
    (mensajes largos), los botones solo van en el último trozo — son la acción sobre TODO el
    mensaje, no tiene sentido repetirlos."""
    if not telegram_enabled(settings):
        raise RuntimeError("Configura TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID en el .env")
    url = f"https://api.telegram.org/bot{settings.telegram_token}/sendMessage"
    # Se trocea por bloques completos (separados por línea en blanco), nunca a medio bloque:
    # con parse_mode HTML, cortar dentro de una etiqueta rompe el mensaje entero.
    chunks, current = [], ""
    for block in text.split("\n\n"):
        addition = (("\n\n" if current else "") + block)
        if current and len(current) + len(addition) > 4000:
            chunks.append(current)
            current = block
        else:
            current += addition
    if current:
        chunks.append(current)
    for idx, chunk in enumerate(chunks):
        body = {
            "chat_id": settings.telegram_chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if buttons and idx == len(chunks) - 1:
            body["reply_markup"] = buttons
        request_json("POST", url, json_body=body)


def set_webhook(settings: Settings, url: str, secret_token: str) -> dict:
    """Apunta el bot a la URL del Worker. Telegram reenvía ese `secret_token` en la cabecera
    `X-Telegram-Bot-Api-Secret-Token` de cada llamada, para que el Worker rechace todo lo que
    no venga de Telegram. Solo pide las pulsaciones de botones (`callback_query`)."""
    if not telegram_enabled(settings):
        raise RuntimeError("Configura TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID en el .env")
    return request_json(
        "POST",
        f"https://api.telegram.org/bot{settings.telegram_token}/setWebhook",
        json_body={"url": url, "secret_token": secret_token, "allowed_updates": ["callback_query"]},
    )


def send_report(settings: Settings, sections: list[tuple[str, dict | None]]) -> None:
    """Manda cada especialidad (alineación, mercado, cláusulas...) como un mensaje aparte,
    con sus botones si los lleva."""
    for text, buttons in sections:
        if text.strip():
            send_telegram(settings, text, buttons=buttons)
