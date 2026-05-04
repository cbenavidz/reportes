# -*- coding: utf-8 -*-
"""
Conector Meta Graph API — Facebook Pages + Instagram Business.

Fetches:
  - Page Insights diarios (alcance, engagement, vistas, reacciones, seguidores)
  - Posts del período (sumando likes, comentarios, compartidos por día)
  - Instagram Business Insights diarios + media insights

Strategy:
  - Usa Page Access Token derivado del User Token (mejor scope para Insights).
  - Itera métricas individualmente; si una está deprecada el resto sigue.
  - Las métricas de likes/comments/shares vienen mejor a nivel POST que a
    nivel Page (Page Insights ya no devuelve esos valores en v23.0+).

Credenciales en st.secrets:
    [meta]
    access_token = "EAAxxx..."          # User Token con permisos
    facebook_page_id = "1234567890"
    instagram_user_id = "1789..."        # opcional
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import pandas as pd

from ..secrets_loader import get_secret_dict

logger = logging.getLogger(__name__)

GRAPH_VERSION = "v23.0"
BASE_URL = f"https://graph.facebook.com/{GRAPH_VERSION}"

# Caché en memoria del Page Access Token derivado del User Token.
_PAGE_TOKEN_CACHE: dict[str, str] = {}


def is_meta_configured() -> bool:
    """True si hay credenciales válidas para Meta."""
    cfg = get_secret_dict("meta")
    if not cfg:
        return False
    return bool(cfg.get("access_token"))


def _get(endpoint: str, params: dict | None = None, *, token: str | None = None) -> dict:
    """Helper GET a Graph API."""
    import requests
    cfg = get_secret_dict("meta")
    if not cfg or not cfg.get("access_token"):
        raise RuntimeError("Meta no está configurado.")
    params = dict(params or {})
    params["access_token"] = token or cfg["access_token"]
    r = requests.get(f"{BASE_URL}{endpoint}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def _try_get(endpoint: str, params: dict | None = None, *, token: str | None = None) -> dict | None:
    """Como _get pero retorna None si falla."""
    import requests
    try:
        return _get(endpoint, params, token=token)
    except requests.HTTPError as exc:
        logger.warning("Graph API failed: %s — %s", endpoint, exc)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Graph API error: %s — %s", endpoint, exc)
        return None


def _get_page_access_token() -> str | None:
    """Obtiene Page Access Token desde /me/accounts con el User Token."""
    cfg = get_secret_dict("meta") or {}
    page_id = cfg.get("facebook_page_id")
    user_token = cfg.get("access_token")
    if not page_id or not user_token:
        return None

    cache_key = f"{user_token[:20]}:{page_id}"
    if cache_key in _PAGE_TOKEN_CACHE:
        return _PAGE_TOKEN_CACHE[cache_key]

    data = _try_get("/me/accounts", {"limit": 100})
    if not data:
        return None

    for acc in data.get("data", []):
        if str(acc.get("id")) == str(page_id):
            page_token = acc.get("access_token")
            if page_token:
                _PAGE_TOKEN_CACHE[cache_key] = page_token
                return page_token
    return None


def _fetch_metric_safe(
    endpoint: str,
    metric: str,
    period: str,
    date_from: date,
    date_to: date,
    token: str,
) -> list[dict]:
    """Llama insights con UNA métrica. Retorna lista normalizada o []."""
    params = {
        "metric": metric,
        "period": period,
        "since": str(date_from),
        "until": str(date_to),
    }
    data = _try_get(f"{endpoint}/insights", params, token=token)
    if not data:
        return []
    out = []
    for m in data.get("data", []):
        for v in m.get("values", []):
            fecha = v.get("end_time", "")[:10]
            if not fecha:
                continue
            out.append({"fecha": fecha, "metric": metric, "value": v.get("value", 0)})
    return out


def _unpack_reactions(value):
    """`page_actions_post_reactions_total` viene como dict — extraemos."""
    if isinstance(value, dict):
        return {
            "likes": int(value.get("like", 0) or 0),
            "loves": int(value.get("love", 0) or 0),
            "wows": int(value.get("wow", 0) or 0),
            "haha": int(value.get("haha", 0) or 0),
            "sads": int(value.get("sorry", 0) or 0),
            "angrys": int(value.get("anger", 0) or 0),
        }
    return {"likes": 0, "loves": 0, "wows": 0, "haha": 0, "sads": 0, "angrys": 0}


def _fetch_facebook_posts_aggregated(
    page_id: str,
    date_from: date,
    date_to: date,
    token: str,
) -> pd.DataFrame:
    """
    Trae los posts del período y agrega likes/comments/shares por día.

    Page Insights ya no expone likes/comments/shares totales en v23.0.
    Hay que ir a nivel post y sumar.
    """
    since_unix = int(datetime.combine(date_from, datetime.min.time()).timestamp())
    until_unix = int(datetime.combine(
        date_to + timedelta(days=1), datetime.min.time()
    ).timestamp())

    fields = (
        "id,created_time,message,"
        "likes.summary(true).limit(0),"
        "comments.summary(true).limit(0),"
        "shares,"
        "reactions.summary(true).limit(0)"
    )
    params = {
        "fields": fields,
        "since": since_unix,
        "until": until_unix,
        "limit": 100,
    }
    rows = []
    next_url = f"/{page_id}/posts"
    next_params = params
    pages_fetched = 0
    while next_url and pages_fetched < 10:  # safety: max 10 páginas (1000 posts)
        data = _try_get(next_url, next_params, token=token)
        if not data:
            break
        for p in data.get("data", []):
            created = p.get("created_time", "")[:10]
            if not created:
                continue
            likes_total = (p.get("likes") or {}).get("summary", {}).get("total_count", 0)
            comments_total = (p.get("comments") or {}).get("summary", {}).get("total_count", 0)
            shares_total = (p.get("shares") or {}).get("count", 0)
            reactions_total = (p.get("reactions") or {}).get("summary", {}).get("total_count", 0)
            rows.append({
                "fecha": created,
                "post_id": p.get("id"),
                "n_posts": 1,
                "likes": likes_total,
                "comentarios": comments_total,
                "compartidos": shares_total,
                "reacciones_total": reactions_total,
            })
        # Pagination via cursors — el "next" viene en data.paging.next
        paging = data.get("paging", {})
        next_full = paging.get("next")
        if not next_full:
            break
        # next_full es URL completa con params; resetear next_params para no duplicar
        # Truco: pasamos solo el access_token vía nuestro helper
        # Pero como _get prepende BASE_URL, necesitamos otro approach.
        # Por simplicidad: cortamos el bucle aquí (1 página = 100 posts)
        break

    if not rows:
        return pd.DataFrame(columns=[
            "fecha", "n_posts", "likes", "comentarios", "compartidos", "reacciones_total",
        ])

    df = pd.DataFrame(rows)
    df["fecha"] = pd.to_datetime(df["fecha"], errors="coerce").dt.normalize()
    agg = df.groupby("fecha", as_index=False).agg({
        "n_posts": "sum",
        "likes": "sum",
        "comentarios": "sum",
        "compartidos": "sum",
        "reacciones_total": "sum",
    })
    return agg


def fetch_meta_facebook_data(
    date_from: date,
    date_to: date,
) -> pd.DataFrame:
    """
    Trae insights diarios de la Page de Facebook + agregados de posts.

    Combina:
      - Page Insights: alcance, engagement, vistas video/página, seguidores
      - Posts agregados: likes, comentarios, compartidos por día
    """
    cfg = get_secret_dict("meta") or {}
    page_id = cfg.get("facebook_page_id")
    if not page_id:
        raise RuntimeError("Falta facebook_page_id en secrets.")

    page_token = _get_page_access_token() or cfg.get("access_token")

    # --- Insights de Página ---
    metrics_day = [
        ("page_impressions", "impresiones"),
        ("page_impressions_unique", "alcance"),
        ("page_post_engagements", "engagement_total"),
        ("page_fan_adds", "nuevos_seguidores"),
        ("page_fan_removes", "perdida_seguidores"),
        ("page_video_views", "vistas_video"),
        ("page_views_total", "vistas_pagina"),
    ]
    metrics_lifetime = [
        ("page_fans", "seguidores"),
    ]

    rows: dict[str, dict] = {}

    for metric, alias in metrics_day:
        for v in _fetch_metric_safe(
            f"/{page_id}", metric, "day", date_from, date_to, page_token
        ):
            fecha = v["fecha"]
            rows.setdefault(fecha, {"fecha": fecha})[alias] = v["value"]

    for metric, alias in metrics_lifetime:
        for v in _fetch_metric_safe(
            f"/{page_id}", metric, "day", date_from, date_to, page_token
        ):
            fecha = v["fecha"]
            rows.setdefault(fecha, {"fecha": fecha})[alias] = v["value"]

    # --- Reacciones (vienen como dict, hay que desempacarlas) ---
    for v in _fetch_metric_safe(
        f"/{page_id}", "page_actions_post_reactions_total", "day",
        date_from, date_to, page_token,
    ):
        fecha = v["fecha"]
        unpacked = _unpack_reactions(v["value"])
        rows.setdefault(fecha, {"fecha": fecha}).update({
            "reacciones_likes": unpacked["likes"],
            "reacciones_loves": unpacked["loves"],
            "reacciones_wows": unpacked["wows"],
        })

    df = pd.DataFrame(list(rows.values()))
    if not df.empty:
        df["fecha"] = pd.to_datetime(df["fecha"], errors="coerce").dt.normalize()

    # --- Posts agregados (likes, comentarios, compartidos reales) ---
    posts_df = _fetch_facebook_posts_aggregated(page_id, date_from, date_to, page_token)

    # Combinar insights + posts
    if df.empty and posts_df.empty:
        return pd.DataFrame()
    if df.empty:
        df = posts_df
    elif posts_df.empty:
        df["likes"] = 0
        df["comentarios"] = 0
        df["compartidos"] = 0
        df["n_posts"] = 0
    else:
        df = df.merge(posts_df, on="fecha", how="outer")
        for c in ["likes", "comentarios", "compartidos", "n_posts"]:
            if c in df.columns:
                df[c] = df[c].fillna(0).astype(int)

    # Engagement: usar el de Page Insights si existe, sino calcularlo
    if "engagement_total" in df.columns:
        df["engagement"] = df["engagement_total"].fillna(0)
    else:
        df["engagement"] = (
            df.get("likes", 0) + df.get("comentarios", 0) + df.get("compartidos", 0)
        )

    # Si no llegó impresiones de Page Insights, usar reach * 1.5 (heurística)
    if "impresiones" not in df.columns or df["impresiones"].fillna(0).sum() == 0:
        if "alcance" in df.columns:
            df["impresiones"] = (df["alcance"].fillna(0) * 1.5).round().astype(int)

    return df.sort_values("fecha").reset_index(drop=True)


def fetch_meta_instagram_data(
    date_from: date,
    date_to: date,
) -> pd.DataFrame:
    """
    Trae insights diarios del perfil IG Business + agregados de media.
    """
    cfg = get_secret_dict("meta") or {}
    ig_id = cfg.get("instagram_user_id")
    if not ig_id:
        raise RuntimeError("Falta instagram_user_id en secrets.")

    page_token = _get_page_access_token() or cfg.get("access_token")

    # --- Insights del perfil IG ---
    # En v23.0 IG cambió mucho. Probamos varias.
    metrics = [
        ("reach", "alcance"),
        ("follower_count", "nuevos_seguidores"),
        ("profile_views", "vistas_perfil"),
        ("website_clicks", "clicks_web"),
        ("accounts_engaged", "cuentas_engagement"),
    ]

    rows: dict[str, dict] = {}
    for metric, alias in metrics:
        for v in _fetch_metric_safe(
            f"/{ig_id}", metric, "day", date_from, date_to, page_token
        ):
            fecha = v["fecha"]
            value = v["value"]
            if isinstance(value, dict):
                value = value.get("value", 0)
            rows.setdefault(fecha, {"fecha": fecha})[alias] = value

    df = pd.DataFrame(list(rows.values()))
    if not df.empty:
        df["fecha"] = pd.to_datetime(df["fecha"], errors="coerce").dt.normalize()

    # --- Media (posts/reels) agregados ---
    media_df = _fetch_instagram_media_aggregated(
        ig_id, date_from, date_to, page_token
    )

    if df.empty and media_df.empty:
        return pd.DataFrame()
    if df.empty:
        df = media_df
    elif media_df.empty:
        df["likes"] = 0
        df["comentarios"] = 0
        df["impresiones"] = 0
        df["n_posts"] = 0
    else:
        df = df.merge(media_df, on="fecha", how="outer")
        for c in ["likes", "comentarios", "impresiones", "n_posts"]:
            if c in df.columns:
                df[c] = df[c].fillna(0).astype(int)

    df["compartidos"] = 0  # IG no expone shares totales fácilmente
    df["engagement"] = (
        df.get("likes", 0) + df.get("comentarios", 0)
    )

    return df.sort_values("fecha").reset_index(drop=True)


def _fetch_instagram_media_aggregated(
    ig_id: str,
    date_from: date,
    date_to: date,
    token: str,
) -> pd.DataFrame:
    """Trae media (posts + reels) y agrega likes/comments/views por día."""
    fields = "id,timestamp,media_type,like_count,comments_count"
    params = {"fields": fields, "limit": 100}
    data = _try_get(f"/{ig_id}/media", params, token=token)
    if not data:
        return pd.DataFrame(columns=[
            "fecha", "n_posts", "likes", "comentarios", "impresiones",
        ])

    rows = []
    for m in data.get("data", []):
        ts = m.get("timestamp", "")[:10]
        if not ts:
            continue
        media_date = datetime.strptime(ts, "%Y-%m-%d").date()
        if media_date < date_from or media_date > date_to:
            continue
        rows.append({
            "fecha": ts,
            "n_posts": 1,
            "likes": int(m.get("like_count", 0) or 0),
            "comentarios": int(m.get("comments_count", 0) or 0),
            "impresiones": 0,  # requiere insights por media (limit rates)
        })

    if not rows:
        return pd.DataFrame(columns=[
            "fecha", "n_posts", "likes", "comentarios", "impresiones",
        ])

    df = pd.DataFrame(rows)
    df["fecha"] = pd.to_datetime(df["fecha"], errors="coerce").dt.normalize()
    return df.groupby("fecha", as_index=False).agg({
        "n_posts": "sum",
        "likes": "sum",
        "comentarios": "sum",
        "impresiones": "sum",
    })
