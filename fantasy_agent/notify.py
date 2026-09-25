"""Envío a Telegram (HTML ligero — negrita/cursiva —, troceado a 4000 caracteres)."""
from __future__ import annotations

from .config import Settings
from .http import request_json


def telegram_enabled(settings: Settings) -> bool:
    return bool(settings.telegram_token and settings.telegram_chat_id)


def send_message(settings: Settings, text: str) -> int | None:
    """Un solo mensaje (sin trocear) devolviendo su `message_id`, para poder editarlo luego
    (`edit_message`): contadores atrás que se actualizan en el sitio."""
    if not telegram_enabled(settings):
        raise RuntimeError("Configura TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID en el .env")
    resp = request_json(
        "POST", f"https://api.telegram.org/bot{settings.telegram_token}/sendMessage",
        json_body={"chat_id": settings.telegram_chat_id, "text": text, "parse_mode": "HTML"},
    )
    return (resp or {}).get("result", {}).get("message_id")


def edit_message(settings: Settings, message_id: int, text: str) -> None:
    """Reescribe un mensaje ya enviado. Un fallo (mensaje idéntico, límite de ediciones) no debe
    tumbar quien lo llama: es solo cosmética."""
    try:
        request_json(
            "POST", f"https://api.telegram.org/bot{settings.telegram_token}/editMessageText",
            json_body={"chat_id": settings.telegram_chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"},
            retries=0,
        )
    except Exception:
        pass


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
    no venga de Telegram. Pide pulsaciones de botones (`callback_query`) y mensajes de texto
    (`message`, 2026-09-23: hace falta para "/menu" — sin "message" en `allowed_updates`,
    Telegram ni siquiera reenvía el mensaje al Worker, por más que este ya supiera manejarlo)."""
    if not telegram_enabled(settings):
        raise RuntimeError("Configura TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID en el .env")
    return request_json(
        "POST",
        f"https://api.telegram.org/bot{settings.telegram_token}/setWebhook",
        json_body={"url": url, "secret_token": secret_token, "allowed_updates": ["callback_query", "message"]},
    )


# El único comando de texto que el Worker entiende de verdad (ver `worker/telegram-webhook.js`,
# solo mira `msg.text === "/menu"`) -- una sola cuenta, sin flujo de "/start" como sniperfantasy,
# así que no tiene sentido listar más comandos de los que hacen algo al pulsarlos.
COMMANDS = [{"command": "menu", "description": "Abrir el panel de consultas"}]


def set_my_commands(settings: Settings, commands: list[dict] | None = None) -> dict:
    """Registra el desplegable nativo de comandos (icono "/" junto a la caja de texto en
    Telegram) -- de solo lectura para Telegram, una llamada de una vez, como `set_webhook`; no
    hace falta repetirla salvo que cambie la lista. Sin argumento usa `COMMANDS`."""
    if not telegram_enabled(settings):
        raise RuntimeError("Configura TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID en el .env")
    return request_json(
        "POST",
        f"https://api.telegram.org/bot{settings.telegram_token}/setMyCommands",
        json_body={"commands": commands if commands is not None else COMMANDS},
    )


def send_report(settings: Settings, sections: list[tuple[str, dict | None]]) -> None:
    """Manda cada especialidad (alineación, mercado, cláusulas...) como un mensaje aparte,
    con sus botones si los lleva."""
    for text, buttons in sections:
        if text.strip():
            send_telegram(settings, text, buttons=buttons)
