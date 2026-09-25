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
// exacta que enseña el botón); el resto, solo un id. "q" es distinto: no es un id, es el nombre
// de un comando de solo lectura ya fijado de antemano (ver TOP_MENU/CATEGORIES) — el enum de la regex es
// la validación, no hace falta nada más para que no se pueda colar un subcomando arbitrario.
// Un botón por cada acción de consulta que existe en `fantasy_agent/cli.py` (2026-09-26,
// petición del usuario: "necesito que existan botones por cada tipo de acción que podemos
// lograr, veo que faltan algunas" -- y que el menú fuera IGUAL que el de sniperfantasy, que ya
// tenía más botones que este). Sin plan de pago aquí (una sola cuenta, sin Premium/Standard:
// nunca se ha metido ese concepto en fantasy-agent a propósito), así que ningún botón lleva
// candado ni aviso de mejorar el plan -- todo está siempre disponible.
const REPORT_LABELS = {
  report: "📊 Informe completo",
  lineup: "🧩 Alineación recomendada",
  trends: "📈 Tendencias de tus jugadores",
  advice: "🎓 Consejo del día",
  market: "🛒 Mercado para tu once",
  investment: "💹 Oportunidades de inversión",
  "market-news": "🗞️ Nuevo en el mercado",
  bids: "📌 Tus pujas pendientes",
  listings: "📤 En venta ahora",
  losses: "🔻 Corta pérdidas",
  "sell-candidates": "📉 Candidatos a vender",
  "clause-risk": "🛡️ Riesgo de que te clausulen",
  "clause-raise": "🎣 Anzuelo: sube tu cláusula",
  "clause-bait": "⏰ Tus cláusulas que se desbloquean pronto",
  unlocks: "🔓 Desbloqueos de rivales",
  rivals: "🤝 Saldo de rivales",
  balance: "🧮 Balance de hoy",
};

// Categorías (2026-09-26): con 17 acciones ya no cabe un menú plano -- se agrupan igual que en
// sniperfantasy (bot/menu_handler.py), mismas claves y mismo orden, para que ambos bots se
// sientan iguales.
const CATEGORIES = {
  team: ["🧭 Tu equipo", ["lineup", "trends", "advice"]],
  market: ["🛒 Mercado", ["market", "investment", "market-news", "bids"]],
  sell: ["📤 Vender", ["listings", "losses", "sell-candidates"]],
  clause: ["🔐 Cláusulas", ["clause-risk", "clause-raise", "clause-bait", "unlocks"]],
  rivals: ["💰 Rivales y balance", ["rivals", "balance"]],
};

const PAYLOADS = {
  c: /^\d{1,12}$/,
  b: /^\d{1,12}(:\d{1,12})?$/,
  u: /^\d{1,12}:\d{1,12}:\d{1,12}$/, // cambiar puja: "<anuncio>:<puja>:<cantidad>"
  s: /^\d{1,12}$/,
  w: /^\d{1,12}$/,
  a: /^\d{1,12}(:\d{1,12})?$/, // armar la compra de una cláusula al desbloquearse: "<jugador>:<importe>"
  o: /^\d{1,12}:\d{1,12}:\d{1,12}$/, // aceptar oferta: "<jugador>:<oferta>:<importe>"
  r: /^\d{1,12}:\d{1,12}$/, // rechazar oferta: "<jugador>:<oferta>"
  // consulta del menú "/menu": "home"/"cat:<categoría>" son navegación pura (no disparan nada,
  // solo cambian qué teclado se ve); el resto son los nombres de comando reales de la CLI.
  q: new RegExp(`^(home|cat:(${Object.keys(CATEGORIES).join("|")})|${Object.keys(REPORT_LABELS).join("|")})$`),
};
// Qué workflow lanza cada verbo al confirmar: por defecto el corto de acciones (fantasy-action);
// armar una cláusula espera hasta el desbloqueo, así que va a su propio trabajo largo; una
// consulta del menú es de solo lectura y va a su propio workflow corto (fantasy-query.yml).
const EVENTS = { a: "fantasy-clause-snipe", q: "fantasy-query" };
const CONFIRM_PREFIX = "✅ Confirmar · ";

// Menú de consultas bajo demanda ("/menu" en el chat, petición del usuario 2026-09-23: "un panel
// de botones... para consultar cosas exactas"). Cada opción de hoja dispara fantasy-query.yml,
// que ejecuta el comando de la CLI correspondiente con --telegram — mismo mecanismo de siempre
// (repository_dispatch), solo que de SOLO LECTURA, así que se lanza al primer toque, sin la
// doble confirmación de las acciones que mueven dinero (ver más abajo, verbo "q"). Las categorías
// no disparan nada: solo cambian el teclado que se ve (igual que "⬅️ Atrás").
const TOP_MENU = [
  [{ text: REPORT_LABELS.report, callback_data: "q:report" }],
  ...Object.entries(CATEGORIES).map(([key, [label]]) => [{ text: label, callback_data: `q:cat:${key}` }]),
];

function categoryKeyboard(key) {
  const [, actions] = CATEGORIES[key];
  return [
    ...actions.map((action) => [{ text: REPORT_LABELS[action], callback_data: `q:${action}` }]),
    [{ text: "⬅️ Atrás", callback_data: "q:home" }],
  ];
}

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
  // Único cron (wrangler.toml): cada hora comprueba que el vigilante sigue vivo. "fantasy-snipe"
  // (rebaja de último segundo) ya no lo dispara un cron de hora fija — lo arma dinámicamente el
  // propio tick de GitHub Actions al detectar el cierre real de esta liga (2026-09-23).
  async scheduled(event, env, ctx) {
    ctx.waitUntil(healthCheck(env));
  },

  async fetch(request, env) {
    if (request.method !== "POST") return new Response("ok");
    if (request.headers.get("x-telegram-bot-api-secret-token") !== env.TELEGRAM_WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 403 });
    }

    const update = await request.json();

    // "/menu" en el chat (mensaje de texto, no un botón): abre el teclado de consultas. Fuera de
    // esto, cualquier otro texto se ignora — este webhook solo entiende botones y este comando.
    const msg = update.message;
    if (msg && msg.text === "/menu" && msg.chat && String(msg.chat.id) === String(env.TELEGRAM_CHAT_ID)) {
      await tg(env, "sendMessage", {
        chat_id: msg.chat.id, text: "☰ ¿Qué quieres consultar?",
        reply_markup: { inline_keyboard: TOP_MENU },
      });
      return new Response("ok");
    }

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

    if (verb === "q") {
      // Navegación por categorías (2026-09-26, petición del usuario: "que se mueva hacia
      // adelante y hacia atrás en el mismo menú"): no dispara nada, solo cambia qué teclado se
      // ve -- se EDITA este mismo mensaje (editMessageText, ya tenemos el message_id de arriba),
      // no se manda uno nuevo. El resto de consultas de verdad (informe, mercado...) sigue
      // mandando mensaje nuevo, tiene sentido dejarlas en el historial del chat.
      if (payload === "home") {
        await answer();
        await tg(env, "editMessageText", { chat_id, message_id, text: "☰ ¿Qué quieres consultar?", reply_markup: { inline_keyboard: TOP_MENU } });
        return new Response("ok");
      }
      if (payload.startsWith("cat:")) {
        const key = payload.slice(4);
        const category = CATEGORIES[key];
        if (!category) {
          await answer("Categoría no válida", true);
          return new Response("ok");
        }
        await answer();
        await tg(env, "editMessageText", {
          chat_id, message_id, text: `☰ ${category[0]}`, reply_markup: { inline_keyboard: categoryKeyboard(key) },
        });
        return new Response("ok");
      }

      // Consulta de solo lectura (menú "/menu"): no mueve nada, así que se dispara al primer
      // toque, sin la doble confirmación de las acciones (regla del usuario, 2026-09-23).
      await answer("Consultando…");
      const res = await dispatch(env, EVENTS.q, { query: payload });
      if (!res.ok) {
        await tg(env, "sendMessage", { chat_id, text: `❌ No pude lanzar la consulta (GitHub respondió ${res.status}).` });
        return new Response("ok");
      }
      // El toast de "Consultando…" desaparece en un par de segundos y esto tarda 20-40 s
      // (arrancar un runner de GitHub desde cero, no hay forma de acelerarlo con este mecanismo
      // sin montar un servidor siempre encendido) — sin un mensaje que se quede en el chat, el
      // usuario no tiene forma de saber que el toque sí ha hecho algo (petición del usuario,
      // 2026-09-23). No hay contador en vivo: para eso el propio job necesitaría el message_id
      // de este aviso para ir editándolo, más cableado del que compensa por ahora.
      await tg(env, "sendMessage", {
        chat_id, text: "⏳ Generando… tarda unos 20-40 s, te lo mando en cuanto esté.",
      });
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
