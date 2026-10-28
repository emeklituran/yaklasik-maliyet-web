"""YapımMaliyet web — URL yapılandırması."""
from django.urls import include, path

urlpatterns = [
    path("", include("core.urls")),
]
