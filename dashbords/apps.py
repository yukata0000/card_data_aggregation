from django.apps import AppConfig


class DashbordsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "dashbords"

    def ready(self) -> None:
        # custom AdminSite に対する register を確実に実行するために読み込む
        from . import admin  # noqa: F401


