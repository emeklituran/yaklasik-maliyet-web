"""Ana sayfa için durum bilgisi."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from yapimmaliyet import __version__
from yapimmaliyet.services.app_context import AppContext

_COUNTED_TABLES = {
    "authorities": "İdare",
    "price_books": "Fiyat kitabı",
    "position_prices": "Poz fiyat kaydı",
    "custom_positions": "Özel poz",
    "projects": "Proje",
}


@dataclass(frozen=True)
class AppStatus:
    app_version: str
    schema_version: int
    data_dir: str
    last_backup: datetime | None
    backup_count: int
    counts: dict[str, int]
    warnings: list[str]


def gather_status(ctx: AppContext) -> AppStatus:
    counts = {label: ctx.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
              for table, label in _COUNTED_TABLES.items()}
    backups = ctx.backup.list_backups()
    return AppStatus(
        app_version=__version__,
        schema_version=ctx.schema_version,
        data_dir=str(ctx.paths.data_dir),
        last_backup=backups[0].created_at if backups else None,
        backup_count=len(backups),
        counts=counts,
        warnings=list(ctx.warnings),
    )
