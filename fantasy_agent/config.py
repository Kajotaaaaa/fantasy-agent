"""Configuración: variables de entorno (.env) y rutas."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Carga un .env sencillo (CLAVE=valor) sin dependencias externas."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if value and value[0] in "\"'":
            quote = value[0]
            end = value.find(quote, 1)
            value = value[1:end] if end != -1 else value[1:]
        else:
            value = value.split("#", 1)[0].strip()
        os.environ.setdefault(key.strip(), value)


ROOT = Path(__file__).resolve().parent.parent
_load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    league_id: str | None
    team_id: str | None
    telegram_token: str | None
    telegram_chat_id: str | None
    clause_window_hours: int
    watch_interval_min: int
    report_hour: int
    request_delay_s: float

    @property
    def tokens_file(self) -> Path:
        return self.data_dir / "tokens.json"

    @property
    def pending_auth_file(self) -> Path:
        return self.data_dir / "pending_auth.json"

    @property
    def db_file(self) -> Path:
        return self.data_dir / "fantasy.sqlite3"


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("FANTASY_DATA_DIR", ROOT / "data")).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        data_dir=data_dir,
        league_id=os.environ.get("FANTASY_LEAGUE_ID") or None,
        team_id=os.environ.get("FANTASY_TEAM_ID") or None,
        telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID") or None,
        clause_window_hours=int(os.environ.get("CLAUSE_WINDOW_HOURS", "24")),
        watch_interval_min=int(os.environ.get("WATCH_INTERVAL_MIN", "30")),
        report_hour=int(os.environ.get("REPORT_HOUR", "9")),
        request_delay_s=float(os.environ.get("REQUEST_DELAY_S", "0.4")),
    )
