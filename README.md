# ⚽ fantasy-agent

Tu analista de **LaLiga Fantasy**: estudia el mercado, las plantillas y cláusulas de tus
rivales, las subidas y bajadas de valor, estima titularidades por histórico de jornadas y te
propone el once. Te lo manda por **Telegram**, con alertas cuando una cláusula interesante
se desbloquea o te pueden clausular a uno de los tuyos.

> **Solo lectura.** No puja, no vende, no paga cláusulas ni guarda alineaciones: te dice
> qué hacer y tú lo ejecutas en la app.

> ⚠️ **Aviso.** Usa la API *interna* de la app (no oficial ni documentada). Automatizarla
> probablemente incumple las condiciones de LaLiga Fantasy y podría acarrear sanciones en
> tu cuenta; además, LaLiga puede cambiar la API y romper el programa. Úsalo bajo tu
> responsabilidad y con un ritmo de peticiones moderado.

## Qué hace

| Comando | Qué obtienes |
|---|---|
| `market` | Ranking de oportunidades: puntos por millón, tendencia, precio vs valor, estado, pujas, saldo |
| `trends` | Quién sube y quién baja (1/3/7 días) y cuáles de tus jugadores conviene vender |
| `rivals` | Clasificación, valor de plantilla, top jugadores y cláusulas abiertas de cada rival |
| `clauses` | 🔓 cláusulas rivales que se desbloquean pronto · 💰 cláusulas que puedes pagar ya · 🛡️ tus jugadores en riesgo |
| `lineup --news` | Once legal con más puntos esperados, con el próximo rival de cada jugador (🏠/✈️) |
| `report --news --telegram` | Todo lo anterior, en un mensaje de Telegram por especialidad (alineación / mercado / cláusulas), no un tocho único |
| `watch` | Vigilancia continua: alertas nuevas cada ~30 min + informe diario |

El mercado separa dos cosas distintas: oportunidades **para tu once** (puntos por millón, medias)
y una lista aparte 💹 **para invertir** (comprar barato y revender: solo mira si está en subida y
aún infravalorado, no si es buen jugador). Tendencias también avisa de 🏔️ tuyos "en máximo" —
venderlos ya antes de que empiecen a bajar.

## Instalación

```bash
cd fantasy-agent
cp .env.example .env      # rellena lo que vayas a usar
python3 -m unittest discover -s tests -v   # comprueba que todo va (sin red)
```

Sin dependencias: Python 3.10+ y nada más.

## 1. Iniciar sesión (una vez)

LaLiga Fantasy solo va en móvil, pero su login OAuth acepta el navegador del ordenador.
**Tú introduces tu contraseña en la web oficial de LaLiga; el programa nunca la ve.**

```bash
python3 -m fantasy_agent auth url
```

1. En Chrome abre DevTools (F12) → **Network** → marca **Preserve log**.
2. Pega la URL que imprime el comando e inicia sesión (Google/Apple/email, como en la app).
3. La página se queda en blanco: es lo normal. En Network, la **última fila `(canceled)`**
   empieza por `?state=…&code=…` → clic derecho → **Copy link address**.
4. Canjéala (con comillas):

```bash
python3 -m fantasy_agent auth code 'authredirect://com.lfp.laligafantasy/?state=...&code=...'
```

El refresh token mantiene la sesión viva; no deberías repetirlo. `auth status` para comprobarlo.

## 2. Primera prueba real

```bash
python3 -m fantasy_agent leagues     # tus ligas (pon FANTASY_LEAGUE_ID si tienes varias)
python3 -m fantasy_agent standing    # ids de equipos (pon FANTASY_TEAM_ID si no te detecta)
python3 -m fantasy_agent report
```

Si algún dato sale a `?` o `0`, la API ha cambiado algún nombre de campo:

```bash
python3 -m fantasy_agent probe /v1/competition/1/league/<LIGA>/market
```

y ajusta la clave en `fantasy_agent/models.py` (o pídeselo a Claude Code pegándole el JSON).

## 3. Telegram

1. Habla con **@BotFather** → `/newbot` → copia el token a `TELEGRAM_BOT_TOKEN`.
2. Escribe cualquier cosa a tu bot y abre `https://api.telegram.org/bot<TOKEN>/getUpdates`:
   el `chat.id` va a `TELEGRAM_CHAT_ID`.
3. `python3 -m fantasy_agent clauses --telegram`

## 4. Titularidad estimada

`lineup --news` (sin ninguna clave ni coste) mira, para cada jugador de tu plantilla, cuántas
de sus últimas 5 jornadas completadas ha sumado puntos (`weekPoints` de la API pública de
jugadores) como proxy de si suele jugar, y con eso pondera el once recomendado. No sabe el
motivo si no juega (lesión, sanción, suplente...) — para eso ya se usa el estado real
(`playerStatus`) que trae la propia API de LaLiga Fantasy, sin depender de esto.

## 5. Dejarlo corriendo 24/7

**Opción A — GitHub Actions (gratis, sin servidor propio).** El repo debe ser público (el
código no tiene datos personales; tu sesión y tokens van en Secrets, nunca en el repo). El
workflow `.github/workflows/watch.yml` ejecuta `python -m fantasy_agent tick` cada ~30 min
(una sola pasada de `watch`) y persiste `data/` (sesión + dedupe de alertas) con
`actions/cache` entre ejecuciones. Secrets necesarios: `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`, `FANTASY_TOKENS_JSON` (contenido inicial de `data/tokens.json` tras tu
login local, para arrancar la sesión la primera vez), y opcionalmente
`FANTASY_LEAGUE_ID`/`FANTASY_TEAM_ID`. GitHub desactiva los cron de un repo tras 60 días sin
actividad: si pasas mucho tiempo sin tocarlo, basta un commit cualquiera para reactivarlo.

**Opción B — Raspberry o servidor propio:**

```bash
python3 -m fantasy_agent watch
```

o como servicio con `deploy/fantasy-watch.service`. Alternativa con cron:

```cron
*/30 * * * *  cd /home/pi/fantasy-agent && python3 -m fantasy_agent tick
0 9 * * *     cd /home/pi/fantasy-agent && python3 -m fantasy_agent report --news --telegram
```

## Cómo puntúa

- **Oportunidad de mercado** = puntos por millón + tendencia a 3 días + descuento sobre el
  valor + media de puntos − penalización por lesión/duda o si no te llega el saldo.
- **Cláusula interesante** = cuesta ≤1,6× su valor (con media ≥3) o es un jugador de media ≥6
  a ≤2,2× su valor.
- **Riesgo propio** = tu cláusula es ≤1,25× el valor y está abierta o se abre pronto.
- **Once** = media de puntos × % titularidad (× 0,55 si es duda, 0 si lesionado/sancionado),
  probando todas las formaciones legales.

Todos los umbrales están en `analysis.py` y `lineup.py`: ajústalos a tu liga.

## Próximos pasos con Claude Code
- Estimar el **saldo de los rivales** a partir del historial de fichajes (`/activity`) para
  saber quién puede pagarte una cláusula.
- Recomendación de **puja máxima** según tendencia y competencia.
- Panel web con histórico en SQLite.
