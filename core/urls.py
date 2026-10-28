"""URL yapılandırması — tüm sayfalar tek uygulamada."""
from django.urls import path
from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("projeler/", views.projects, name="projects"),
    path("projeler/<int:project_id>/", views.project_detail, name="project_detail"),
    path("kutüphane/", views.library, name="library"),
    path("raporlar/", views.reports, name="reports"),
    path("raporlar/<int:project_id>/<str:report_type>/pdf/", views.report_pdf, name="report_pdf"),
    path("raporlar/<int:project_id>/<str:report_type>/xlsx/", views.report_xlsx, name="report_xlsx"),
    path("ayarlar/", views.settings_view, name="settings"),
]
