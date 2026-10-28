"""Startup preferences live outside the selected database; cancellation never opens it."""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from uuid import uuid4

from yapimmaliyet.infra.paths import DATA_DIR_ENV, app_base_dir
from yapimmaliyet.infra.settings import save_settings


@dataclass
class StartupPlan:
    data_dir: str
    company_name: str = ""
    contact_name: str = ""
    backup_keep_count: int = 10
    backup_on_exit: bool = True
    show_tour: bool = False
    stage: int = 0
    complete: bool = False
    hidden_modules: list[str] = field(default_factory=list)   # kenar menüde gizlenen modül başlıkları
    create_sample: bool = False                               # kurulumdan sonra örnek çalışma eklensin mi


LAST_STAGE = 7   # Hoş geldiniz, veri klasörü, firma, tercihler, modüller, yedekleme, örnek proje, tamamla


def config_path() -> Path:
    override = os.environ.get("YAPIMMALIYET_BASLANGIC")
    if override:
        return Path(override).resolve()
    data = os.environ.get(DATA_DIR_ENV)
    if data:
        target = Path(data).resolve()
        return target.parent / f".{target.name}-baslangic.json"
    return app_base_dir() / "baslangic.json"


def default_plan() -> StartupPlan:
    return StartupPlan(str(Path(os.environ.get(DATA_DIR_ENV) or app_base_dir() / "Veri").resolve()))


def load_plan(path: Path) -> StartupPlan:
    if not path.exists():
        return default_plan()
    try:   # Not Defteri gibi düzenleyiciler dosyanın başına BOM ekleyebilir; utf-8-sig ikisini de okur
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise ValueError("Başlangıç kaydı okunamadı (metin kodlaması tanınmadı). Veri klasörünü yeniden seçin.") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Başlangıç kaydı bozuk (satır {exc.lineno}, sütun {exc.colno}). "
                         "Veri klasörünü yeniden seçin.") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("data_dir"), str) or not raw["data_dir"].strip():
        raise ValueError("Başlangıç kaydı geçersiz. Veri klasörünü yeniden seçin.")
    plan = StartupPlan(**{k: v for k, v in raw.items() if k in {f.name for f in fields(StartupPlan)}})
    folder = Path(plan.data_dir)
    if not folder.is_absolute():
        if folder.drive or folder.root or ".." in folder.parts:
            raise ValueError("Başlangıç veri klasörü geçersiz. Veri klasörünü yeniden seçin.")
        # taşınabilir kullanım: program klasörüne göre yol (flash diskte sürücü harfi değişebilir)
        plan.data_dir = str((path.parent / folder).resolve())
    if not isinstance(plan.stage, int) or not 0 <= plan.stage <= LAST_STAGE or not isinstance(plan.complete, bool):
        raise ValueError("Başlangıç adımı geçersiz.")
    if not isinstance(plan.backup_keep_count, int) or not 1 <= plan.backup_keep_count <= 500:
        raise ValueError("Yedek sayısı geçersiz.")
    if not isinstance(plan.hidden_modules, list) or not all(
            isinstance(v, str) and len(v) <= 100 for v in plan.hidden_modules) or len(plan.hidden_modules) > 50:
        raise ValueError("Modül seçimi geçersiz.")
    if not all(isinstance(v, bool) for v in (plan.backup_on_exit, plan.show_tour, plan.create_sample)):
        raise ValueError("Başlangıç tercihleri geçersiz.")
    if not all(isinstance(v, str) and len(v) <= 200 for v in (plan.company_name, plan.contact_name)):
        raise ValueError("Firma bilgileri geçersiz.")
    return plan


def portable_dir(base: Path, data_dir: str) -> str:
    """Veri klasörü başlangıç kaydının klasörü içindeyse göreli yol döner; flash disk başka harfle takılsa da açılır."""
    if not data_dir.strip() or not Path(data_dir).is_absolute():
        return data_dir
    try:
        return Path(data_dir).resolve().relative_to(base.resolve()).as_posix() or "."
    except ValueError:
        return data_dir


def save_plan(path: Path, plan: StartupPlan) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = asdict(plan)
    data["data_dir"] = portable_dir(path.parent, plan.data_dir)
    content = json.dumps(data, ensure_ascii=False, indent=2)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    tmp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    tmp.write_text(content, encoding="utf-8")
    for attempt in range(4):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 3:
                raise
            time.sleep(0.03 * (attempt + 1))


def inspect_folder(text: str) -> tuple[Path, bool]:
    if not text.strip():
        raise ValueError("Veri klasörünü seçin.")
    path = Path(text.strip()).expanduser()
    if not path.is_absolute():
        raise ValueError("Tam bir klasör yolu seçin (örneğin C:\\YapimMaliyet\\Veri).")
    path = path.resolve()
    if path.exists() and not path.is_dir():
        raise ValueError("Seçilen yol bir klasör olmalıdır.")
    db = path / "yapimmaliyet.db"
    if db.exists() and not db.is_file():
        raise ValueError("Veritabanı yolu bir dosya olmalıdır.")
    exists = db.is_file()
    if path.exists() and not exists and any(path.iterdir()):
        raise ValueError("Bu klasör dolu ama YapımMaliyet veritabanı içermiyor. Boş bir klasör seçin.")
    return path, exists


def ready(plan: StartupPlan) -> bool:
    return plan.complete and (Path(plan.data_dir) / "yapimmaliyet.db").is_file()


def finish_setup(ctx, plan: StartupPlan, path: Path) -> None:
    settings = replace(ctx.settings, company_name=plan.company_name.strip(), contact_name=plan.contact_name.strip(),
                       backup_keep_count=plan.backup_keep_count, backup_on_exit=plan.backup_on_exit,
                       show_tour=plan.show_tour, hidden_modules=list(plan.hidden_modules), setup_completed=True)
    save_settings(ctx.paths.settings_file, settings)
    ctx.settings = settings
    ctx.backup.keep_count = settings.backup_keep_count
    save_plan(path, replace(plan, data_dir=str(ctx.paths.data_dir), complete=True, stage=LAST_STAGE,
                            create_sample=False))
