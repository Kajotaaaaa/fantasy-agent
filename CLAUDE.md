# Contexto para Claude Code

Agente de análisis para LaLiga Fantasy. Python 3.10+, **solo librería estándar**.

## Reglas del proyecto
- **SOLO LECTURA.** No añadir llamadas que modifiquen el juego (pujas, ventas, cláusulas,
  alineación) salvo que el usuario lo pida explícitamente, y en ese caso siempre con
  confirmación previa por operación.
- La API de LaLiga Fantasy NO es pública ni documentada. Rutas y campos vienen de
  ingeniería inversa de la comunidad y pueden cambiar. Antes de tocar `models.py`,
  mira el JSON real con `python -m fantasy_agent probe <ruta>`.
- Mantener un ritmo de peticiones moderado (`REQUEST_DELAY_S`); no paralelizar contra la API.
- Nunca imprimir ni commitear `data/tokens.json` ni `.env`.

## Reglas del juego que el código ya modela
- **Solo son pujables los anuncios con `seller == "LaLiga"`.** Lo que "vende" otro entrenador
  de la liga privada no es pujable entre nosotros: a esos solo se llega pagando su cláusula.
- **Cláusula "lógica"** = precio de cláusula ≤ ~1.2x el valor de mercado real (si no, aunque
  sea una estrella, no compensa — ver `analysis.clause_alerts`, `max_ratio`).
- **Veredicto propio (1-4 estrellas)**: cada alerta de cláusula no es un ratio suelto, es un
  criterio del bot que junta precio, rendimiento (media pts/partido), racha de valor a 7 días
  y noticias reales del día (lesión/duda/titular casi seguro vía `futbolfantasy.py`) más
  potencial de reventa a 14 días. Ver `analysis.clause_verdict`. Las noticias/tendencia solo
  se piden para quien pasa antes el filtro económico barato (`analysis.clause_candidate_players`,
  `service.ensure_clause_trends`) — no para los 30+ rivales de la liga.
- **Blindaje**: un jugador puede estar `isShielded` con `shieldedEndDate` — mientras dure, su
  cláusula NO es pagable aunque `buyoutClauseLockedEndTime` ya haya pasado. Ver `SquadSlot.clause_open`.
- **Congelación de jornada**: la liga bloquea TODAS las cláusulas desde 24h antes del primer
  partido de la jornada hasta que arranca ese partido. Ver `service.clause_freeze_window`.
- **Horizonte de inversión**: comprar (puja o cláusula) blinda al jugador 14 días — las
  proyecciones de reventa (`analysis.project_value`) usan ese horizonte, no unos pocos días.
- **TOP de liga** (`service.league_top_ids`, top 3 por posición en puntos totales de TODA
  LaLiga): son fichajes prioritarios, no oportunidades de inversión — se excluyen de
  `investment_report` y aparecen siempre en `market_report` aunque su score sea bajo.
- **Movimientos propios** (`service.my_transactions`, endpoint `api.activity`, sin documentar):
  tipos identificados cruzando contra la plantilla real: `31` compra en mercado LaLiga, `33`
  venta, `1` cláusula pagada entre managers (`user1` paga, `user2` la sufre/cobra); `4`
  blindaje y `6` bono semanal no son transacciones de jugador. Usa como marca de agua el id de
  actividad más alto visto (`kv.last_activity_id`, sin TTL — el feed devuelve todo el
  histórico siempre) y cachea el precio de compra por jugador (`kv.buy_price:<id>`) para poder
  calcular ganancia/pérdida real en la venta o cláusula siguiente.
- **Corta pérdidas** (`analysis.loss_cut_candidates`, `service.losing_positions_report`):
  compara el valor de mercado actual de tus jugadores contra `kv.buy_price:<id>` (lo que
  pagaste de verdad, no una referencia arbitraria). Si ya está recuperando (`trend.d3 > 1%`)
  no avisa todavía — solo pérdidas sostenidas. Solo cubre lo comprado desde que arrancó el
  seguimiento de movimientos (no hay dato de compra para lo que ya tenías antes).
- **Estudio de mercado diario** (`service.market_arrivals_report`): el mercado de LaLiga se
  refresca cada día a las 21:00; a las `MARKET_STUDY_HOUR:MARKET_STUDY_MINUTE` (21:05 por
  defecto, hora de España) se manda un veredicto (1-4 estrellas, como `clause_verdict`) de
  cada jugador nuevo desde el último estudio (`kv.market_seen:<id>` como snapshot). Las horas
  de juego se calculan con `cli._madrid_now()` (regla DST de la UE a mano, sin `zoneinfo`/
  `tzdata`: en Windows `zoneinfo` necesita el paquete `tzdata`, que no es estándar, y el
  runner de GitHub Actions va en UTC) — nunca compares horas de juego con `datetime.now()` a
  secas.

## Formato de los mensajes
Los informes se mandan a Telegram con `parse_mode=HTML` (`notify.send_telegram`). Helpers en
`analysis.py` (`b`, `i`, `esc`) para negrita/cursiva/escapado — reexportados desde `service.py`.
Cualquier texto que venga de la API o de scraping (nombres de jugador, mánager, notas) pasa por
`esc()`/`b()`/`i()` antes de insertarse: sin esto, un `&`/`<`/`>` suelto rompe el mensaje entero
en Telegram. `notify.send_telegram` trocea por bloques completos (líneas en blanco), nunca a
medio bloque, para no cortar una etiqueta HTML por la mitad.

## Mapa
- `auth.py`     login Azure B2C (PKCE) y refresh de tokens
- `api.py`      rutas de la API (GET únicamente)
- `models.py`   normalización defensiva del JSON (claves alternativas por campo)
- `analysis.py` tendencias, puntuación de oportunidades (once vs inversión), alarmas de cláusula (puro, testeable)
- `lineup.py`   mejor once legal por puntos esperados
- `attendance.py` estima % de titularidad por histórico de jornadas jugadas (gratis, sin APIs externas)
- `service.py`  construye el estado de la liga (incl. próximos partidos vía `calendar`) y los informes de texto;
  `report_sections()` devuelve un mensaje por especialidad para Telegram (no un solo tocho)
- `cli.py`      comandos, bucle `watch` y `tick` (una pasada, para GitHub Actions/cron)
- `.github/workflows/watch.yml` ejecuta `tick` cada ~30 min en GitHub Actions (repo público, estado en `actions/cache`)
- `tests/`      `python -m unittest discover -s tests -v` (sin red, API falsa)

## Primeros pasos tras el login real
1. `probe /v1/competition/1/leagues`, `probe /v1/competition/1/leagues/<id>/standing`,
   `probe /v1/competition/1/league/<id>/market` y `probe /v1/competition/1/leagues/<id>/teams/<teamId>`.
2. Comparar con las claves de `models.py` y ajustar las que no coincidan.
3. Actualizar los fixtures de `tests/test_offline.py` con la forma real.
