#!/usr/bin/env python
"""YapımMaliyet web yönetim aracı."""
import os
import sys
from pathlib import Path

# src/ dizinini path'e ekle — bağımsız web deposunu ve üst projedeki eski yerleşimi destekle.
ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT if (ROOT / "src").is_dir() else ROOT.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "yapweb.settings")

if __name__ == "__main__":
    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)
