from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import os
from io import BytesIO, StringIO
import csv
import zipfile
from typing import Any, Optional

import streamlit as st

# Streamlit Cloud では `streamlit_app/streamlit_app.py` をディレクトリ直下として実行するため、
# `streamlit_app.django_bootstrap` のようなパッケージ参照だと同名ファイル解決の衝突が起きうる。
# 同一ディレクトリのモジュールとして import する。
from django_bootstrap import init_django


@dataclass(frozen=True)
class AuthState:
    user_id: int
    username: str


def _require_django() -> None:
    init_django()


def _get_auth_state() -> Optional[AuthState]:
    raw = st.session_state.get("auth")
    if not isinstance(raw, dict):
        return None
    if "user_id" not in raw or "username" not in raw:
        return None
    try:
        return AuthState(user_id=int(raw["user_id"]), username=str(raw["username"]))
    except Exception:
        return None


def _set_auth_state(user_id: int, username: str) -> None:
    st.session_state["auth"] = {"user_id": int(user_id), "username": str(username)}


def _logout() -> None:
    st.session_state.pop("auth", None)


def _get_user(user_id: int):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    return User.objects.filter(id=user_id).first()


def _login_ui() -> None:
    st.subheader("ログイン / 初期セットアップ")

    # 直前の復元が完了して rerun した場合に表示
    if st.session_state.pop("sqlite_restore_done", False):
        st.success("db.sqlite3 を復元しました。ログインしてください。")

    # --- 初期復元（未ログイン） ---
    with st.expander("初期セットアップ（未ログインで復元）", expanded=False):
        st.caption(
            "SQLiteの `db.sqlite3` を復元すればユーザー情報も含めて戻せます。"
            "（本アプリからの新規ユーザー作成は行いません）"
        )

        st.markdown("#### 1) SQLite(db.sqlite3) をアップロードして復元（ユーザーも含む）")
        # 復元先パスを明示（Cloudでもここに書き込みます）
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
        db_path = os.path.join(repo_root, "db.sqlite3")
        st.caption(f"復元先: `{db_path}`")

        # PostgreSQL設定が有効だと SQLite を書き換えても反映されないため注意喚起
        use_postgres_env = (os.getenv("USE_POSTGRES") or "").strip().lower() in {"1", "true", "yes", "on"}
        if use_postgres_env:
            st.warning("環境変数 `USE_POSTGRES=1` が設定されています。現在はPostgreSQL優先のため、SQLite復元は反映されません。")

        setup_token_required = (os.getenv("SETUP_TOKEN") or "").strip()
        token_ok = True
        if setup_token_required:
            token_in = st.text_input("SETUP_TOKEN", type="password", key="setup_token_input")
            token_ok = token_in == setup_token_required
            st.caption("`SETUP_TOKEN` が設定されているため、入力が一致した場合のみ復元できます。")

        uploaded_db = st.file_uploader(
            "db.sqlite3 を選択（または db.sqlite3 を含むZIP）",
            type=["sqlite3", "db", "sqlite", "zip"],
            key="upload_sqlite_db",
        )
        if st.button(
            "SQLiteを復元して再起動",
            type="primary",
            use_container_width=True,
            disabled=(uploaded_db is None or not token_ok or use_postgres_env),
        ):
            try:
                raw = uploaded_db.getvalue()
                # ZIPの場合は中の db.sqlite3（または *.sqlite3 / *.db / *.sqlite）を探して取り出す
                if (uploaded_db.name or "").lower().endswith(".zip"):
                    with zipfile.ZipFile(BytesIO(raw), mode="r") as z:
                        candidates = [
                            n for n in z.namelist() if n.lower().endswith(("db.sqlite3", ".sqlite3", ".db", ".sqlite"))
                        ]
                        if not candidates:
                            st.error("ZIP内に db.sqlite3（または .sqlite3/.db/.sqlite）が見つかりません。")
                            st.stop()
                        preferred = [n for n in candidates if n.lower().endswith("db.sqlite3")]
                        target_name = preferred[0] if preferred else candidates[0]
                        raw = z.read(target_name)
                        st.caption(f"ZIPから `{target_name}` を取り出しました。")

                with open(db_path, "wb") as f:
                    f.write(raw)

                # キャッシュが残ると古いDB接続のままになるため全消し
                try:
                    init_django.clear()  # type: ignore[attr-defined]
                except Exception:
                    pass
                try:
                    st.cache_resource.clear()
                    st.cache_data.clear()
                except Exception:
                    pass

                st.session_state["sqlite_restore_done"] = True
                st.rerun()
            except Exception as e:  # noqa: BLE001
                st.error(f"復元に失敗しました: {e}")

        st.divider()
        st.markdown("#### 2) ZIPバックアップの復元について")
        st.caption("ZIPバックアップの復元はログイン後に「バックアップ/復元」ページから実行してください。")

    # ここから Django が必要
    _require_django()
    col1, col2 = st.columns(2)
    with col1:
        username = st.text_input("ユーザー名", key="login_username")
    with col2:
        password = st.text_input("パスワード", type="password", key="login_password")

    cols = st.columns(2)
    with cols[0]:
        if st.button("ログイン", use_container_width=True):
            from django.contrib.auth import authenticate

            user = authenticate(username=username, password=password)
            if user is None:
                st.error("ユーザー名またはパスワードが違います。")
            else:
                _set_auth_state(user.id, user.username)
                st.rerun()


def _active_decks(user) -> list[str]:
    from dashbords.models import Deck

    return list(Deck.objects.filter(user=user, is_active=True).order_by("name", "id").values_list("name", flat=True))


def _active_opponent_decks(user):
    from dashbords.models import OpponentDeck

    return list(OpponentDeck.objects.filter(user=user, is_active=True).order_by("name", "id"))


def _ensure_user() -> Any:
    auth = _get_auth_state()
    if not auth:
        st.info("まずログインしてください。")
        _login_ui()
        st.stop()

    _require_django()
    user = _get_user(auth.user_id)
    if user is None:
        st.warning("ログイン情報が無効になりました。再ログインしてください。")
        _logout()
        st.rerun()
    return user


def _page_input(user) -> None:
    from dashbords.models import Result

    st.subheader("対戦結果の入力")

    decks = _active_decks(user)
    opp_decks = _active_opponent_decks(user)

    col1, col2, col3 = st.columns(3)
    with col1:
        input_date = st.date_input("日付", value=date.today())
    with col2:
        used_deck = st.selectbox("使用デッキ", options=(decks if decks else [""]), index=0)
        if not decks:
            used_deck = st.text_input("使用デッキ（自由入力）", value="")
    with col3:
        opp_options = [("", "（未選択）")] + [(str(d.id), d.name) for d in opp_decks]
        opp_selected = st.selectbox("対面デッキ", options=opp_options, format_func=lambda x: x[1])

    col4, col5 = st.columns(2)
    with col4:
        play_order = st.radio("先行/後攻", options=["先行", "後攻"], horizontal=True)
    with col5:
        match_result = st.radio("勝敗", options=["〇", "×", "両敗"], horizontal=True)

    note = st.text_area("備考", value="", height=120)

    if st.button("保存", type="primary", use_container_width=True):
        opponent_deck_obj = None
        if opp_selected and opp_selected[0]:
            from dashbords.models import OpponentDeck

            opponent_deck_obj = (
                OpponentDeck.objects.filter(user=user, is_active=True, id=opp_selected[0]).first()
            )

        Result.objects.create(
            user=user,
            date=input_date,
            used_deck=used_deck or "",
            opponent_deck=opponent_deck_obj,
            play_order=play_order or "",
            match_result=match_result,
            note=note or "",
        )
        st.success("保存しました。")


def _results_queryset(user, filters: dict[str, Any]):
    from dashbords.models import Result
    from django.db.models import Q

    qs = Result.objects.filter(user=user).select_related("opponent_deck")

    date_from = filters.get("date_from")
    date_to = filters.get("date_to")
    used_deck = (filters.get("used_deck") or "").strip()
    opponent_deck_id = (filters.get("opponent_deck_id") or "").strip()
    play_order = (filters.get("play_order") or "").strip()
    match_result = (filters.get("match_result") or "").strip()
    q = (filters.get("q") or "").strip()

    if date_from:
        qs = qs.filter(date__gte=date_from)
    if date_to:
        qs = qs.filter(date__lte=date_to)
    if used_deck:
        qs = qs.filter(used_deck=used_deck)
    if opponent_deck_id:
        qs = qs.filter(opponent_deck__id=opponent_deck_id, opponent_deck__user=user)
    if play_order:
        qs = qs.filter(play_order=play_order)
    if match_result:
        qs = qs.filter(match_result=match_result)
    if q:
        qs = qs.filter(Q(note__icontains=q) | Q(used_deck__icontains=q) | Q(opponent_deck__name__icontains=q))

    sort_key = (filters.get("sort") or "date").strip()
    sort_dir = (filters.get("dir") or "desc").strip()
    allowed_sort = {
        "date": "date",
        "used_deck": "used_deck",
        "opponent_deck": "opponent_deck__name",
        "play_order": "play_order",
        "match_result": "match_result",
        "id": "id",
    }
    sort_field = allowed_sort.get(sort_key, "date")
    prefix = "-" if sort_dir == "desc" else ""
    return qs.order_by(f"{prefix}{sort_field}", f"{prefix}id")


def _page_results(user) -> None:
    from dashbords.models import Deck, OpponentDeck, Result

    st.subheader("結果一覧")

    with st.expander("フィルタ / ソート", expanded=True):
        c1, c2, c3 = st.columns(3)
        with c1:
            date_from = st.date_input("開始日（任意）", value=None, key="filter_date_from")
        with c2:
            date_to = st.date_input("終了日（任意）", value=None, key="filter_date_to")
        with c3:
            q = st.text_input("キーワード（備考/デッキ名）", value="", key="filter_q")

        used_deck_values_from_master = list(
            Deck.objects.filter(user=user, is_active=True).order_by("name", "id").values_list("name", flat=True)
        )
        used_deck_values_from_results = list(
            Result.objects.filter(user=user).exclude(used_deck="").values_list("used_deck", flat=True).distinct()
        )
        used_deck_values = sorted({*used_deck_values_from_master, *used_deck_values_from_results})
        opp_decks = list(OpponentDeck.objects.filter(user=user).order_by("name", "id"))
        opp_options = [("", "（全て）")] + [(str(d.id), d.name) for d in opp_decks]

        c4, c5, c6 = st.columns(3)
        with c4:
            used_deck = st.selectbox("使用デッキ", options=[""] + used_deck_values, format_func=lambda x: x or "（全て）")
        with c5:
            opponent_deck = st.selectbox("対面デッキ", options=opp_options, format_func=lambda x: x[1])[0]
        with c6:
            play_order = st.selectbox("先行/後攻", options=["", "先行", "後攻"], format_func=lambda x: x or "（全て）")

        c7, c8, c9 = st.columns(3)
        with c7:
            match_result = st.selectbox("勝敗", options=["", "〇", "×", "両敗"], format_func=lambda x: x or "（全て）")
        with c8:
            sort = st.selectbox(
                "ソートキー",
                options=["date", "id", "used_deck", "opponent_deck", "play_order", "match_result"],
                format_func=lambda x: {
                    "date": "日付",
                    "id": "ID",
                    "used_deck": "使用デッキ",
                    "opponent_deck": "対面デッキ",
                    "play_order": "先行/後攻",
                    "match_result": "勝敗",
                }[x],
            )
        with c9:
            dir_ = st.selectbox("昇順/降順", options=["desc", "asc"], format_func=lambda x: "降順" if x == "desc" else "昇順")

    filters = {
        "date_from": date_from,
        "date_to": date_to,
        "used_deck": used_deck,
        "opponent_deck_id": opponent_deck,
        "play_order": play_order,
        "match_result": match_result,
        "q": q,
        "sort": sort,
        "dir": dir_,
    }

    results = list(_results_queryset(user, filters)[:2000])
    st.caption(f"表示件数: {len(results)}（最大2000件）")

    rows: list[dict[str, Any]] = []
    for r in results:
        rows.append(
            {
                "selected": False,
                "id": r.id,
                "date": r.date.isoformat(),
                "used_deck": r.used_deck,
                "opponent_deck": (r.opponent_deck.name if r.opponent_deck else ""),
                "play_order": r.play_order,
                "match_result": r.match_result,
                "note": r.note,
            }
        )

    edited = st.data_editor(
        rows,
        hide_index=True,
        use_container_width=True,
        disabled=["id"],
        column_config={
            "selected": st.column_config.CheckboxColumn("選択"),
            # id は内部的に選択/編集/削除に使うが、表には表示しない
            "id": None,
            "date": st.column_config.TextColumn("日付", width="small"),
            "used_deck": st.column_config.TextColumn("使用デッキ", width="medium"),
            "opponent_deck": st.column_config.TextColumn("対面デッキ", width="medium"),
            "play_order": st.column_config.TextColumn("先行/後攻", width="small"),
            "match_result": st.column_config.TextColumn("勝敗", width="small"),
            "note": st.column_config.TextColumn("備考", width="large"),
        },
        key="results_editor",
    )

    selected_ids = [int(r["id"]) for r in edited if r.get("selected")]

    c1, c2 = st.columns(2)
    with c1:
        if st.button("選択を削除", type="secondary", use_container_width=True, disabled=(not selected_ids)):
            Result.objects.filter(user=user, id__in=selected_ids).delete()
            st.success(f"{len(selected_ids)}件 削除しました。")
            st.rerun()
    with c2:
        with st.popover("選択を編集（1件）", use_container_width=True):
            if len(selected_ids) != 1:
                st.info("編集は1件選択のみ対応です。")
            else:
                target_id = selected_ids[0]
                target = Result.objects.filter(user=user, id=target_id).select_related("opponent_deck").first()
                if target is None:
                    st.error("対象が見つかりません。")
                else:
                    opps = _active_opponent_decks(user)
                    opp_opts = [("", "（未選択）")] + [(str(d.id), d.name) for d in opps]

                    ed_date = st.date_input("日付", value=target.date, key="edit_date")
                    ed_used_deck = st.text_input("使用デッキ", value=target.used_deck, key="edit_used_deck")
                    ed_opp = st.selectbox(
                        "対面デッキ",
                        options=opp_opts,
                        format_func=lambda x: x[1],
                        index=0,
                        key="edit_opponent_deck",
                    )
                    ed_play = st.selectbox("先行/後攻", options=["", "先行", "後攻"], index=(1 if target.play_order == "先行" else 2 if target.play_order == "後攻" else 0))
                    ed_result = st.selectbox("勝敗", options=["〇", "×", "両敗"], index=(0 if target.match_result == "〇" else 1 if target.match_result == "×" else 2))
                    ed_note = st.text_area("備考", value=target.note, height=120, key="edit_note")

                    if st.button("更新", type="primary", use_container_width=True, key="edit_submit"):
                        opponent_deck_obj = None
                        if ed_opp and ed_opp[0]:
                            opponent_deck_obj = OpponentDeck.objects.filter(user=user, is_active=True, id=ed_opp[0]).first()
                        target.date = ed_date
                        target.used_deck = ed_used_deck or ""
                        target.opponent_deck = opponent_deck_obj
                        target.play_order = ed_play or ""
                        target.match_result = ed_result
                        target.note = ed_note or ""
                        target.save()
                        st.success("更新しました。")
                        st.rerun()


def _page_analysis(user) -> None:
    from dashbords.models import Result
    from django.db.models import Count, Q

    st.subheader("分析")

    qs = Result.objects.filter(user=user)

    total_matches = qs.count()
    overall_win = qs.filter(match_result="〇").count()
    overall_loss = qs.filter(match_result="×").count()
    overall_other = total_matches - overall_win - overall_loss
    overall_decided = overall_win + overall_loss
    overall_win_rate = ((overall_win / overall_decided) * 100.0) if overall_decided else None

    c1, c2, c3, c4 = st.row(4)
    c1.metric("総対戦数", total_matches)
    c2.metric("勝ち", overall_win)
    c3.metric("負け", overall_loss)
    c4.metric("勝率（%）", "-" if overall_win_rate is None else f"{overall_win_rate:.1f}%")

    st.divider()
    st.markdown("#### 先行/後攻別")
    play_order_summary = []
    for label in ["先行", "後攻"]:
        po_total = qs.filter(play_order=label).count()
        po_win = qs.filter(play_order=label, match_result="〇").count()
        po_loss = qs.filter(play_order=label, match_result="×").count()
        po_other = po_total - po_win - po_loss
        po_decided = po_win + po_loss
        po_win_rate = ((po_win / po_decided) * 100.0) if po_decided else None
        play_order_summary.append(
            {
                "play_order": label,
                "total": po_total,
                "win": po_win,
                "loss": po_loss,
                "other": po_other,
                "win_rate": None if po_win_rate is None else round(po_win_rate, 1),
            }
        )
    po_unknown_total = qs.filter(Q(play_order="") | Q(play_order__isnull=True)).count()
    st.caption(f"先行/後攻 未入力: {po_unknown_total}")
    st.dataframe(play_order_summary, use_container_width=True, hide_index=True)

    st.divider()
    st.markdown("#### 使用デッキごとの集計")
    per_deck_rows = (
        qs.values("used_deck")
        .annotate(
            total=Count("id"),
            win=Count("id", filter=Q(match_result="〇")),
            loss=Count("id", filter=Q(match_result="×")),
        )
        .order_by("-total", "used_deck")
    )
    per_deck = []
    for r in per_deck_rows:
        used_deck = (r.get("used_deck") or "").strip() or "（未入力）"
        win = int(r.get("win") or 0)
        loss = int(r.get("loss") or 0)
        total = int(r.get("total") or 0)
        other = total - win - loss
        decided = win + loss
        win_rate = ((win / decided) * 100.0) if decided else None
        per_deck.append(
            {
                "used_deck": used_deck,
                "total": total,
                "win": win,
                "loss": loss,
                "other": other,
                "win_rate": "-" if win_rate is None else f"{win_rate:.1f}%",
            }
        )
    st.dataframe(per_deck, use_container_width=True, hide_index=True)

    st.divider()
    st.markdown("#### (使用デッキ × 対面デッキ) の集計")
    matchup_rows = (
        qs.values("used_deck", "opponent_deck__name")
        .annotate(
            total=Count("id"),
            win=Count("id", filter=Q(match_result="〇")),
            loss=Count("id", filter=Q(match_result="×")),
        )
        .order_by("-total", "used_deck", "opponent_deck__name")
    )
    matchups = []
    for r in matchup_rows:
        used_deck = (r.get("used_deck") or "").strip() or "（未入力）"
        opponent_deck = (r.get("opponent_deck__name") or "").strip() or "（不明）"
        win = int(r.get("win") or 0)
        loss = int(r.get("loss") or 0)
        total = int(r.get("total") or 0)
        other = total - win - loss
        decided = win + loss
        win_rate = ((win / decided) * 100.0) if decided else None
        matchups.append(
            {
                "used_deck": used_deck,
                "opponent_deck": opponent_deck,
                "total": total,
                "win": win,
                "loss": loss,
                "other": other,
                "win_rate": "-" if win_rate is None else f"{win_rate:.1f}%",
            }
        )
    st.dataframe(matchups, use_container_width=True, hide_index=True)


def _page_master(user) -> None:
    from dashbords.models import Deck, OpponentDeck

    st.subheader("設定")

    tab1, tab2 = st.tabs(["使用デッキ", "対面デッキ"])

    with tab1:
        decks = list(Deck.objects.filter(user=user).order_by("-is_active", "name", "id"))
        st.caption(f"{len(decks)} 件")
        rows = [{"id": d.id, "name": d.name, "is_active": bool(d.is_active)} for d in decks]
        edited = st.data_editor(
            rows,
            hide_index=True,
            use_container_width=True,
            disabled=["id"],
            column_config={
                "id": st.column_config.NumberColumn("ID", width="small"),
                "name": st.column_config.TextColumn("デッキ名"),
                "is_active": st.column_config.CheckboxColumn("有効"),
            },
            key="deck_editor",
        )
        if st.button("更新（使用デッキ）", use_container_width=True, key="deck_update"):
            for r in edited:
                Deck.objects.filter(user=user, id=int(r["id"])).update(name=str(r["name"]), is_active=bool(r["is_active"]))
            st.success("更新しました。")
            st.rerun()

        with st.popover("追加（使用デッキ）"):
            new_name = st.text_input("デッキ名", key="deck_add_name")
            if st.button("追加", type="primary", use_container_width=True, key="deck_add_submit"):
                if not new_name.strip():
                    st.error("デッキ名は必須です。")
                elif Deck.objects.filter(user=user, name=new_name.strip()).exists():
                    st.error("同名デッキが既に存在します。")
                else:
                    Deck.objects.create(user=user, name=new_name.strip(), is_active=True)
                    st.success("追加しました。")
                    st.rerun()

    with tab2:
        decks = list(OpponentDeck.objects.filter(user=user).order_by("-is_active", "name", "id"))
        st.caption(f"{len(decks)} 件")
        rows = [{"id": d.id, "name": d.name, "is_active": bool(d.is_active)} for d in decks]
        edited = st.data_editor(
            rows,
            hide_index=True,
            use_container_width=True,
            disabled=["id"],
            column_config={
                "id": st.column_config.NumberColumn("ID", width="small"),
                "name": st.column_config.TextColumn("デッキ名"),
                "is_active": st.column_config.CheckboxColumn("有効"),
            },
            key="opp_deck_editor",
        )
        if st.button("更新（対面デッキ）", use_container_width=True, key="opp_deck_update"):
            for r in edited:
                OpponentDeck.objects.filter(user=user, id=int(r["id"])).update(
                    name=str(r["name"]), is_active=bool(r["is_active"])
                )
            st.success("更新しました。")
            st.rerun()

        with st.popover("追加（対面デッキ）"):
            new_name = st.text_input("デッキ名", key="opp_deck_add_name")
            if st.button("追加", type="primary", use_container_width=True, key="opp_deck_add_submit"):
                if not new_name.strip():
                    st.error("デッキ名は必須です。")
                elif OpponentDeck.objects.filter(user=user, name=new_name.strip()).exists():
                    st.error("同名デッキが既に存在します。")
                else:
                    OpponentDeck.objects.create(user=user, name=new_name.strip(), is_active=True)
                    st.success("追加しました。")
                    st.rerun()


def _get_db_info() -> dict[str, Any]:
    """
    現在のDB設定情報を取得する（表示/バックアップ判断用）。
    """
    from django.conf import settings

    default = settings.DATABASES.get("default", {})
    engine = str(default.get("ENGINE") or "")
    name = default.get("NAME")
    return {"engine": engine, "name": name}


def _export_user_data_zip(user) -> bytes:
    """
    ログインユーザーのデータをZIPでエクスポートする。
    - decks.csv
    - opponent_decks.csv
    - results.csv
    """
    from dashbords.models import Deck, OpponentDeck, Result

    decks = list(Deck.objects.filter(user=user).order_by("id"))
    opps = list(OpponentDeck.objects.filter(user=user).order_by("id"))
    results = list(Result.objects.filter(user=user).select_related("opponent_deck").order_by("id"))

    def write_csv(rows: list[dict[str, Any]], fieldnames: list[str]) -> bytes:
        s = StringIO()
        w = csv.DictWriter(s, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
        return s.getvalue().encode("utf-8")

    deck_rows = [{"name": d.name, "is_active": int(bool(d.is_active))} for d in decks]
    opp_rows = [{"name": d.name, "is_active": int(bool(d.is_active))} for d in opps]
    result_rows = [
        {
            "date": r.date.isoformat(),
            "used_deck": r.used_deck,
            "opponent_deck": (r.opponent_deck.name if r.opponent_deck else ""),
            "play_order": r.play_order,
            "match_result": r.match_result,
            "note": r.note,
        }
        for r in results
    ]

    buf = BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("decks.csv", write_csv(deck_rows, ["name", "is_active"]))
        z.writestr("opponent_decks.csv", write_csv(opp_rows, ["name", "is_active"]))
        z.writestr(
            "results.csv",
            write_csv(
                result_rows,
                ["date", "used_deck", "opponent_deck", "play_order", "match_result", "note"],
            ),
        )
    return buf.getvalue()


def _import_user_data_zip(user, zip_bytes: bytes, *, purge_before_import: bool) -> dict[str, int]:
    """
    ZIP(上記export形式)からログインユーザーのデータを復元する。
    - purge_before_import=True の場合は対象ユーザーの既存データを削除してから取り込む
    """
    from dashbords.models import Deck, OpponentDeck, Result
    from django.db import transaction

    def parse_csv(content: bytes) -> list[dict[str, str]]:
        text = content.decode("utf-8", errors="replace")
        r = csv.DictReader(StringIO(text))
        return [dict(row) for row in r]

    with zipfile.ZipFile(BytesIO(zip_bytes), mode="r") as z:
        names = set(z.namelist())
        decks_csv = parse_csv(z.read("decks.csv")) if "decks.csv" in names else []
        opp_csv = parse_csv(z.read("opponent_decks.csv")) if "opponent_decks.csv" in names else []
        results_csv = parse_csv(z.read("results.csv")) if "results.csv" in names else []

    counters = {"decks": 0, "opponent_decks": 0, "results": 0}

    with transaction.atomic():
        if purge_before_import:
            Result.objects.filter(user=user).delete()
            Deck.objects.filter(user=user).delete()
            OpponentDeck.objects.filter(user=user).delete()

        # Decks
        for row in decks_csv:
            name = (row.get("name") or "").strip()
            if not name:
                continue
            is_active = (row.get("is_active") or "").strip() in {"1", "true", "True", "yes", "on"}
            obj, created = Deck.objects.get_or_create(user=user, name=name, defaults={"is_active": is_active})
            if not created and obj.is_active != is_active:
                obj.is_active = is_active
                obj.save(update_fields=["is_active"])
            counters["decks"] += 1

        # OpponentDecks
        for row in opp_csv:
            name = (row.get("name") or "").strip()
            if not name:
                continue
            is_active = (row.get("is_active") or "").strip() in {"1", "true", "True", "yes", "on"}
            obj, created = OpponentDeck.objects.get_or_create(user=user, name=name, defaults={"is_active": is_active})
            if not created and obj.is_active != is_active:
                obj.is_active = is_active
                obj.save(update_fields=["is_active"])
            counters["opponent_decks"] += 1

        # Results（常に追記。opponent_deck は名前で紐付け）
        for row in results_csv:
            raw_date = (row.get("date") or "").strip()
            try:
                d = date.fromisoformat(raw_date) if raw_date else date.today()
            except ValueError:
                d = date.today()

            used_deck = (row.get("used_deck") or "").strip()
            opponent_deck_name = (row.get("opponent_deck") or "").strip()
            play_order = (row.get("play_order") or "").strip()
            match_result = (row.get("match_result") or "").strip() or "〇"
            note = (row.get("note") or "").strip()

            opponent_deck_obj = None
            if opponent_deck_name:
                opponent_deck_obj, _ = OpponentDeck.objects.get_or_create(
                    user=user, name=opponent_deck_name, defaults={"is_active": True}
                )

            Result.objects.create(
                user=user,
                date=d,
                used_deck=used_deck,
                opponent_deck=opponent_deck_obj,
                play_order=play_order,
                match_result=match_result,
                note=note,
            )
            counters["results"] += 1

    return counters


def _page_backup_restore(user) -> None:
    st.subheader("バックアップ / 復元")

    db = _get_db_info()
    engine = db["engine"]
    name = db["name"]

    st.markdown("#### 重要")
    st.info(
        "Streamlit Community Cloud のファイルシステムは揮発します。"
        "SQLite運用の場合、再起動/再デプロイ等でデータが消える可能性があるため、"
        "定期的にバックアップをダウンロードしてください。"
    )

    st.markdown("#### バックアップ（ZIP）")
    zip_bytes = _export_user_data_zip(user)
    st.download_button(
        "ログインユーザーのデータをZIPでダウンロード",
        data=zip_bytes,
        file_name=f"data_aggregation_backup_user_{user.id}.zip",
        mime="application/zip",
        use_container_width=True,
    )

    st.markdown("#### SQLite DBファイルのダウンロード（SQLite利用時のみ）")
    if "sqlite3" in engine.lower() and name:
        try:
            # NAME は Path か文字列
            path_str = str(name)
            with open(path_str, "rb") as f:
                db_bytes = f.read()
            st.caption(f"DB: `{path_str}`")
            st.download_button(
                "db.sqlite3 をダウンロード",
                data=db_bytes,
                file_name="db.sqlite3",
                mime="application/octet-stream",
                use_container_width=True,
            )
        except Exception as e:  # noqa: BLE001
            st.warning(f"SQLite DBファイルを読み取れませんでした: {e}")
    else:
        st.caption("現在はSQLiteではありません（またはDBパスが不明）。")

    st.divider()
    st.markdown("#### 復元（ZIPアップロード）")
    st.warning("復元はログインユーザーの範囲にのみ反映されます。")
    purge = st.checkbox("取り込み前に自分の既存データを全削除してから復元する", value=False)
    up = st.file_uploader("バックアップZIPを選択", type=["zip"])
    if up is not None:
        if st.button("復元を実行", type="primary", use_container_width=True):
            counters = _import_user_data_zip(user, up.getvalue(), purge_before_import=purge)
            st.success(f"復元しました: decks={counters['decks']} opponent_decks={counters['opponent_decks']} results={counters['results']}")
            st.rerun()


def main() -> None:
    st.set_page_config(page_title="Data Aggregation (Streamlit)", layout="wide")

    st.title("試合結果集計ツール")  

    auth = _get_auth_state()
    with st.sidebar:
        st.markdown("### メニュー")
        if auth:
            st.success(f"ログイン中: {auth.username}")
            if st.button("ログアウト", use_container_width=True):
                _logout()
                st.rerun()
        else:
            st.info("未ログイン")

        page = st.radio(
            "ページ",
            options=["ログイン", "入力", "結果一覧", "分析", "設定", "バックアップ/復元"],
            index=0 if not auth else 1,
        )

    if page == "ログイン":
        if auth:
            st.info("すでにログイン済みです。")
        _login_ui()
        return

    user = _ensure_user()

    if page == "入力":
        _page_input(user)
        return
    if page == "結果一覧":
        _page_results(user)
        return
    if page == "分析":
        _page_analysis(user)
        return
    if page == "マスタ管理":
        _page_master(user)
        return
    if page == "バックアップ/復元":
        _page_backup_restore(user)
        return


if __name__ == "__main__":
    main()


