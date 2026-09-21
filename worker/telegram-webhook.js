// Cloudflare Worker: recibe las pulsaciones de botones de Telegram (webhook) y, tras una
// segunda confirmación, dispara el workflow de GitHub Actions que ejecuta la acción real.
//
// Flujo (código de acción en callback_data, ver CLAUDE.md):
//   "c:<id>"    botón original "Pagar cláusula"  -> se cambia por "Sí, pagar" / "Cancelar"
//   "C:<id>"    "Sí, pagar" (confirmado)         -> se quitan los botones y se dispara el workflow
//   "N:c:<id>"  "Cancelar"                       -> se restaura el botón original
//
// Secretos (npx wrangler secret put <NOMBRE>):
//   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_WEBHOOK_SECRET, GITHUB_TOKEN, GITHUB_REPO

const ACTIONS = {
  c: { label: "💳 Pagar cláusula", confirm: "✅ Sí, pagar", valid: /^\d{1,12}$/ },
};

const tg = (env, method, body) =>
  fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });

export default {
  async fetch(request, env) {
    if (request.method !== "POST") return new Response("ok");
    if (request.headers.get("x-telegram-bot-api-secret-token") !== env.TELEGRAM_WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 403 });
    }

    const update = await request.json();
    const cb = update.callback_query;
    if (!cb || !cb.message) return new Response("ok");
    // Solo se atiende tu chat: si alguien más encuentra el bot, sus pulsaciones no hacen nada.
    if (String(cb.message.chat.id) !== String(env.TELEGRAM_CHAT_ID)) return new Response("ok");

    const chat_id = cb.message.chat.id;
    const message_id = cb.message.message_id;
    const answer = (text, alert = false) =>
      tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text, show_alert: alert });
    const setKeyboard = (inline_keyboard) =>
      tg(env, "editMessageReplyMarkup", { chat_id, message_id, reply_markup: { inline_keyboard } });

    // "N:c:123" (cancelar) lleva dentro el código original; el resto es "<verbo>:<payload>".
    const parts = (cb.data || "").split(":");
    const cancelled = parts[0] === "N";
    const [rawVerb, ...rest] = cancelled ? parts.slice(1) : parts;
    const payload = rest.join(":");
    const verb = (rawVerb || "").toLowerCase();
    const action = ACTIONS[verb];
    if (!action || !action.valid.test(payload)) {
      await answer("Acción no válida", true);
      return new Response("ok");
    }
    const originalKeyboard = () => [[{ text: action.label, callback_data: `${verb}:${payload}` }]];

    if (cancelled) {
      await answer("Cancelado");
      await setKeyboard(originalKeyboard());
      return new Response("ok");
    }

    if (rawVerb === verb) {
      // Minúscula = primera pulsación: pedir confirmación, no ejecutar nada todavía.
      await answer();
      await setKeyboard([[
        { text: action.confirm, callback_data: `${verb.toUpperCase()}:${payload}` },
        { text: "❌ Cancelar", callback_data: `N:${verb}:${payload}` },
      ]]);
      return new Response("ok");
    }

    // Mayúscula = confirmado. Se quitan los botones antes de disparar para que una segunda
    // pulsación rápida no pueda lanzar la acción dos veces.
    await answer("Ejecutando…");
    await setKeyboard([]);
    const res = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`, {
      method: "POST",
      headers: {
        authorization: `Bearer ${env.GITHUB_TOKEN}`,
        accept: "application/vnd.github+json",
        "user-agent": "fantasy-agent-worker",
        "x-github-api-version": "2022-11-28",
      },
      body: JSON.stringify({ event_type: "fantasy-action", client_payload: { action: `${verb}:${payload}` } }),
    });
    if (!res.ok) {
      await setKeyboard(originalKeyboard());
      await tg(env, "sendMessage", {
        chat_id,
        text: `❌ No pude lanzar la acción (GitHub respondió ${res.status}). No se ha hecho nada.`,
      });
    }
    return new Response("ok");
  },
};
