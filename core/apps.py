"""Core uygulama — AppContext başlatma."""
from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "core"
    verbose_name = "YapımMaliyet Core"

    def ready(self) -> None:
        # Sunucu başlarken AppContext'i hazırla
        from .context import init_context
        try:
            init_context()
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "AppContext başlatılamadı: %s — bazı sayfalar boş görünebilir.", exc
            )
