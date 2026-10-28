"""Hakediş dosyasını türüne göre (taşeron / idari) oluşturup Excel ya da PDF olarak kaydeder."""

from __future__ import annotations

import re
from pathlib import Path

from yapimmaliyet.infra.errors import AppError
from yapimmaliyet.reports.hakedis_idari import idari_packet
from yapimmaliyet.reports.hakedis_taseron import taseron_packet
from yapimmaliyet.reports.packet import Packet
from yapimmaliyet.reports.packet_pdf import write_packet_pdf
from yapimmaliyet.reports.packet_xlsx import write_packet_xlsx


def build_packet(view) -> Packet:
    return taseron_packet(view) if view.contract.kind == "TASERON" else idari_packet(view)


def default_file_name(view, suffix: str) -> str:
    kind = "Taseron" if view.contract.kind == "TASERON" else "Idari"
    title = re.sub(r"[^\w\-]+", "_", view.contract.title, flags=re.UNICODE).strip("_")[:40]
    p = view.payment
    rev = f"_rev{p.revision}" if p.revision else ""
    state = "_TASLAK" if view.draft else ""
    return f"Hakedis_{kind}_{title}_{p.no:02d}{rev}{state}{suffix}"


def export_payment(view, path: Path) -> Path:
    path = Path(path)
    packet = build_packet(view)
    try:
        if path.suffix.lower() == ".xlsx":
            return write_packet_xlsx(packet, path)
        if path.suffix.lower() == ".pdf":
            return write_packet_pdf(packet, path)
    except PermissionError as exc:
        raise AppError(f"Dosya kaydedilemedi; başka bir programda açık olabilir: {path.name}") from exc
    raise AppError("Hakediş dosyası yalnızca .xlsx ya da .pdf olarak kaydedilebilir.")
