// Cloudflare Worker: recibe las pulsaciones de botones de Telegram (webhook) y, tras una
// segunda confirmación, dispara el workflow de GitHub Actions que ejecuta la acción real.
//
// Cada botón lleva un código en callback_data: "<verbo>:<payload>" con verbo c (pagar
// cláusula), b (pujar; payload "<anuncio>:<cantidad>"), s (vender) o w (retirar de la venta).
// Un mensaje puede tener varias filas de
// botones (una por jugador); el estado de cada fila se lleva en el propio teclado, sin base
// de datos:
//   "b:<id>"     botón original          -> la fila pasa a "✅ Confirmar · <etiqueta>" + "❌ Cancelar"
//   "B:<id>"     "Confirmar" (mayúscula) -> se quitan esas filas y se dispara el workflow
//   "N:b:<id>"   "Cancelar"              -> la fila vuelve a su botón original
//
// Secretos (npx wrangler secret put <NOMBRE>):
//   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_WEBHOOK_SECRET, GITHUB_TOKEN, GITHUB_REPO

// Cada verbo con el formato de su payload: la puja lleva "<anuncio>:<cantidad>" (la cantidad
// exacta que enseña el botón); el resto, solo un id.
const PAYLOADS = {
  c: /^\d{1,12}$/,
  b: /^\d{1,12}(:\d{1,12})?$/,
  u: /^\d{1,12}:\d{1,12}:\d{1,12}$/, // cambiar puja: "<anuncio>:<puja>:<cantidad>"
  s: /^\d{1,12}$/,
  w: /^\d{1,12}$/,
  a: /^\d{1,12}$/, // armar la compra de una cláusula al desbloquearse
};
// Qué workflow lanza cada verbo al confirmar: por defecto el corto de acciones (fantasy-action);
// armar una cláusula espera hasta el desbloqueo, así que va a su propio trabajo largo.
const EVENTS = { a: "fantasy-clause-snipe" };
const CONFIRM_PREFIX = "✅ Confirmar · ";

const tg = (env, method, body) =>
  fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });

const dispatch = (env, event_type, client_payload) =>
  fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${env.GITHUB_TOKEN}`,
      accept: "application/vnd.github+json",
      "user-agent": "fantasy-agent-worker",
      "x-github-api-version": "2022-11-28",
    },
    body: JSON.stringify({ event_type, client_payload }),
  });

// Vigilante de la vigilancia: el `tick` corre cada 30 min en GitHub Actions y, si deja de correr
// (cron desactivado por inactividad, token de GitHub roto, sesión caducada en bucle...), nadie
// se entera. Cada hora se mira la última ejecución CORRECTA de watch.yml y se avisa por Telegram
// en la primera hora a partir de las 2 h de silencio, y otra vez a partir de las 24 h.
async function healthCheck(env) {
  const res = await fetch(
    `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/watch.yml/runs?status=success&per_page=1`,
    {
      headers: {
        authorization: `Bearer ${env.GITHUB_TOKEN}`,
        accept: "application/vnd.github+json",
        "user-agent": "fantasy-agent-worker",
        "x-github-api-version": "2022-11-28",
      },
    },
  );
  if (!res.ok) return; // sin datos no se alarma (podría ser un fallo momentáneo de GitHub)
  const last = (await res.json()).workflow_runs?.[0];
  const ageMin = last ? (Date.now() - Date.parse(last.updated_at)) / 60000 : Infinity;
  const first = ageMin >= 120 && ageMin < 180;
  const daily = ageMin >= 1440 && ageMin < 1500;
  if (first || daily) {
    const hours = Number.isFinite(ageMin) ? `${Math.round(ageMin / 60)} h` : "mucho tiempo";
    await tg(env, "sendMessage", {
      chat_id: env.TELEGRAM_CHAT_ID,
      text: `⚠️ El vigilante lleva ${hours} sin ejecutarse con éxito. Mira la pestaña Actions del repositorio (fantasy-watch).`,
    });
  }
}

export default {
  // Crons (wrangler.toml): a las 20:50 hora de España lanza la rebaja de último segundo
  // (workflow snipe.yml; el cron de GitHub se retrasa minutos, el de Cloudflare es puntual) y
  // cada hora comprueba que el vigilante sigue vivo.
  async scheduled(event, env, ctx) {
    if (event.cron === "13 * * * *") ctx.waitUntil(healthCheck(env));
    else ctx.waitUntil(dispatch(env, "fantasy-snipe", {}));
  },

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
    const rows = (cb.message.reply_markup && cb.message.reply_markup.inline_keyboard) || [];
    const answer = (text, alert = false) =>
      tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text, show_alert: alert });
    const setKeyboard = (inline_keyboard) =>
      tg(env, "editMessageReplyMarkup", { chat_id, message_id, reply_markup: { inline_keyboard } });

    const hasCode = (row, code) => row.some((btn) => btn.callback_data === code);
    const findButton = (code) => {
      for (const row of rows) {
        const btn = row.find((b) => b.callback_data === code);
        if (btn) return btn;
      }
      return null;
    };

    // "N:c:123" (cancelar) lleva dentro el código original; el resto es "<verbo>:<id>".
    const parts = (cb.data || "").split(":");
    const cancelled = parts[0] === "N";
    const [rawVerb, ...rest] = cancelled ? parts.slice(1) : parts;
    const payload = rest.join(":");
    const verb = (rawVerb || "").toLowerCase();
    if (!PAYLOADS[verb] || !PAYLOADS[verb].test(payload)) {
      await answer("Acción no válida", true);
      return new Response("ok");
    }

    const code = `${verb}:${payload}`;
    const confirmCode = `${verb.toUpperCase()}:${payload}`;
    const cancelCode = `N:${verb}:${payload}`;
    const withoutCancel = rows.filter((row) => !hasCode(row, cancelCode));
    const labelOf = (btn) => (btn.text.startsWith(CONFIRM_PREFIX) ? btn.text.slice(CONFIRM_PREFIX.length) : btn.text);

    if (cancelled) {
      const confirmBtn = findButton(confirmCode);
      await answer("Cancelado");
      if (confirmBtn) {
        await setKeyboard(
          withoutCancel.map((row) => (hasCode(row, confirmCode) ? [{ text: labelOf(confirmBtn), callback_data: code }] : row)),
        );
      }
      return new Response("ok");
    }

    if (rawVerb === verb) {
      // Minúscula = primera pulsación: pedir confirmación, no ejecutar nada todavía.
      const original = findButton(code);
      if (!original) {
        await answer("Ese botón ya no está, pide el aviso de nuevo", true);
        return new Response("ok");
      }
      await answer();
      const next = [];
      for (const row of rows) {
        if (hasCode(row, code)) {
          next.push([{ text: CONFIRM_PREFIX + original.text, callback_data: confirmCode }]);
          next.push([{ text: "❌ Cancelar", callback_data: cancelCode }]);
        } else {
          next.push(row);
        }
      }
      await setKeyboard(next);
      return new Response("ok");
    }

    // Mayúscula = confirmado. Si el botón de confirmar ya no está en el teclado es que esa
    // acción ya se procesó (doble pulsación): no se dispara nada por segunda vez.
    const confirmBtn = findButton(confirmCode);
    if (!confirmBtn) {
      await answer("Ya estaba en marcha", true);
      return new Response("ok");
    }
    await answer("Ejecutando…");
    await setKeyboard(withoutCancel.filter((row) => !hasCode(row, confirmCode)));
    const res = await dispatch(env, EVENTS[verb] || "fantasy-action", { action: code });
    if (!res.ok) {
      await setKeyboard([
        ...withoutCancel.filter((row) => !hasCode(row, confirmCode)),
        [{ text: labelOf(confirmBtn), callback_data: code }],
      ]);
      await tg(env, "sendMessage", {
        chat_id,
        text: `❌ No pude lanzar la acción (GitHub respondió ${res.status}). No se ha hecho nada.`,
      });
    }
    return new Response("ok");
  },
};
