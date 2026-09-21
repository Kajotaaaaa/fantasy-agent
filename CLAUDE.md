# Contexto para Claude Code

Agente de análisis para LaLiga Fantasy. Python 3.10+, **solo librería estándar**.

## Reglas del proyecto
- **SOLO LECTURA por defecto.** No añadir llamadas que modifiquen el juego salvo que el
  usuario lo pida explícitamente, y en ese caso siempre con confirmación previa por operación.
  Ya existen escrituras verificadas contra la cuenta real (ver sección "Escritura" abajo):
  todas siguen el patrón vista-previa-por-defecto + `--confirm` para ejecutar de verdad, y
  nunca se deben disparar automáticamente sin que el usuario apruebe esa operación concreta.

## Flipeo autónomo (`flip.py`) — la única parte que mueve dinero sin confirmar
Decisión explícita del usuario (2026-09-21): flipeo autónomo, real desde el primer día, tope del
25% del saldo. Interruptor `FLIP_MODE` (variable del repositorio en GitHub: Settings > Secrets
and variables > Actions > Variables; lo lee `watch.yml`): `on` ejecuta, `shadow` solo cuenta lo
que haría por Telegram, vacío/otro = apagado (por defecto). Para PARAR: ponerla a `off`.
`python -m fantasy_agent flip` prueba en sombra contra el mercado real (siempre sombra: el
estado real vive en la caché de Actions, no en el SQLite local; no ejecutar `watch` local con
`FLIP_MODE=on` a la vez que el tick de Actions, duplicaría operaciones).
- Ciclo (estado en el `Store`: `flip_pending:<anuncio>`, `flip_held:<jugador>`): una vez al día
  (primer tick tras `REPORT_HOUR`, cuando ya hay tendencias) `_buy` puja; cada tick
  `_resolve_pending` mira si se ganó (el jugador aparece en tu plantilla tras el cierre 21:02)
  y `_list_held` pone a la venta lo ganado a valor de mercado (1 intento al día, 5 máx). Solo se
  toca lo que el bot compró: nunca vende algo de tu once por su cuenta.
- Reglas de compra (`flip_amount`/`plan_bids`): solo candidatos de `_investment_picks` (suben,
  precio ≤ 105% del valor), que sigan subiendo AHORA (hoy ≥ +0.5% y ≥ la mitad del ritmo diario
  de los 3 últimos días: una subida que se frena no vale) y sin `cooling`. Lección real
  (2026-09-21): la primera compra forzada fue Pablo García (+41% en 9 días, pero +7.5%, +6.7%,
  ... ayer +1.8%, hoy +0.18%): pasó el filtro "d1 > 0" y se pujó 8.47M, un 2.2% sobre su valor,
  porque `bid_plan` proyectaba el +4.4% de 3 días como si siguiera. Ahora `bid_plan` proyecta 3
  días con el menor de d1 y d3/3, y `flip_amount` exige ese ritmo mínimo. Puja = mínimo o
  puja con margen, nunca > 103% del valor de mercado; comprometido (pujas pendientes + coste de
  lo comprado) ≤ 25% de (saldo + coste de lo comprado), cada puja ≤ la mitad de ese tope, ≤ 4
  flips abiertos y ≤ 3 pujas nuevas por día; lo pujado nunca supera el saldo (prohibido
  quedarse en negativo). No puja por lo que sale en "Mercado para tu once" (es tu lista de
  fichajes manuales; la API no deja ver tus propias pujas, así que no se puede saber si ya
  pujaste a mano).
- **PENDIENTE (bloqueante): aceptar/rechazar ofertas.** Nunca se ha visto una oferta real
  (`player_offers`/`accept_offer` sin verificar). Hasta verificarlo con la primera oferta real
  (Iván Azón, ciclo de las 21:00) el bot NO acepta nada: las ofertas las decides tú. Al
  implementarlo usar `analysis.flip_decision` + `squad_can_field_eleven` (reglas del usuario más
  abajo) y verificar el formato antes de activar la aceptación automática.

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
  **Pujar dos veces al mismo anuncio NO se puede**: el segundo POST da 400 `030.01.09 "Team
  has pending bid in this player"` (Pablo García, 2026-09-21; el usuario creía que la nueva
  puja sobrescribía a la anterior — falso por este endpoint). Para cambiar una puja existe
  `PUT /league/{id}/market/{market_id}/bid/{bid_id}` (encontrado con OPTIONS/405, sin probar el
  cuerpo — probablemente `{"money": cantidad}`), pero hace falta el `bid_id` que devuelve el
  POST original (`"id"` en la respuesta) y NO hay ninguna ruta para listar las pujas propias
  (probadas ~25 rutas, todas 404): ahora el flipeo lo guarda en `flip_pending` (`bid_id`) y el
  aviso de puja por botón lo muestra. La puja de Pablo García (8.47M) se hizo sin guardarlo y
  no se puede modificar.
  **Cantidad**: no vale pujar siempre el precio pedido. Si el anuncio pide menos que el valor
  de mercado actual del jugador, el servidor responde 400 `030.01.01 "\"15207008\" is not a
  valid money quantity for this player"` (Yuri, 2026-09-21: pedía 15.21M, valía 15.52M; el
  precio del anuncio se fija en el ciclo de las 21:00 y ronda ±2-3% del valor de entonces).
  `service.bid_amount` puja el mayor de precio pedido y valor de mercado. Hipótesis
  consistente con los datos (Cestero, que pedía 0.1% más de lo que valía, sí coló) pero sin
  confirmar del todo: si aún falla con `bid_amount`, mirar el error antes de tocar nada más.
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

**Fiabilidad medida (2026-09-21) — baja en valor absoluto:**
- El historial está completo (3 páginas, 461 eventos, desde el 04/08, ids sin duplicar) y los
  importes de los eventos son cifras de dinero reales: la cláusula de Yoel Lago (12.550.633)
  coincide EXACTAMENTE con lo que bajó mi saldo (153.144.766 → 140.594.133).
- El blindaje no cuesta dinero (importe 0 en el feed y es gratuito en el juego oficial): no falta
  nada por ahí. `teamMoney` de los rivales es `null` tanto en `standing` como en `teams/{id}`.
- Reglas del juego que importan aquí (búsqueda web, 2026-09-21): el saldo PUEDE ser negativo
  temporalmente (deuda máx. 20% del valor de la plantilla) pero debe ser ≥ 0 cuando empieza la
  jornada, y NO se puede pagar una cláusula endeudándose (hace falta el dinero). Además, según
  el usuario, cada mánager arranca con 11 jugadores + el dinero que falte hasta un valor
  común, así que el presupuesto inicial NO tiene por qué ser igual para todos (el modelo asume
  que sí).
- Calibrando contra tu saldo real el presupuesto inicial sale 57.140.754 (nada redondo). Con
  la regla "tras pagar una cláusula el saldo es ≥ 0", el inicio mínimo de cada mánager sale
  entre 80M y 99M (C4STILL0FC 84.0, dmoral08 95.7, Paneq 98.9, icb30 87.0, Kajota 96.5,
  paauu10 80.3): un grupo compacto compatible con que todos arrancaran con ~100M. Para ti eso
  daría 183.45M frente a los 140.59M reales: quedan ~42.9M de salida de dinero SIN explicar
  (no es un % limpio: 8.3% de tus ventas o de tus compras a LaLiga, 15% de las cláusulas
  cobradas, 19.5% de las pagadas). No están localizados. El (-70M..) "saldo negativo imposible"
  que se anotó primero era un error: la deuda está permitida; lo sólido es la restricción de
  cláusulas.
- **Modelo mejorado (mismo VALOR TOTAL inicial, no mismo dinero):** el juego da a cada mánager
  una plantilla inicial (siempre 14 jugadores, comprobado en los 6) y el dinero justo hasta un
  valor común. `analysis.initial_squad_ids` reconstruye esa plantilla a partir del historial
  (jugadores cuyo primer movimiento fue perderlos + los de hoy sin movimientos) y
  `service.initial_squad_values` los valora en la fecha en que el mánager se unió (histórico de
  valores; se cachea en el Store como `sv0:<manager_id>`, ~14 llamadas por mánager la primera
  vez). Valor de plantilla inicial: 130-143M por mánager. `estimate_cash` despeja el valor
  común con tu saldo real: `saldo_rival = tu saldo + (tu plantilla inicial - la suya) + (su
  flujo - el tuyo)`. Comprobación: (inicio mínimo por cláusulas + plantilla inicial) sale 216-240M
  en los 6, o sea un valor común de ~240-250M compatible con todo. Cambió los saldos: C4STILL0FC
  105→93M, dmoral08 8→4M, Paneq 31→19M, icb30 41→34M, paauu10 46→40M.
- **Lo que sigue sin explicar:** con ese modelo tu propio saldo debería ser mayor de lo que es
  en ~53-63M (según el valor común, 240-250M): una salida de dinero que el historial no recoge.
  `service.audit_my_cash` (cada tick) compara el cambio REAL de tu saldo con lo que predicen
  los movimientos nuevos y manda "🔎 Auditoría del saldo" (✅ coincide / ⚠️ diferencia con el %
  del importe): tras unos días de compras, ventas y cláusulas se verá a qué tipo de movimiento
  acompaña la diferencia. Guarda cada medida en el Store (`audit:<id>`).
- Por eso el saldo estimado es ORIENTATIVO: `analysis.CASH_UNCERTAINTY` = 40M de margen, y
  `analysis.can_bid` clasifica seguro / dudoso / no (con pujas baratas casi todo es "dudoso").
  Para un rival el saldo real estaría entre lo estimado (inicio 57M) y ~43M más (inicio ~100M
  sin esa salida sin explicar), según si la comparte.
- Cómo validarlo mejor: comparar saldo real antes/después de un evento de compra/venta a
  LaLiga (tipos 31/33) — hasta ahora solo está comprobado el de cláusula (tipo 1). Los
  cierres del mercado (21:02) y las ventas dan esa comparación exacta.
- Uso: `market`/informe diario añaden "Rivales que pueden pujar: ✅ · ❔ · ❌" a cada fichaje.

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

## Botones de Telegram (cláusula, puja, venta, retirada) — webhook en tiempo real
Elegido por el usuario: webhook (no sondeo). Cuatro acciones, código `<verbo>:<id>`:
| código | botón | dónde sale | qué ejecuta `cli._act_*` |
|---|---|---|---|
| `c:<player_id>` | 💳 Pagar cláusula | alerta de cláusula pagable ya (`open_affordable`) y todas las especulativas | relee la plantilla del rival SIN caché y paga con `playerTeamId` |
| `b:<listing_id>:<cantidad>` | 💰 mínimo / 📈 margen / 🎯 techo (hasta 3 filas por jugador) | "Mercado para tu once" + "Inversión" (un mensaje) y "Nuevo en el mercado" (solo ≥3 ★) | puja EXACTAMENTE esa cantidad; solo anuncios de LaLiga; nunca < mínimo válido, > saldo ni > 3x el mínimo |
| `s:<player_id>` | 📤 Vender <nombre> <valor> | "Candidatos a vender" (tendencia a la baja) salvo los que ya están en venta | pone a la venta a valor de mercado |
| `w:<player_id>` | ↩️ Retirar <nombre> | "En venta ahora" (`my_listings_report`, comando `listings`) | retira el anuncio |

Los avisos "se libera en Xh" no llevan botón (aún no se puede pagar).

**Tres pujas por jugador (`analysis.bid_plan`)** — porque las pujas son ciegas y no juega solo
el usuario: el mínimo es barato pero pierde contra cualquiera que ponga algo más.
- 💰 **mínimo** = `service.bid_amount` (mayor de precio pedido y valor de mercado).
- 📈 **con margen** = mínimo + la MITAD de la ganancia esperada, solo si sube (d3 > 0) y esa
  ganancia supera el 2% del mínimo. Ganancia esperada = valor de mercado proyectando el ritmo
  de 3 días otros 3 (horizonte de flipeo, sin mirar 7 días). Regala solo la mitad del beneficio.
- 🎯 **techo "lo quiero sí o sí"** = `bid_ceiling` por puntos (+30% si TOP), solo si queda
  claramente por encima de la puja anterior, capado a 2x el mínimo (con pocas referencias salió
  un techo de 12M para un medio de 0.70M) y solo con ≥3 referencias de mercado en su posición
  (`position_ppm_benchmark` devuelve 0 con menos). No sale para "Inversión" (flipeo, no once).
- La cantidad viaja en el código del botón: lo que confirmas es exactamente lo que se puja. No
  se ofrece ninguna puja que supere tu saldo (regla del usuario: prohibido quedarse en negativo).
```
botón "b:<id>" → Worker cambia SU fila a [✅ Confirmar · <etiqueta> "B:<id>"] + [❌ Cancelar "N:b:<id>"]
"B:<id>" → Worker quita esas filas + repository_dispatch(fantasy-action, action="b:<id>")
        → .github/workflows/action.yml → `python -m fantasy_agent execute-action "b:<id>"`
        → cmd_execute_action → `_ACTIONS[verbo]` → avisa por Telegram (✅ / ❌ con el motivo)
```
- Un mensaje puede llevar varias filas (una por jugador). El estado de cada fila vive en el
  propio teclado (el Worker lo lee de `callback_query.message.reply_markup`), sin base de datos:
  la etiqueta original se recupera de "✅ Confirmar · <etiqueta>" al cancelar. Si al confirmar
  el botón ya no está en el teclado (doble pulsación), no se dispara nada por segunda vez.
- `report_sections` ya no junta compra y venta en un mensaje: "Mercado + Inversión" (botones de
  puja), "Tus jugadores" (tendencias + corta-pérdidas + candidatos, botones de venta) y "En
  venta ahora" (botones de retirar, solo si tienes a alguien en venta).
- Pendientes de verificar en real: pujar y vender por botón usan los mismos endpoints ya
  verificados con `bid`/`sell`/`withdraw`, pero el circuito completo por botón solo se ha
  probado con `c:` y un id inexistente.
- El doble paso "¿Seguro?" vive en el Worker (`worker/telegram-webhook.js`); `execute-action` NO
  tiene vista previa, por eso no debe lanzarse a mano sin saber qué código pasas.
- Worker: solo atiende el `TELEGRAM_CHAT_ID` configurado, exige la cabecera secreta que Telegram
  reenvía (`set-webhook`), y valida `payload` con regex antes de disparar nada. En el workflow
  la acción entra por variable de entorno (no interpolada en el script) contra inyección.
- `action.yml` NO usa grupo de concurrencia (lección real, 2026-09-21): con uno compartido,
  GitHub solo deja 1 ejecución en marcha + 1 pendiente y CANCELA EN SILENCIO las que sobran;
  el usuario pulsó retirar a 2 jugadores seguidos y solo se retiró uno, sin ningún aviso. Para
  no pisar la sesión del tick, el workflow solo guarda la caché si `data/tokens.json` cambió
  (huella antes/después), así una acción que no renueva el token nunca deja una sesión vieja
  como la más reciente. `cmd_execute_action` no sale con error tras avisar por Telegram (si no,
  GitHub manda un correo de "workflow fallido" por cada botón que no pudo ejecutarse).
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
