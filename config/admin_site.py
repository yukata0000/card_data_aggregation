from __future__ import annotations

from django.contrib.admin import AdminSite
from django.http import HttpRequest


class SuperuserOnlyAdminSite(AdminSite):
    """
    /admin/ を superuser のみに限定する AdminSite。
    （is_staff だけでは入れないようにする）
    """

    site_header = "Data Aggregation 管理"
    site_title = "Data Aggregation 管理"
    index_title = "管理メニュー"

    def has_permission(self, request: HttpRequest) -> bool:
        user = request.user
        return bool(user and user.is_active and user.is_superuser)


# 使い回し用インスタンス（urls/admin登録側で import して利用する）
superuser_admin_site = SuperuserOnlyAdminSite(name="superuser_admin")


