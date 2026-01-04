from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import os
import time
import json
import hmac
import hashlib
import base64
from io import BytesIO, StringIO
import csv
import zipfile
from typing import Any, Optional
from http.cookies import SimpleCookie

import streamlit as st

# Streamlit Cloud では `streamlit_app/streamlit_app.py` をディレクトリ直下として実行するため、
# `streamlit_app.django_bootstrap` のようなパッケージ参照だと同名ファイル解決の衝突が起きうる。
# 同一ディレクトリのモジュールとして import する。
from django_bootstrap import init_django


@dataclass(frozen=True)
class AuthState:
    user_id: int
    username: str


AUTH_COOKIE_NAME = "da_auth"
LAST_USER_COOKIE_NAME = "da_last_user"
AUTH_TTL_SECONDS = 12 * 60 * 60  # 12時間
LAST_USER_TTL_SECONDS = 30 * 24 * 60 * 60  # 30日（ユーザー選択の利便性用。認証とは無関係）


def _inject_global_css() -> None:
    st.markdown(
        """
<style>
/* --- DataEditorの列ヘッダ操作（ドラッグ入れ替え等）を無効化して誤操作を防ぐ --- */
div[data-testid="stDataEditor"] [role="columnheader"] {
  pointer-events: none;
}

/* --- 横/縦に収まらない表はスクロールさせる --- */
.scroll-table {
  width: 100%;
  overflow-x: auto;
  overflow-y: auto;
  max-height: 70vh;
  border: 1px solid rgba(49, 51, 63, 0.2);
  border-radius: 8px;
}
.scroll-table table {
  width: max-content;
  min-width: 100%;
  border-collapse: collapse;
}
.scroll-table th, .scroll-table td {
  white-space: nowrap;
}

/* StreamlitのDataFrame/DataEditorも、はみ出す時は横スクロールできるようにする（環境差の保険） */
div[data-testid="stDataFrame"], div[data-testid="stDataEditor"] {
  overflow-x: auto;
}
</style>
""",
        unsafe_allow_html=True,
    )


def _get_sqlite_db_path() -> str:
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    return os.path.join(repo_root, "db.sqlite3")


def _cookie_secret_bytes() -> Optional[bytes]:
    """
    署名用の秘密鍵。
    追加の環境変数を増やさず、SETUP_TOKEN を秘密鍵として流用する。
    """
    s = (os.getenv("SETUP_TOKEN") or "").strip()
    return s.encode("utf-8") if s else None


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(raw: str) -> bytes:
    pad = "=" * ((4 - (len(raw) % 4)) % 4)
    return base64.urlsafe_b64decode((raw + pad).encode("ascii"))


def _sign_payload(payload_json: bytes, secret: bytes) -> str:
    sig = hmac.new(secret, payload_json, hashlib.sha256).digest()
    return _b64url_encode(sig)


def _encode_auth_token(payload: dict[str, Any], secret: bytes) -> str:
    payload_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return f"{_b64url_encode(payload_json)}.{_sign_payload(payload_json, secret)}"


def _decode_auth_token(token: str, secret: bytes) -> Optional[dict[str, Any]]:
    try:
        p, s = token.split(".", 1)
        payload_json = _b64url_decode(p)
        expected = _sign_payload(payload_json, secret)
        if not hmac.compare_digest(expected, s):
            return None
        payload = json.loads(payload_json.decode("utf-8"))
        if not isinstance(payload, dict):
            return None
        return payload
    except Exception:
        return None


def _get_cookie_value(name: str) -> Optional[str]:
    """
    Streamlitのリクエストヘッダから Cookie を読む。
    取得できない環境の場合は None。
    """
    try:
        headers = getattr(getattr(st, "context", None), "headers", None)
        if not headers:
            return None
        cookie_header = headers.get("cookie") or headers.get("Cookie")
        if not cookie_header:
            return None
        c = SimpleCookie()
        c.load(cookie_header)
        morsel = c.get(name)
        return morsel.value if morsel else None
    except Exception:
        return None


def _set_cookie_js(name: str, value: str, *, max_age_seconds: int) -> None:
    # JSでCookieをセット（Python側からレスポンスヘッダを操作できないため）
    safe_value = value.replace("\\", "\\\\").replace('"', '\\"')
    st.markdown(
        f"""
<script>
document.cookie = "{name}={safe_value}; path=/; max-age={int(max_age_seconds)}; samesite=lax";
</script>
""",
        unsafe_allow_html=True,
    )


def _delete_cookie_js(name: str) -> None:
    st.markdown(
        f"""
<script>
document.cookie = "{name}=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT; samesite=lax";
</script>
""",
        unsafe_allow_html=True,
    )


def _restore_auth_from_cookie_if_possible() -> None:
    """
    再読み込み対策:
    - session_state に auth が無い場合、署名付きCookieから復元する
    - 有効期限(12h)内のみ
    - db.sqlite3 のfingerprint（mtime/size）が一致する場合のみ
    """
    if _get_auth_state() is not None:
        return
    secret = _cookie_secret_bytes()
    if not secret:
        return
    token = _get_cookie_value(AUTH_COOKIE_NAME)
    if not token:
        return
    payload = _decode_auth_token(token, secret)
    if not payload:
        return

    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        return
    if time.time() > float(exp):
        return

    db_path = _get_sqlite_db_path()
    if not os.path.exists(db_path):
        return
    try:
        st_ = os.stat(db_path)
    except Exception:
        return
    expected_fp = {"size": int(st_.st_size), "mtime": int(st_.st_mtime)}
    if payload.get("db_fp") != expected_fp:
        return

    uid = payload.get("user_id")
    uname = payload.get("username")
    if not isinstance(uid, int) or not isinstance(uname, str) or not uname:
        return
    _set_auth_state(uid, uname)

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
    # 再読み込み後に復元されないようCookieも削除
    _delete_cookie_js(AUTH_COOKIE_NAME)

def _sync_text_from_select(*, select_key: str, text_key: str) -> None:
    """
    selectbox で選んだ値を text_input 側へ同期する。
    - これにより「プルダウンから選ぶ」→「同じ欄をクリックして文字入力で上書き」が可能になる。
    """
    v = st.session_state.get(select_key)
    if isinstance(v, str):
        # 空選択に戻した場合も入力欄を空に戻す（自然な挙動）
        st.session_state[text_key] = v.strip()


def _sync_text_from_multiselect(*, select_key: str, text_key: str) -> None:
    """
    multiselect(max_selections=1) で選んだ値を text_input 側へ同期する。
    """
    v = st.session_state.get(select_key)
    if isinstance(v, list):
        s = str(v[0]).strip() if v else ""
        st.session_state[text_key] = s


def _normalize_match_result(value: str) -> str:
    """
    旧表記（勝ち/負け/引き分け）を新表記（〇/×/両敗）に寄せる。
    DBに混在していても表示/集計が崩れないようにする。
    """
    v = (value or "").strip()
    if v == "勝ち":
        return "〇"
    if v == "負け":
        return "×"
    if v == "引き分け":
        return "両敗"
    return v


def _match_result_values_for_filter(value: str) -> list[str]:
    """
    フィルタ用：新表記を選んだ場合も旧表記を含めて検索できるようにする。
    """
    v = (value or "").strip()
    if v == "〇":
        return ["〇", "勝ち"]
    if v == "×":
        return ["×", "負け"]
    if v == "両敗":
        return ["両敗", "引き分け"]
    return [v] if v else []


def _sort_key_deck_label(label: str) -> tuple[int, str]:
    """
    表示用のデッキ名ソートキー。
    「（未入力）」「（不明）」のような特別ラベルは末尾に寄せる。
    """
    v = (label or "").strip()
    if v in {"（未入力）", "（不明）"}:
        return (1, v)
    return (0, v)


def _get_user(user_id: int):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    return User.objects.filter(id=user_id).first()


def _login_ui() -> None:
    st.subheader("ログイン")

    # 復元先パスを明示（Cloudでもここに書き込みます）
    db_path = _get_sqlite_db_path()
    db_exists = os.path.exists(db_path)

    # PostgreSQL設定が有効だと SQLite を書き換えても反映されないため注意喚起
    use_postgres_env = (os.getenv("USE_POSTGRES") or "").strip().lower() in {"1", "true", "yes", "on"}
    if use_postgres_env:
        st.warning("環境変数 `USE_POSTGRES=1` が設定されています。SQLite(db.sqlite3) でのログイン/復元は使えません。")

    setup_token_required = (os.getenv("SETUP_TOKEN") or "").strip()
    if not setup_token_required:
        st.error("環境変数 `SETUP_TOKEN` が未設定です。SETUP_TOKEN を設定してからログインしてください。")
        token_ok = False
    else:
        token_in = st.text_input("SETUP_TOKEN", type="password", key="setup_token_input")
        token_ok = token_in == setup_token_required

    # 直前の復元が完了して rerun した場合に表示
    if st.session_state.pop("sqlite_restore_done", False):
        st.success("データを読み込みました。")

    st.markdown("#### データの準備")
    uploaded_db = st.file_uploader(
        "データを選択（またはデータを含むZIP）",
        type=None,
        key="upload_sqlite_db",
    )
    if st.button(
        "データをアップロードしてログイン",
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
            else:
                # SQLiteファイルか簡易チェック（誤って別ファイルを選んだ場合の保険）
                # SQLite header: b"SQLite format 3\\x00"
                if not raw.startswith(b"SQLite format 3\x00"):
                    st.error("SQLiteファイルではない可能性があります。db.sqlite3（またはZIP）を選択してください。")
                    st.stop()

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

    # ここから Django（復元済みDBのユーザー情報を読む）
    if not db_exists:
        st.info("ログインするにはデータ（db.sqlite3）が必要です。上でアップロードしてください。")
        return
    if use_postgres_env:
        st.info("USE_POSTGRES=1 のため、SQLiteでのログインはできません。")
        return
    if not token_ok:
        st.info("SETUP_TOKEN を入力してください。")
        return

    _require_django()
    from django.contrib.auth import get_user_model

    User = get_user_model()
    users = list(User.objects.order_by("id").values("id", "username"))
    if not users:
        st.error("db.sqlite3 にユーザーが見つかりません。正しいDBをアップロードしてください。")
        return

    if len(users) == 1:
        target_user_id = int(users[0]["id"])
        target_username = str(users[0]["username"])
        st.caption(f"ユーザー: {target_username}")
    else:
        opts = [(int(u["id"]), str(u["username"])) for u in users]
        last_uid_raw = _get_cookie_value(LAST_USER_COOKIE_NAME)
        last_uid = None
        try:
            last_uid = int(last_uid_raw) if last_uid_raw is not None else None
        except Exception:
            last_uid = None
        default_index = 0
        if last_uid is not None:
            for i, (uid, _) in enumerate(opts):
                if uid == last_uid:
                    default_index = i
                    break

        selected = st.selectbox(
            "ユーザーを選択",
            options=opts,
            index=default_index,
            format_func=lambda x: x[1],
            key="setup_login_user_select",
        )
        target_user_id = int(selected[0])
        target_username = str(selected[1])

    if st.button("ログイン", type="primary", use_container_width=True, key="setup_login_submit"):
        _set_auth_state(target_user_id, target_username)
        # 再読み込み復元用Cookie（12時間）
        secret = _cookie_secret_bytes()
        if secret:
            try:
                st_ = os.stat(db_path)
                payload = {
                    "user_id": int(target_user_id),
                    "username": str(target_username),
                    "exp": time.time() + AUTH_TTL_SECONDS,
                    "db_fp": {"size": int(st_.st_size), "mtime": int(st_.st_mtime)},
                }
                token = _encode_auth_token(payload, secret)
                _set_cookie_js(AUTH_COOKIE_NAME, token, max_age_seconds=AUTH_TTL_SECONDS)
                _set_cookie_js(
                    LAST_USER_COOKIE_NAME,
                    str(int(target_user_id)),
                    max_age_seconds=LAST_USER_TTL_SECONDS,
                )
            except Exception:
                # Cookie保存に失敗しても、session_stateログインは成立させる
                pass
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

    # 日付 / 使用デッキ / 対面デッキ は縦に並べる
    input_date = st.date_input("日付", value=date.today())

    st.caption("使用デッキ")
    # 使用デッキのみ「前回入力値」を初期値として入れる（初回表示時のみ）
    if "input_used_deck_text" not in st.session_state:
        last_used = (
            Result.objects.filter(user=user)
            .exclude(used_deck="")
            .order_by("-date", "-id")
            .values_list("used_deck", flat=True)
            .first()
        )
        st.session_state["input_used_deck_text"] = (last_used or "")
    c2a, c2b = st.columns([5, 2])
    with c2a:
        used_deck = st.text_input(
            "使用デッキ",
            key="input_used_deck_text",
            placeholder="使用デッキ選択（候補外は直接入力）",
            label_visibility="collapsed",
        )
    with c2b:
        st.multiselect(
            "候補",
            options=[""] + decks,
            default=[],
            max_selections=1,
            format_func=lambda x: x or "（未選択）",
            key="input_used_deck_select_ms",
            placeholder="デッキリスト",
            on_change=_sync_text_from_multiselect,
            kwargs={"select_key": "input_used_deck_select_ms", "text_key": "input_used_deck_text"},
            label_visibility="collapsed",
        )

    st.caption("対面デッキ")
    opp_names = [d.name for d in opp_decks]
    st.session_state.setdefault("input_opp_deck_text", "")
    c3a, c3b = st.columns([5, 2])
    with c3a:
        opp_text = st.text_input(
            "対面デッキ",
            key="input_opp_deck_text",
            placeholder="対面デッキ選択（候補外は直接入力）",
            label_visibility="collapsed",
        )
    with c3b:
        st.multiselect(
            "候補",
            options=[""] + opp_names,
            default=[],
            max_selections=1,
            format_func=lambda x: x or "（未選択）",
            key="input_opp_deck_select_ms",
            placeholder="デッキリスト",
            on_change=_sync_text_from_multiselect,
            kwargs={"select_key": "input_opp_deck_select_ms", "text_key": "input_opp_deck_text"},
            label_visibility="collapsed",
        )

    col4, col5 = st.columns(2)
    with col4:
        # 選択肢は縦並び（項目自体は左右カラムで横並びOK）
        play_order = st.radio("先行/後攻", options=["先行", "後攻"], horizontal=False)
    with col5:
        # 表示は「勝ち/負け/引き分け」にして視認性を優先（保存時に〇/×/両敗へ正規化）
        match_result = st.radio("勝敗", options=["勝ち", "負け", "引き分け"], horizontal=False)

    note = st.text_area("備考", value="", height=120)

    if st.button("保存", type="primary", use_container_width=True):
        from dashbords.models import Deck, OpponentDeck

        used_deck_name = (used_deck or "").strip()
        opponent_deck_name = (opp_text or "").strip()
        note_text = (note or "").strip()

        # --- 入力されたデッキ名がマスタに無い場合は追加（ユーザー範囲） ---
        if used_deck_name:
            deck_obj, created = Deck.objects.get_or_create(
                user=user,
                name=used_deck_name,
                defaults={"is_active": True},
            )
            if (not created) and (not bool(deck_obj.is_active)):
                deck_obj.is_active = True
                deck_obj.save(update_fields=["is_active"])

        opponent_deck_obj = None
        if opponent_deck_name:
            opponent_deck_obj, created = OpponentDeck.objects.get_or_create(
                user=user,
                name=opponent_deck_name,
                defaults={"is_active": True},
            )
            if (not created) and (not bool(opponent_deck_obj.is_active)):
                opponent_deck_obj.is_active = True
                opponent_deck_obj.save(update_fields=["is_active"])
        else:
            # 対面デッキ未入力＝不戦勝扱い
            if "不戦勝" not in note_text:
                note_text = f"{note_text}\n不戦勝".strip() if note_text else "不戦勝"

        Result.objects.create(
            user=user,
            date=input_date,
            used_deck=used_deck_name,
            opponent_deck=opponent_deck_obj,
            play_order=play_order or "",
            match_result=("〇" if not opponent_deck_name else _normalize_match_result(match_result)),
            note=note_text,
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
        mr_values = _match_result_values_for_filter(match_result)
        if mr_values:
            qs = qs.filter(match_result__in=mr_values)
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

    with st.expander("フィルタ / ソート", expanded=False):
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
            used_deck_ms = st.multiselect(
                "使用デッキ",
                options=[""] + used_deck_values,
                default=[],
                max_selections=1,
                format_func=lambda x: x or "（全て）",
                key="filter_used_deck_ms",
                placeholder="デッキリスト",
            )
            used_deck = (used_deck_ms[0] if used_deck_ms else "")
        with c5:
            opponent_deck_ms = st.multiselect(
                "対面デッキ",
                options=opp_options,
                default=[],
                max_selections=1,
                format_func=lambda x: x[1],
                key="filter_opponent_deck_ms",
                placeholder="デッキリスト",
            )
            opponent_deck = (opponent_deck_ms[0][0] if opponent_deck_ms else "")
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

        c10, _, _ = st.columns(3)
        with c10:
            limit = st.selectbox(
                "表示件数",
                options=[10, 20, 50, 100, 200, 500, 2000],
                index=0,  # デフォルト 10件
                format_func=lambda x: f"{x}件",
                key="filter_limit",
            )

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
        "limit": limit,
    }

    limit_n = int(filters.get("limit") or 10)
    results = list(_results_queryset(user, filters)[:limit_n])
    st.caption(f"表示件数: {len(results)}（設定: {limit_n}件）")

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
                "match_result": _normalize_match_result(r.match_result),
                "note": r.note,
            }
        )

    edited = st.data_editor(
        rows,
        hide_index=True,
        use_container_width=True,
        # チェックボックス（selected）だけ操作可能にして、それ以外は編集不可＝静的表示に寄せる
        disabled=["id", "date", "used_deck", "opponent_deck", "play_order", "match_result", "note"],
        num_rows="fixed",
        # 件数が少ない時に余計な空白行（空白領域）が見えないよう、高さを件数に合わせる
        height=min(600, 90 + (len(rows) * 35)),
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
                    ed_opp_ms = st.multiselect(
                        "対面デッキ",
                        options=opp_opts,
                        default=[],
                        max_selections=1,
                        format_func=lambda x: x[1],
                        key="edit_opponent_deck_ms",
                        placeholder="デッキリスト",
                    )
                    ed_opp = (ed_opp_ms[0] if ed_opp_ms else ("", "（未選択）"))
                    ed_play = st.selectbox("先行/後攻", options=["", "先行", "後攻"], index=(1 if target.play_order == "先行" else 2 if target.play_order == "後攻" else 0))
                    normalized_target_result = _normalize_match_result(target.match_result)
                    ed_result = st.selectbox(
                        "勝敗",
                        options=["〇", "×", "両敗"],
                        index=(0 if normalized_target_result == "〇" else 1 if normalized_target_result == "×" else 2),
                    )
                    ed_note = st.text_area("備考", value=target.note, height=120, key="edit_note")

                    if st.button("更新", type="primary", use_container_width=True, key="edit_submit"):
                        opponent_deck_obj = None
                        if ed_opp and ed_opp[0]:
                            opponent_deck_obj = OpponentDeck.objects.filter(user=user, is_active=True, id=ed_opp[0]).first()
                        target.date = ed_date
                        target.used_deck = ed_used_deck or ""
                        target.opponent_deck = opponent_deck_obj
                        target.play_order = ed_play or ""
                        target.match_result = _normalize_match_result(ed_result)
                        target.note = ed_note or ""
                        target.save()
                        st.success("更新しました。")
                        st.rerun()


def _page_analysis(user) -> None:
    from dashbords.models import Deck, Result
    from django.db.models import Count, Q
    import pandas as pd

    st.subheader("分析")

    def _table_no_index(rows: list[dict[str, Any]]) -> None:
        df = pd.DataFrame(rows)
        # st.table は環境によってインデックス（行番号）が消えないことがあるため、
        # index=False の HTML を生成して確実に非表示にする（値はエスケープして安全に表示）
        html = df.to_html(index=False, escape=True)
        st.markdown(f'<div class="scroll-table">{html}</div>', unsafe_allow_html=True)

    # --- フィルター（表示/集計対象を絞る） ---
    with st.expander("フィルター（分析）", expanded=False):
        # 表示順（指定）:
        # 使用デッキ → 対面デッキ（（未入力）可） → 先行/後攻 → 勝敗 → キーワード

        # 候補（使用デッキ）
        used_deck_values_from_master = list(
            Deck.objects.filter(user=user, is_active=True).order_by("name", "id").values_list("name", flat=True)
        )
        used_deck_values_from_results = list(
            Result.objects.filter(user=user).exclude(used_deck="").values_list("used_deck", flat=True).distinct()
        )
        used_values = sorted({*(v.strip() for v in used_deck_values_from_master if v), *(v.strip() for v in used_deck_values_from_results if v)})

        # 候補（対面デッキ）
        opp_values = list(
            Result.objects.filter(user=user)
            .select_related("opponent_deck")
            .exclude(opponent_deck__isnull=True)
            .exclude(opponent_deck__name__isnull=True)
            .exclude(opponent_deck__name="")
            .values_list("opponent_deck__name", flat=True)
            .distinct()
        )
        opp_values = sorted({*(v.strip() for v in opp_values if v is not None)}, key=lambda x: x)

        r1c1, r1c2 = st.columns(2)
        with r1c1:
            a_used_deck_ms = st.multiselect(
                "使用デッキ",
                options=[""] + used_values,
                default=[],
                max_selections=1,
                format_func=lambda x: x or "（全て）",
                key="analysis_used_deck_ms",
                placeholder="デッキリスト",
            )
            a_used_deck = (a_used_deck_ms[0] if a_used_deck_ms else "")
        with r1c2:
            a_opp_deck_ms = st.multiselect(
                "対面デッキ",
                options=["", "__NONE__"] + opp_values,
                default=[],
                max_selections=1,
                format_func=lambda x: "（全て）" if x == "" else ("（未入力）" if x == "__NONE__" else x),
                key="analysis_opp_deck_ms",
                placeholder="デッキリスト",
            )
            a_opp_deck = (a_opp_deck_ms[0] if a_opp_deck_ms else "")

        r2c1, r2c2 = st.columns(2)
        with r2c1:
            a_play_order = st.selectbox(
                "先行/後攻",
                options=["", "先行", "後攻"],
                format_func=lambda x: x or "（全て）",
                key="analysis_play_order",
            )
        with r2c2:
            a_match_result = st.selectbox(
                "勝敗",
                options=["", "〇", "×", "両敗"],
                format_func=lambda x: x or "（全て）",
                key="analysis_match_result",
            )

        a_q = st.text_input("キーワード（備考/デッキ名）", value="", key="analysis_q")

    qs = Result.objects.filter(user=user).select_related("opponent_deck")
    if (a_used_deck or "").strip():
        qs = qs.filter(used_deck=(a_used_deck or "").strip())
    if a_opp_deck == "__NONE__":
        qs = qs.filter(Q(opponent_deck__isnull=True) | Q(opponent_deck__name__isnull=True) | Q(opponent_deck__name=""))
    elif (a_opp_deck or "").strip():
        qs = qs.filter(opponent_deck__name=(a_opp_deck or "").strip())
    if (a_play_order or "").strip():
        qs = qs.filter(play_order=(a_play_order or "").strip())
    if (a_match_result or "").strip():
        mr_values = _match_result_values_for_filter(a_match_result)
        if mr_values:
            qs = qs.filter(match_result__in=mr_values)
    if (a_q or "").strip():
        q = (a_q or "").strip()
        qs = qs.filter(Q(note__icontains=q) | Q(used_deck__icontains=q) | Q(opponent_deck__name__icontains=q))

    total_matches = qs.count()
    overall_win = qs.filter(match_result__in=["〇", "勝ち"]).count()
    overall_loss = qs.filter(match_result__in=["×", "負け"]).count()
    overall_other = total_matches - overall_win - overall_loss
    overall_decided = overall_win + overall_loss
    overall_win_rate = ((overall_win / overall_decided) * 100.0) if overall_decided else None

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("総対戦数", total_matches)
    c2.metric("勝ち", overall_win)
    c3.metric("負け", overall_loss)
    c4.metric("勝率（%）", "-" if overall_win_rate is None else f"{overall_win_rate:.1f}%")

    st.divider()
    st.markdown("#### 先行/後攻別")
    play_order_summary = []
    for label in ["先行", "後攻"]:
        po_total = qs.filter(play_order=label).count()
        po_win = qs.filter(play_order=label, match_result__in=["〇", "勝ち"]).count()
        po_loss = qs.filter(play_order=label, match_result__in=["×", "負け"]).count()
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
    _table_no_index(play_order_summary)

    st.divider()
    st.markdown("#### 使用デッキごとの集計")
    per_deck_rows = (
        qs.values("used_deck")
        .annotate(
            total=Count("id"),
            win=Count("id", filter=Q(match_result__in=["〇", "勝ち"])),
            loss=Count("id", filter=Q(match_result__in=["×", "負け"])),
        )
        .order_by("used_deck")
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
    per_deck.sort(key=lambda x: _sort_key_deck_label(str(x.get("used_deck") or "")))
    _table_no_index(per_deck)

    st.divider()
    st.markdown("#### (使用デッキ × 対面デッキ) の集計")
    matchup_rows = (
        # opponent_deck が不明（未設定）のデータは「表示しない」
        qs.filter(opponent_deck__isnull=False)
        .exclude(opponent_deck__name__isnull=True)
        .exclude(opponent_deck__name="")
        .values("used_deck", "opponent_deck__name")
        .annotate(
            total=Count("id"),
            win=Count("id", filter=Q(match_result__in=["〇", "勝ち"])),
            loss=Count("id", filter=Q(match_result__in=["×", "負け"])),
        )
        .order_by("used_deck", "opponent_deck__name")
    )
    matchups = []
    for r in matchup_rows:
        used_deck = (r.get("used_deck") or "").strip() or "（未入力）"
        opponent_deck = (r.get("opponent_deck__name") or "").strip()
        if not opponent_deck:
            continue
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
    matchups.sort(
        key=lambda x: (
            _sort_key_deck_label(str(x.get("used_deck") or "")),
            _sort_key_deck_label(str(x.get("opponent_deck") or "")),
        )
    )
    _table_no_index(matchups)


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
    st.set_page_config(page_title="Data Aggregation (Streamlit)", layout="wide", initial_sidebar_state="expanded")
    _inject_global_css()

    # 再読み込み時も、最終ログインから12時間以内ならログイン状態を復元する
    _restore_auth_from_cookie_if_possible()

    auth = _get_auth_state()
    with st.sidebar:
        st.markdown("### サイドバー")
        if auth:
            st.success(f"ログイン中: {auth.username}")
            if st.button("ログアウト", use_container_width=True):
                _logout()
                st.rerun()
        else:
            st.info("未ログイン")

        # 遷移先はサイドバーに表示（項目名をクリックすると遷移）
        if auth:
            page = st.radio(
                "ページ",
                options=["入力", "結果一覧", "分析", "設定", "バックアップ/復元"],
                key="page_nav",
            )
        else:
            page = "ログイン"


    if page == "ログイン":
        st.title("試合結果集計ツール")
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
    if page == "設定":
        _page_master(user)
        return
    if page == "バックアップ/復元":
        _page_backup_restore(user)
        return


if __name__ == "__main__":
    main()


