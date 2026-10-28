"""WSGI girişi — gunicorn, uWSGI ve benzeri için."""
import os
import sys
from pathlib import Path

# src/ — bağımsız web deposunu ve üst projedeki eski yerleşimi destekle.
BASE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BASE_DIR if (BASE_DIR / "src").is_dir() else BASE_DIR.parent
_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "yapweb.settings")

from django.core.wsgi import get_wsgi_application  # noqa: E402

application = get_wsgi_application()
