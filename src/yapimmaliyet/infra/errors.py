"""Uygulama genelindeki hata sınıfları.

Her hata, kullanıcıya gösterilecek Türkçe ve çözüm öneren bir mesaj taşır.
Teknik ayrıntılar günlüğe yazılır, kullanıcı mesajına eklenmez.
"""


class AppError(Exception):
    """Kullanıcıya gösterilebilecek Türkçe mesajı olan temel hata."""

    def __init__(self, user_message: str, *, detail: str | None = None):
        super().__init__(user_message if detail is None else f"{user_message} ({detail})")
        self.user_message = user_message
        self.detail = detail


class DataDirNotWritableError(AppError):
    pass


class AlreadyRunningError(AppError):
    pass


class InsufficientDiskSpaceError(AppError):
    pass


class DatabaseCorruptError(AppError):
    pass


class MigrationError(AppError):
    pass


class SchemaTooNewError(AppError):
    pass


class BackupError(AppError):
    pass


class ValidationError(AppError):
    """Kullanıcı girdisi iş kurallarına uymuyor."""


class NotFoundError(AppError):
    pass


class ConflictError(AppError):
    """İşlem mevcut verilerle çelişiyor (mükerrer kayıt, kullanımda olan kayıt vb.)."""
