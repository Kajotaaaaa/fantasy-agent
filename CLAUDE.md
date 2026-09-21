# Contexto para Claude Code

Agente de análisis para LaLiga Fantasy. Python 3.10+, **solo librería estándar**.

## Reglas del proyecto
- **SOLO LECTURA por defecto.** No añadir llamadas que modifiquen el juego salvo que el
  usuario lo pida explícitamente, y en ese caso siempre con confirmación previa por operación.
  Ya existen escrituras verificadas contra la cuenta real (ver sección "Escritura" abajo):
  todas siguen el patrón vista-previa-por-defecto + `--confirm` para ejecutar de verdad, y
  nunca se deben disparar automáticamente sin que el usuario apruebe esa operación concreta.

## Techo de puja (fichajes para el once, no flipeo)
`analysis.bid_ceiling` + `service.position_ppm_benchmark`/`bid_ceiling_report` (comando
`ceiling <player_id>`): las pujas son ciegas por diseño del juego (confirmado en el FAQ
oficial: no se puede ver lo que puja nadie), así que "pujar un poco más que el rival" no
existe como estrategia — la única defensa real es un techo basado en valor, no en adivinar a
la competencia. Techo = puntos de media del jugador ÷ mediana de puntos-por-millón del
mercado pujable en su posición ahora mismo (la "tarifa" vigente); +30% si es TOP de liga
(`league_top_ids` — top 3 de TODA LaLiga por puntos totales en su posición, no solo de tu
liga privada: son escasos e insustituibles, vale la pena pagar de más). Por encima del techo,
aunque ganes la puja, te habría salido mejor la alternativa del mercado.

## Flipeo (comprar en subida, revender rápido) — reglas exactas del usuario
`analysis.flip_decision`: con beneficio, aceptar siempre (no ser codicioso con márgenes
pequeños). Sin beneficio y con menos de 3 días desde la compra, esperar SI la tendencia
sigue subiendo (`trend.d3 > 0`) — el juego ofrece un precio distinto cada ciclo, no hay
prisa. Pasados 3 días sin beneficio, priorizar liquidez: aceptar en cuanto la oferta cubra
al menos lo pagado; evitar vender por debajo salvo que ya no quede alternativa (fecha límite
antes de la jornada, saldo necesario, etc. — no está codificado como "vender sí o sí" incluso
en pérdida; esa decisión final la toma el usuario cuando llegue el caso).
`analysis.squad_can_field_eleven`: antes de proponer aceptar una venta, comprobar que la
plantilla sin ese jugador sigue pudiendo alinear un once legal (cuerpos disponibles por
posición, no calidad) — la regla de "nunca quedarse corto para la jornada" que pidió el
usuario. `buy_date:<id>` en `kv` (junto a `buy_price:<id>`, ambos puestos/borrados a la vez
por `my_transactions`) da los días transcurridos para esta lógica.
Pendiente: conectar esto con `accept`/`reject` reales — falta ver una oferta real (campos
`offerId`/`marketId`/importe) para terminar el parseo (`api.player_offers`, forma sin
verificar). Autonomía acordada: SIEMPRE confirmación del usuario por operación, nunca
autoejecutar pujas/ventas de este flujo sin preguntar primero.

## Cuándo vender: tendencia, no cuánto has perdido ya
`analysis.sell_candidates`/`service.sell_candidates_report` (comando `sell-candidates`, y en
el informe diario): el disparador para poner algo a la venta es que la tendencia lleve
bajando (`d1 < 0` y `d3 < 0`), NO la magnitud de la pérdida acumulada frente a lo pagado —
eso ya lo cubre "corta pérdidas" (`loss_cut_candidates`), que es un umbral distinto (≥8%) y
un propósito distinto (avisar de pérdida seria, no decidir el momento de vender). Un jugador
comprado caro como apuesta especulativa (cláusula por encima de mercado) que sigue subiendo
NO es candidato aunque siga por debajo de lo pagado — la apuesta puede seguir siendo buena
(caso real: Yoel Lago, cláusula pagada con pérdida bruta pero d3 +22%, excluido a propósito).

## Escritura (pujar, cláusula, venta) — verificado contra cuenta real
Endpoints sin documentar oficialmente, localizados cruzando 3 proyectos independientes de la
comunidad (mismo prefijo `/v1/competition/1` que usamos nosotros) y verificados uno a uno
contra la liga real de producción:
- **Pujar**: `POST /league/{league_id}/market/{market_id}/bid`, body `{"money": cantidad}`.
  Verificado: queda `status: "pending"`, el saldo NO baja al instante (se resuelve más tarde).
- **Pagar cláusula**: `POST /league/{league_id}/buyout/{playerTeamId}/pay`, body
  `{"buyoutClauseToPay": cantidad}`. Verificado: instantáneo, saldo baja al momento. Ojo:
  usa `playerTeamId` (el hueco de plantilla, campo `SquadSlot.player_team_id`), NO el id
  genérico del jugador — con el id equivocado da un 409 "Buyout wanted to pay is not updated"
  que parece un problema de datos desactualizados pero es la identidad equivocada.
- **Poner a la venta**: `POST /league/{league_id}/market/sell`, body
  `{"playerId": playerTeamId, "salePrice": precio}`. Verificado: no cobra nada al instante,
  el juego genera ofertas (±5% del valor de mercado) en cada ciclo de mercado (21:00) durante
  unos días hasta que se acepta/rechaza/retira.
- **Retirar del mercado**: `DELETE /league/{league_id}/market/{market_id}/delete`, sin body.
  Verificado.
- **Leer ofertas pendientes**: `GET /league/{league_id}/playerTeam/{playerTeamId}/offer`
  (solo lectura). Forma de la respuesta SIN verificar todavía (nunca hemos visto una oferta
  real) — probar contra un jugador puesto a la venta antes de fiarse del parseo.
- **Aceptar/rechazar oferta**: `POST .../market/{market_id}/offer/{offer_id}/accept` (body
  `{"offerMoney": cantidad}`) / `.../reject` (sin body). SIN verificar todavía.
- Todas estas viven en `api.py` bajo `_write()` (sin reintentos — reintentar una escritura
  financiera tras un timeout podría duplicarla) y tienen su comando de prueba en `cli.py`
  (`bid`, `clause`, `sell`, `withdraw`, `offers`) con vista previa por defecto y `--confirm`
  para ejecutar de verdad.
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

## Saldo estimado de rivales (la API solo expone el tuyo)
`analysis.reconstruct_cash_flow`/`estimate_cash`, `service.estimate_rival_cash` (comando
`rivals` y `clause-risk`, y en el informe diario): el campo `teamMoney` viene `null` para
cualquier equipo que no sea el tuyo (comprobado con `probe`), así que se reconstruye sumando
todo el historial de `/activity` (paginado hacia atrás hasta que llega vacío — cubre toda la
temporada) y calibrando el presupuesto de partida con tu propio saldo real, que sí conocemos.
Asume que todos los equipos empezaron con el mismo presupuesto — es una ESTIMACIÓN, se marca
como tal en todos los informes que la usan. `clause_theft_risk`/`clause_theft_report`: de tus
jugadores con la cláusula pagable ahora, qué rivales tienen saldo estimado suficiente para
pagarla (excluye tu propio team_id explícitamente — si no, aparecías como "amenaza" de ti
mismo).

## Cláusulas especulativas (caras pero con racha fuerte y sostenida)
`analysis.speculative_clause_candidates`/`speculative_clause_verdict`/`speculative_clause_alerts`
(`service.speculative_clauses_report`, comando `clauses-hot`): cláusulas que NO pasan el filtro
"lógico" (`ratio > max_ratio`, hasta un `ratio_ceiling` de 3.0 — pagar 3x mercado no lo salva
ninguna racha) pero cuya subida de valor es fuerte (`d7 >= 15%`) Y sostenida (`not cooling`,
para no repetir el error de recomendar una racha que ya se frenó). En vez de una única
proyección a 14 días (poco fiable cuando el ritmo diario es alto: compone de forma irreal —
ver el caso real de Yoel Lago, +73%/7d, que a ritmo plano de 7d proyectaría x4 en 14 días),
se calculan dos escenarios con el ritmo de 3 días como base: optimista (se mantiene) y
pesimista (se parte a la mitad cada 3 días), y se dice en qué día de cada uno recuperarías lo
pagado. Van en un mensaje aparte, claramente marcados como alto riesgo — no se mezclan con las
cláusulas "lógicas" de `clause_alerts`.

## Rentabilidad de cláusula: contra lo que pagas, no contra el valor de mercado
`clause_verdict` proyecta el valor de mercado a 14 días (`project_value`) pero mide la
ganancia contra `price` (lo que de verdad pagas por la cláusula), no contra el valor de
mercado — si pagas por encima de mercado (ratio > 1, como Mbappé a 200 con mercado en 140) la
ganancia real es menor que comparar contra mercado; si es una ganga (ratio < 1) es mayor.
Antes de este fix se comparaba contra el valor de mercado, lo que infla la rentabilidad
aparente de cualquier cláusula pagada por encima de mercado.

## Tendencias: 7 días puede ser una racha ya muerta
`analysis.Trend` compara el ritmo de 7 días contra el de 3: `cooling` = subió a 7d pero el
corto plazo ya no lo confirma (racha vieja, no pagues de más por ella); `recovering` = al
revés, cayó a 7d pero ya remonta. `project_value` (usa el ritmo de 3 días si detecta
`cooling`/`recovering` en vez del de 7, que ya no describe el momento actual) y los veredictos
(`clause_verdict`, `market_verdict`) usan esto en vez de mirar `d7` solo — evita decir "en
racha" de un valor que ya se frenó. El campo `d7` sigue calculándose y se usa como umbral
interno (`cooling`/`recovering`, `sell_high_candidates`, el filtro de especulativas), pero ya
no se muestra en ningún mensaje (pedido explícito del usuario: "a los 7 días no lo quiero, no
me hace falta, queremos algo acotando más para ver si está para flipin"). Lo que se enseña es
`analysis.trend_words(trend)`: una frase en palabras llanas con el ritmo al día y a 3 días.

## Consejo del día (estrategia, no solo datos)
Pedido explícito del usuario: que el bot aprenda táctica de verdad (no solo reporte datos) y
aconseje a diario. Investigado por web (Comuniate, FútbolFantasy, JornadaPerfecta) en vez de
inventado — son las mismas fuentes que ya salían citando este propio repo en los resultados.
`service.daily_advice_report` cierra el informe diario con un solo consejo, priorizado: si hay
riesgo real de que te clausulen algo importante (blindar en la app — no hay endpoint de
escritura para esto, solo se lee `shielded_until` de `SquadSlot`), avisa de eso primero; si no,
si tienes mucho dinero parado (>25% del valor de tu plantilla en cash sin invertir), avisa de
eso; si no hay nada urgente, rota uno de `analysis.STRATEGY_TIPS` (principios generales de
cláusulas/economía de las guías, uno distinto cada día por `día del año % len(...)`, para no
repetir el mismo consejo en cada informe).

**Nota:** hubo una primera versión con recomendación de capitán (`analysis.pick_captain`,
mostrado en `lineup_report`). El usuario avisó de que el brazalete de capitán es una mecánica
de la versión premium del juego y esta liga no la tiene — se eliminó por completo (código y
tip relacionado en `STRATEGY_TIPS`). Si algún día hay premium de por medio, revisar el
historial de git antes de reconstruirlo desde cero.

## Botones de Telegram (pagar cláusula) — webhook en tiempo real
Elegido por el usuario: webhook (no sondeo). Cada alerta de cláusula pagable ya
(`kind == "open_affordable"`, y todas las especulativas) lleva un botón "💳 Pagar cláusula".
Los avisos "se libera en Xh" no llevan botón (aún no se puede pagar).
```
botón "c:<player_id>" → Worker cambia a [Sí, pagar "C:<id>"] [Cancelar "N:c:<id>"]
"C:<id>" → Worker quita botones + repository_dispatch(fantasy-action, action="c:<id>")
        → .github/workflows/action.yml → `python -m fantasy_agent execute-action "c:<id>"`
        → cmd_execute_action relee la plantilla del rival SIN caché, paga con playerTeamId
          y avisa por Telegram (✅ / ❌ con el motivo)
```
- El doble paso "¿Seguro?" vive en el Worker (`worker/telegram-webhook.js`); `execute-action` NO
  tiene vista previa, por eso no debe lanzarse a mano sin saber qué código pasas.
- Worker: solo atiende el `TELEGRAM_CHAT_ID` configurado, exige la cabecera secreta que Telegram
  reenvía (`set-webhook`), y valida `payload` con regex antes de disparar nada. En el workflow
  la acción entra por variable de entorno (no interpolada en el script) contra inyección.
- `action.yml` comparte el grupo de concurrencia `fantasy-watch` con el tick: dos ejecuciones
  a la vez podrían pisarse la sesión (los refresh tokens rotan). Coste conocido: si llegan a
  la vez un tick y otro tick mientras hay una acción pendiente, GitHub cancela la pendiente
  más antigua; sin confirmación de Telegram el usuario lo nota y la repite (no se mueve dinero).
- Mensajes con botones: `report_sections`/`clauses_report` devuelven `(texto, teclado|None)`;
  `notify.send_telegram(..., buttons=)` los adjunta (en mensajes troceados, solo al último).
- Para añadir otra acción (pujar, vender, retirar): nueva entrada en `ACTIONS` del Worker, rama
  nueva en `cmd_execute_action`, y botón en el mensaje correspondiente. Hoy solo existe `c`.
- Estado (2026-09-21): desplegado y verificado de punta a punta — doble confirmación, cancelar,
  dispatch a GitHub y respuesta por Telegram — usando un botón de prueba con un id inexistente
  (`c:99999999`, termina en "❌ ya no está disponible", sin pagar nada). El pago real por botón
  aún no se ha ejercitado con dinero: la primera cláusula real pagada así es la prueba final.
  Token de GitHub del Worker caduca el 21/09/2027 (si falla el botón con "GitHub respondió
  401", renovarlo y repetir `npx wrangler secret put GITHUB_TOKEN`).
- Puesta en marcha (lo hace el usuario, requiere sus cuentas): `cd worker && npx wrangler deploy`,
  `npx wrangler secret put` de TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_WEBHOOK_SECRET,
  GITHUB_TOKEN (PAT con permiso sobre el repo), GITHUB_REPO ("owner/repo"); luego
  `python -m fantasy_agent set-webhook <url-del-worker> <mismo-secreto>`.

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
