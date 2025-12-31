from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import streamlit as st


_DJANGO_INIT_LOCK = threading.Lock()


@st.cache_resource
def init_django() -> None:
    """
    Streamlit から Django ORM を使うための初期化。
    - リポジトリ直下を sys.path に追加
    - DJANGO_SETTINGS_MODULE を設定
    - django.setup()
    - 可能なら migrate を実行（初回起動時のDB作成/更新用）
    """

    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

    import django  # noqa: WPS433 (runtime import is intentional)
    from django.apps import apps as django_apps  # noqa: WPS433

    # Streamlit Cloud では同時実行で初期化が競合することがあるため、ロック＋ガードで保護する
    with _DJANGO_INIT_LOCK:
        if django_apps.ready:
            return

        try:
            django.setup()
        except RuntimeError as e:  # noqa: BLE001
            # 別スレッドが populate 中に再入すると発生する。ready になるまで待って回避する。
            if "populate() isn't reentrant" in str(e):
                for _ in range(50):  # 最大約5秒待つ
                    if django_apps.ready:
                        return
                    time.sleep(0.1)
            raise

    # DBが無い/古い場合に備えて migrate を実行（例外が出てもUIは落とさない）
    try:
        from django.core.management import call_command  # noqa: WPS433

        call_command("migrate", interactive=False, verbosity=0)
    except Exception as e:  # noqa: BLE001
        # Streamlit Cloud等でDBが読み取り専用/環境不足の場合でも最低限起動させる
        st.warning(f"Django migrate をスキップしました: {e}")


