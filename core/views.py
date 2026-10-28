"""YapımMaliyet web — sayfalar."""
from __future__ import annotations

from decimal import Decimal

from django.http import HttpRequest, HttpResponse, Http404
from django.shortcuts import render
from django.views.decorators.http import require_GET

from .context import get_ctx


# ---------- yardımcı -------------------------------------------------------

def _ctx_or_error(request: HttpRequest):
    """AppContext yoksa hata sayfası döndür; varsa None döndür."""
    if get_ctx() is None:
        return render(request, "error.html", {
            "msg": "Veri dizini bulunamadı veya bağlantı kurulamadı. "
                   "YAPIMMALIYET_VERI ortam değişkenini kontrol edin."
        }, status=503)
    return None


def _money(val) -> str:
    if val is None:
        return "—"
    try:
        d = Decimal(str(val))
    except Exception:
        return str(val)
    return f"{d:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# ---------- ana sayfa -------------------------------------------------------

@require_GET
def home(request: HttpRequest) -> HttpResponse:
    err = _ctx_or_error(request)
    if err:
        return err
    ctx = get_ctx()
    from yapimmaliyet.services.dashboard_service import DashboardService
    from yapimmaliyet.services.project_service import ProjectService

    dash = DashboardService(ctx.conn)
    overview = dash.overview()
    projects = ProjectService(ctx.conn).list_projects(include_archived=False)
    dist = [
        {"name": name, "amount": _money(amount), "pct": float(amount / overview.distribution_total * 100) if overview.distribution_total else 0}
        for name, amount in overview.distribution
    ]
    return render(request, "home.html", {
        "page": "home",
        "overview": overview,
        "dist": dist,
        "dist_total": _money(overview.distribution_total),
        "projects": projects,
        "activity": overview.activity,
    })


# ---------- projeler --------------------------------------------------------

@require_GET
def projects(request: HttpRequest) -> HttpResponse:
    err = _ctx_or_error(request)
    if err:
        return err
    ctx = get_ctx()
    from yapimmaliyet.services.project_service import ProjectService
    from yapimmaliyet.services.calc_service import CalcService

    svc = ProjectService(ctx.conn)
    calc = CalcService(ctx.conn)
    project_list = svc.list_projects(include_archived=True)

    rows = []
    for p in project_list:
        estimates = svc.list_estimates(p.id)
        cost = None
        for est in estimates:
            snaps = [s for s in calc.snapshots(est.id) if s.kind == "YAKLASIK"]
            if snaps:
                payload = calc.snapshot_payload(max(snaps, key=lambda s: s.id).id)
                total = payload.get("totals", {}).get("total_excl_vat")
                if total is not None:
                    cost = (cost or Decimal(0)) + Decimal(str(total))
        rows.append({
            "project": p,
            "estimate_count": len(estimates),
            "cost": _money(cost) if cost is not None else "—",
        })

    return render(request, "projects.html", {
        "page": "projects",
        "rows": rows,
    })


@require_GET
def project_detail(request: HttpRequest, project_id: int) -> HttpResponse:
    err = _ctx_or_error(request)
    if err:
        return err
    ctx = get_ctx()
    from yapimmaliyet.services.project_service import ProjectService
    from yapimmaliyet.services.calc_service import CalcService
    from yapimmaliyet.infra.errors import NotFoundError

    svc = ProjectService(ctx.conn)
    calc = CalcService(ctx.conn)
    try:
        project = svc.get_project(project_id)
    except NotFoundError:
        raise Http404

    estimates = svc.list_estimates(project_id)
    est_rows = []
    for est in estimates:
        items = svc.items(est.id)
        snaps = [s for s in calc.snapshots(est.id) if s.kind == "YAKLASIK"]
        cost = None
        if snaps:
            payload = calc.snapshot_payload(max(snaps, key=lambda s: s.id).id)
            cost = payload.get("totals", {}).get("total_excl_vat")
        est_rows.append({
            "estimate": est,
            "item_count": len(items),
            "cost": _money(cost) if cost is not None else "—",
        })

    return render(request, "project_detail.html", {
        "page": "projects",
        "project": project,
        "est_rows": est_rows,
    })


# ---------- kütüphane -------------------------------------------------------

@require_GET
def library(request: HttpRequest) -> HttpResponse:
    err = _ctx_or_error(request)
    if err:
        return err
    ctx = get_ctx()
    from yapimmaliyet.services.library_service import LibraryService
    from yapimmaliyet.domain.models import SearchFilter

    svc = LibraryService(ctx.conn)
    authorities = svc.list_authorities()
    books = svc.list_books()

    q = request.GET.get("q", "").strip()
    authority_id = request.GET.get("authority")
    book_id = request.GET.get("book")
    page = max(1, int(request.GET.get("page", 1)))
    per_page = 100

    f = SearchFilter(
        text=q,
        authority_id=int(authority_id) if authority_id else None,
        book_id=int(book_id) if book_id else None,
        exclude_drafts=True,
    )
    total = svc.count(f)
    prices = svc.search(f, offset=(page - 1) * per_page, limit=per_page)
    pages = (total + per_page - 1) // per_page

    price_rows = [
        {
            "code": p.code,
            "description": p.description,
            "unit": p.unit,
            "price": _money(p.unit_price),
            "authority": p.authority_name,
            "book": p.book_title,
            "year": p.book_year,
        }
        for p in prices
    ]

    return render(request, "library.html", {
        "page": "library",
        "authorities": authorities,
        "books": books,
        "prices": price_rows,
        "total": total,
        "q": q,
        "sel_authority": authority_id or "",
        "sel_book": book_id or "",
        "cur_page": page,
        "pages": pages,
    })


# ---------- raporlar --------------------------------------------------------

@require_GET
def reports(request: HttpRequest) -> HttpResponse:
    err = _ctx_or_error(request)
    if err:
        return err
    ctx = get_ctx()
    from yapimmaliyet.services.project_service import ProjectService

    svc = ProjectService(ctx.conn)
    project_list = svc.list_projects(include_archived=False)

    REPORT_TYPES = [
        ("YAKLASIK_MALIYET", "Yaklaşık Maliyet"),
        ("KESIF_LISTESI", "Keşif Listesi"),
        ("METRAJ", "Metraj"),
        ("BIRIM_FIYAT_LISTESI", "Birim Fiyat Listesi"),
        ("DISIPLIN_DAGILIM", "Disiplin Dağılımı"),
    ]

    return render(request, "reports.html", {
        "page": "reports",
        "projects": project_list,
        "report_types": REPORT_TYPES,
    })


def _report_response(request: HttpRequest, project_id: int, report_type: str, fmt: str) -> HttpResponse:
    err = _ctx_or_error(request)
    if err:
        return err
    ctx = get_ctx()
    import tempfile
    from pathlib import Path
    from yapimmaliyet.services.project_service import ProjectService
    from yapimmaliyet.services.report_service import ReportService, ReportKind
    from yapimmaliyet.infra.errors import NotFoundError, ValidationError

    try:
        project = ProjectService(ctx.conn).get_project(project_id)
    except NotFoundError:
        raise Http404

    try:
        kind = ReportKind(report_type)
    except ValueError:
        raise Http404

    svc = ReportService(ctx.conn)
    try:
        doc = svc.build(kind, project_id=project_id)
    except (NotFoundError, ValidationError):
        raise Http404

    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / f"report.{fmt}"
        try:
            svc.export(doc, out_path)
        except Exception:
            raise Http404
        data = out_path.read_bytes()

    fname = f"{project.name}_{report_type}.{fmt}"
    if fmt == "pdf":
        ct = "application/pdf"
    else:
        ct = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    response = HttpResponse(data, content_type=ct)
    response["Content-Disposition"] = f'attachment; filename="{fname}"'
    return response


@require_GET
def report_pdf(request: HttpRequest, project_id: int, report_type: str) -> HttpResponse:
    return _report_response(request, project_id, report_type, "pdf")


@require_GET
def report_xlsx(request: HttpRequest, project_id: int, report_type: str) -> HttpResponse:
    return _report_response(request, project_id, report_type, "xlsx")


# ---------- ayarlar ---------------------------------------------------------

@require_GET
def settings_view(request: HttpRequest) -> HttpResponse:
    ctx = get_ctx()
    from yapimmaliyet import __version__
    info = {
        "version": __version__,
        "data_dir": str(ctx.paths.data_dir) if ctx else "—",
        "schema": ctx.schema_version if ctx else "—",
        "db_ok": ctx is not None,
    }
    return render(request, "settings.html", {
        "page": "settings",
        "info": info,
    })
