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

## Mapa
- `auth.py`     login Azure B2C (PKCE) y refresh de tokens
- `api.py`      rutas de la API (GET únicamente)
- `models.py`   normalización defensiva del JSON (claves alternativas por campo)
- `analysis.py` tendencias, puntuación de oportunidades, alarmas de cláusula (puro, testeable)
- `lineup.py`   mejor once legal por puntos esperados
- `attendance.py` estima % de titularidad por histórico de jornadas jugadas (gratis, sin APIs externas)
- `service.py`  construye el estado de la liga y los informes de texto
- `cli.py`      comandos, bucle `watch` y `tick` (una pasada, para GitHub Actions/cron)
- `.github/workflows/watch.yml` ejecuta `tick` cada ~30 min en GitHub Actions (repo público, estado en `actions/cache`)
- `tests/`      `python -m unittest discover -s tests -v` (sin red, API falsa)

## Primeros pasos tras el login real
1. `probe /v1/competition/1/leagues`, `probe /v1/competition/1/leagues/<id>/standing`,
   `probe /v1/competition/1/league/<id>/market` y `probe /v1/competition/1/leagues/<id>/teams/<teamId>`.
2. Comparar con las claves de `models.py` y ajustar las que no coincidan.
3. Actualizar los fixtures de `tests/test_offline.py` con la forma real.
