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


def _fetch_metric_total_value(
    endpoint: str,
    metric: str,
    date_from: date,
    date_to: date,
    token: str,
) -> int | None:
    """
    Llama insights de IG (v22+) con metric_type=total_value.
    Devuelve UN solo número (total del período) o None si falla.

    Necesario para IG: en v25.0 las métricas de cuenta IG ya no
    soportan time-series y devuelven total_value: {value: N}.
    """
    params = {
        "metric": metric,
        "period": "day",
        "since": str(date_from),
        "until": str(date_to),
        "metric_type": "total_value",
    }
    data = _try_get(f"{endpoint}/insights", params, token=token)
    if not data:
        return None
    for m in data.get("data", []):
        tv = m.get("total_value", {})
        if isinstance(tv, dict) and "value" in tv:
            return int(tv["value"] or 0)
    return None


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


def _fetch_facebook_posts(
    page_id: str,
    date_from: date,
    date_to: date,
    token: str,
) -> pd.DataFrame:
    """
    Trae los posts del período con todas sus métricas de engagement.
    Devuelve DataFrame con UN POST POR FILA — se usa para agregados,
    top posts, y métricas de engagement.
    """
    since_unix = int(datetime.combine(date_from, datetime.min.time()).timestamp())
    until_unix = int(datetime.combine(
        date_to + timedelta(days=1), datetime.min.time()
    ).timestamp())

    # IMPORTANTE: usar /published_posts (no /posts) para mejor cobertura.
    # comments.summary(total_count): pages_read_engagement bastá.
    fields = (
        "id,created_time,message,permalink_url,"
        "likes.summary(total_count).limit(0),"
        "comments.summary(total_count).limit(0),"
        "shares,"
        "reactions.summary(total_count).limit(0)"
    )
    params = {
        "fields": fields,
        "since": since_unix,
        "until": until_unix,
        "limit": 100,
    }
    rows = []
    # Intentar primero /published_posts (incluye historic), fallback a /posts
    data = _try_get(f"/{page_id}/published_posts", params, token=token)
    if not data:
        data = _try_get(f"/{page_id}/posts", params, token=token)
    if not data:
        return pd.DataFrame(columns=[
            "fecha", "post_id", "mensaje", "url",
            "likes", "comentarios", "compartidos", "reacciones_total",
        ])
    for p in data.get("data", []):
        created = p.get("created_time", "")[:10]
        if not created:
            continue
        # Intentar varias formas de obtener el count (Meta cambió shapes)
        likes_obj = p.get("likes") or {}
        comments_obj = p.get("comments") or {}
        reactions_obj = p.get("reactions") or {}
        likes_total = (
            likes_obj.get("summary", {}).get("total_count")
            or likes_obj.get("count")
            or len(likes_obj.get("data", []))
            or 0
        )
        comments_total = (
            comments_obj.get("summary", {}).get("total_count")
            or comments_obj.get("count")
            or len(comments_obj.get("data", []))
            or 0
        )
        shares_total = (p.get("shares") or {}).get("count", 0)
        reactions_total = (
            reactions_obj.get("summary", {}).get("total_count")
            or reactions_obj.get("count")
            or 0
        )
        rows.append({
            "fecha": created,
            "post_id": p.get("id"),
            "mensaje": (p.get("message") or "")[:200],
            "url": p.get("permalink_url", ""),
            "likes": likes_total,
            "comentarios": comments_total,
            "compartidos": shares_total,
            "reacciones_total": reactions_total,
        })
    if not rows:
        return pd.DataFrame(columns=[
            "fecha", "post_id", "mensaje", "url",
            "likes", "comentarios", "compartidos", "reacciones_total",
        ])
    df = pd.DataFrame(rows)
    df["fecha"] = pd.to_datetime(df["fecha"], errors="coerce")
    df["engagement"] = df["likes"] + df["comentarios"] + df["compartidos"]
    return df.sort_values("engagement", ascending=False).reset_index(drop=True)


def _fetch_facebook_posts_aggregated(
    page_id: str,
    date_from: date,
    date_to: date,
    token: str,
) -> pd.DataFrame:
    """Posts agregados por día — lo que la página principal usa."""
    posts = _fetch_facebook_posts(page_id, date_from, date_to, token)
    if posts.empty:
        return pd.DataFrame(columns=[
            "fecha", "n_posts", "likes", "comentarios", "compartidos", "reacciones_total",
        ])
    posts2 = posts.copy()
    posts2["fecha"] = pd.to_datetime(posts2["fecha"]).dt.normalize()
    posts2["n_posts"] = 1
    return posts2.groupby("fecha", as_index=False).agg({
        "n_posts": "sum",
        "likes": "sum",
        "comentarios": "sum",
        "compartidos": "sum",
        "reacciones_total": "sum",
    })


def fetch_meta_facebook_top_posts(
    date_from: date,
    date_to: date,
    n: int = 10,
) -> pd.DataFrame:
    """Top N posts del período por engagement (likes + comentarios + compartidos)."""
    cfg = get_secret_dict("meta") or {}
    page_id = cfg.get("facebook_page_id")
    if not page_id:
        raise RuntimeError("Falta facebook_page_id en secrets.")
    page_token = _get_page_access_token() or cfg.get("access_token")
    posts = _fetch_facebook_posts(page_id, date_from, date_to, page_token)
    return posts.head(n)


def fetch_meta_facebook_monthly_evolution(
    months: int = 12,
) -> pd.DataFrame:
    """
    Trae evolución MENSUAL de KPIs de Facebook para los últimos N meses.

    Estrategia:
      - Itera de 90 días en 90 días hacia atrás (límite Meta API).
      - Concatena resultados.
      - Agrupa por mes y devuelve DataFrame con columnas:
        mes, seguidores_fin_mes, nuevos_seguidores, engagement,
        alcance, n_posts.
    """
    cfg = get_secret_dict("meta") or {}
    page_id = cfg.get("facebook_page_id")
    if not page_id:
        raise RuntimeError("Falta facebook_page_id en secrets.")
    page_token = _get_page_access_token() or cfg.get("access_token")

    today = date.today()
    months_back = months
    all_rows = []

    # Snapshot actual de seguidores (para reconstruir histórico)
    page_data = _try_get(
        f"/{page_id}", {"fields": "fan_count,followers_count"},
        token=page_token,
    )
    seguidores_actuales = 0
    if page_data:
        seguidores_actuales = int(
            page_data.get("followers_count", 0) or page_data.get("fan_count", 0) or 0
        )

    # Itera en chunks de 90 días hacia atrás
    chunk_end = today
    chunk_start = chunk_end - timedelta(days=90)
    months_covered = 0
    while months_covered < months_back:
        # Insights diarios de este chunk (en v25.0+ page_fans NO existe)
        for metric, alias in [
            ("page_fan_adds", "nuevos_seguidores"),
            ("page_daily_follows", "follows_diarios"),
            ("page_post_engagements", "engagement"),
            ("page_impressions_unique", "alcance"),
        ]:
            for v in _fetch_metric_safe(
                f"/{page_id}", metric, "day", chunk_start, chunk_end, page_token
            ):
                all_rows.append({
                    "fecha": v["fecha"],
                    "metric": alias,
                    "value": v["value"] if not isinstance(v["value"], dict) else 0,
                })
        # Posts del chunk
        try:
            posts = _fetch_facebook_posts(
                page_id, chunk_start, chunk_end, page_token
            )
            for _, p in posts.iterrows():
                all_rows.append({
                    "fecha": p["fecha"].strftime("%Y-%m-%d") if hasattr(p["fecha"], "strftime") else str(p["fecha"])[:10],
                    "metric": "n_posts",
                    "value": 1,
                })
        except Exception:  # noqa: BLE001
            pass

        months_covered += 3  # ~3 meses por chunk
        chunk_end = chunk_start - timedelta(days=1)
        chunk_start = chunk_end - timedelta(days=90)

    if not all_rows:
        return pd.DataFrame(columns=[
            "mes", "seguidores_fin_mes", "nuevos_seguidores",
            "engagement", "alcance", "n_posts",
        ])

    df = pd.DataFrame(all_rows)
    df["fecha"] = pd.to_datetime(df["fecha"], errors="coerce")
    df = df.dropna(subset=["fecha"])
    df["mes"] = df["fecha"].dt.to_period("M").dt.to_timestamp()

    # Pivote: una fila por mes
    df["value"] = pd.to_numeric(df["value"], errors="coerce").fillna(0)

    monthly = df.groupby(["mes", "metric"], as_index=False)["value"].agg(
        # Para 'seguidores' nos interesa el ÚLTIMO valor del mes (snapshot)
        # Para los demás, el SUMATORIO
        # Hack: distinguimos por metric en post-process
        "sum"
    )

    # Pivot to wide format
    wide = monthly.pivot(index="mes", columns="metric", values="value").reset_index()

    # Reconstruir seguidores_fin_mes: actuales - sum(follows_diarios) en meses futuros
    # (Como page_fans no existe en v25.0, usamos el snapshot actual + delta)
    if seguidores_actuales > 0 and "follows_diarios" in wide.columns:
        wide = wide.sort_values("mes", ascending=False).reset_index(drop=True)
        wide["follows_diarios"] = wide["follows_diarios"].fillna(0)
        # seguidores al fin de cada mes = actuales - follows posteriores
        wide["seguidores_fin_mes"] = (
            seguidores_actuales - wide["follows_diarios"].cumsum().shift(1).fillna(0)
        )
        wide = wide.sort_values("mes").reset_index(drop=True)
    elif seguidores_actuales > 0:
        wide["seguidores_fin_mes"] = seguidores_actuales

    # Asegurar columnas estándar
    for col in ["seguidores_fin_mes", "nuevos_seguidores", "engagement",
                "alcance", "n_posts"]:
        if col not in wide.columns:
            wide[col] = 0
        wide[col] = wide[col].fillna(0)

    # Solo últimos N meses
    wide = wide.sort_values("mes").tail(months_back).reset_index(drop=True)
    return wide[[
        "mes", "seguidores_fin_mes", "nuevos_seguidores",
        "engagement", "alcance", "n_posts",
    ]]


def fetch_meta_instagram_monthly_evolution(
    months: int = 12,
) -> pd.DataFrame:
    """
    Evolución mensual de IG basada en posts (/media endpoint).

    NOTA v25.0: las métricas account-level de IG ya no soportan time
    series, solo total_value por período. Por eso esta función se basa
    en posts reales (que sí tienen timestamps).
    """
    cfg = get_secret_dict("meta") or {}
    ig_id = cfg.get("instagram_user_id")
    if not ig_id:
        raise RuntimeError("Falta instagram_user_id en secrets.")
    page_token = _get_page_access_token() or cfg.get("access_token")

    # Snapshot actual de seguidores
    ig_data = _try_get(
        f"/{ig_id}", {"fields": "followers_count,media_count"},
        token=page_token,
    )
    seguidores_actuales = 0
    if ig_data:
        seguidores_actuales = int(ig_data.get("followers_count", 0) or 0)

    # Pull all media (paginated) — limitado a las últimas 100 publicaciones
    fields = "id,timestamp,like_count,comments_count,media_type"
    params = {"fields": fields, "limit": 100}
    data = _try_get(f"/{ig_id}/media", params, token=page_token)
    rows = []
    if data:
        cutoff = date.today() - timedelta(days=months * 31)
        for m in data.get("data", []):
            ts = m.get("timestamp", "")[:10]
            if not ts:
                continue
            try:
                media_date = datetime.strptime(ts, "%Y-%m-%d").date()
            except ValueError:
                continue
            if media_date < cutoff:
                continue
            rows.append({
                "fecha": ts,
                "n_posts": 1,
                "likes": int(m.get("like_count", 0) or 0),
                "comentarios": int(m.get("comments_count", 0) or 0),
            })

    if not rows:
        return pd.DataFrame(columns=[
            "mes", "seguidores_fin_mes", "n_posts", "likes", "comentarios",
            "engagement",
        ])

    df = pd.DataFrame(rows)
    df["fecha"] = pd.to_datetime(df["fecha"])
    df["mes"] = df["fecha"].dt.to_period("M").dt.to_timestamp()
    monthly = df.groupby("mes", as_index=False).agg({
        "n_posts": "sum",
        "likes": "sum",
        "comentarios": "sum",
    })
    monthly["engagement"] = monthly["likes"] + monthly["comentarios"]
    monthly["seguidores_fin_mes"] = seguidores_actuales  # Solo tenemos snapshot
    return monthly.sort_values("mes").tail(months).reset_index(drop=True)


def fetch_meta_instagram_top_posts(
    date_from: date,
    date_to: date,
    n: int = 10,
) -> pd.DataFrame:
    """Top N posts/reels de IG por engagement."""
    cfg = get_secret_dict("meta") or {}
    ig_id = cfg.get("instagram_user_id")
    if not ig_id:
        raise RuntimeError("Falta instagram_user_id en secrets.")
    page_token = _get_page_access_token() or cfg.get("access_token")
    fields = "id,timestamp,media_type,media_url,permalink,caption,like_count,comments_count"
    params = {"fields": fields, "limit": 100}
    data = _try_get(f"/{ig_id}/media", params, token=page_token)
    if not data:
        return pd.DataFrame()
    rows = []
    for m in data.get("data", []):
        ts = m.get("timestamp", "")[:10]
        if not ts:
            continue
        media_date = datetime.strptime(ts, "%Y-%m-%d").date()
        if media_date < date_from or media_date > date_to:
            continue
        likes = int(m.get("like_count", 0) or 0)
        comments = int(m.get("comments_count", 0) or 0)
        rows.append({
            "fecha": ts,
            "post_id": m.get("id"),
            "tipo": m.get("media_type", ""),
            "caption": (m.get("caption") or "")[:200],
            "url": m.get("permalink", ""),
            "likes": likes,
            "comentarios": comments,
            "engagement": likes + comments,
        })
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["fecha"] = pd.to_datetime(df["fecha"])
    return df.sort_values("engagement", ascending=False).head(n).reset_index(drop=True)


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

    # --- Insights de Página (v25.0+) ---
    # NOTA: page_fans y page_fan_removes están deprecadas en v25.0.
    # page_daily_follows reemplaza a page_fans para histórico diario.
    metrics_day = [
        ("page_impressions", "impresiones"),
        ("page_impressions_unique", "alcance"),
        ("page_post_engagements", "engagement_total"),
        ("page_fan_adds", "nuevos_seguidores"),
        ("page_daily_follows", "follows_diarios"),
        ("page_video_views", "vistas_video"),
        ("page_views_total", "vistas_pagina"),
    ]

    rows: dict[str, dict] = {}

    for metric, alias in metrics_day:
        for v in _fetch_metric_safe(
            f"/{page_id}", metric, "day", date_from, date_to, page_token
        ):
            fecha = v["fecha"]
            rows.setdefault(fecha, {"fecha": fecha})[alias] = v["value"]

    # --- Seguidores totales (snapshot via field directo) ---
    # `fan_count` y `followers_count` funcionan como fields del Page
    # incluso cuando el insights metric `page_fans` está deprecado.
    page_data = _try_get(
        f"/{page_id}", {"fields": "fan_count,followers_count"},
        token=page_token,
    )
    seguidores_actuales = 0
    if page_data:
        seguidores_actuales = int(
            page_data.get("followers_count", 0) or page_data.get("fan_count", 0) or 0
        )

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

    # --- Reconstruir seguidores históricos: actuales - sum(daily_follows futuros) ---
    # Esto da el conteo aproximado de seguidores cada día.
    if seguidores_actuales > 0 and not df.empty and "follows_diarios" in df.columns:
        df = df.sort_values("fecha", ascending=False).reset_index(drop=True)
        df["follows_diarios"] = df["follows_diarios"].fillna(0)
        # Cumulative: para cada día, seguidores = actuales - follows desde mañana hasta hoy
        # (simplificación: actuales menos follows posteriores a la fecha)
        df["seguidores"] = seguidores_actuales - df["follows_diarios"].cumsum().shift(1).fillna(0)
        df = df.sort_values("fecha").reset_index(drop=True)
    elif seguidores_actuales > 0 and not df.empty:
        # Fallback: poner el conteo actual en todas las filas
        df["seguidores"] = seguidores_actuales

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

    df = df.sort_values("fecha").reset_index(drop=True)

    # Forzar columna 'seguidores' a estar siempre presente (snapshot actual).
    # El merge con posts_df puede haber dejado NaN en esta columna.
    if seguidores_actuales > 0:
        if "seguidores" not in df.columns:
            df["seguidores"] = seguidores_actuales
        else:
            df["seguidores"] = df["seguidores"].fillna(seguidores_actuales)

    # Engagement: usar el de Page Insights si existe, sino calcularlo
    if "engagement_total" in df.columns:
        df["engagement"] = df["engagement_total"].fillna(0)
    else:
        df["engagement"] = (
            df.get("likes", 0) + df.get("comentarios", 0) + df.get("compartidos", 0)
        )

    # Si engagement es 0 pero hay likes/comentarios/compartidos, recalcular
    if df["engagement"].sum() == 0:
        df["engagement"] = (
            df.get("likes", 0).fillna(0)
            + df.get("comentarios", 0).fillna(0)
            + df.get("compartidos", 0).fillna(0)
        )

    # Si no llegó impresiones de Page Insights, usar reach * 1.5 (heurística)
    if "impresiones" not in df.columns or df["impresiones"].fillna(0).sum() == 0:
        if "alcance" in df.columns:
            df["impresiones"] = (df["alcance"].fillna(0) * 1.5).round().astype(int)

    return df.reset_index(drop=True)


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
    # IMPORTANTE: en v25.0 IG insights devuelven UN SOLO total_value
    # por período (no time series). Hay que usar metric_type=total_value.
    period_totals: dict[str, int] = {}
    for metric, alias in [
        ("reach", "alcance_periodo"),
        ("accounts_engaged", "cuentas_engagement_periodo"),
        ("profile_views", "vistas_perfil_periodo"),
        ("website_clicks", "clicks_web_periodo"),
        ("follower_count", "nuevos_seguidores_periodo"),
    ]:
        val = _fetch_metric_total_value(
            f"/{ig_id}", metric, date_from, date_to, page_token
        )
        if val is not None:
            period_totals[alias] = val

    # Snapshot actual de seguidores IG
    ig_data = _try_get(
        f"/{ig_id}", {"fields": "followers_count,media_count"},
        token=page_token,
    )
    if ig_data:
        period_totals["seguidores_actual"] = int(
            ig_data.get("followers_count", 0) or 0
        )
        period_totals["total_media"] = int(ig_data.get("media_count", 0) or 0)

    # --- Media (posts/reels) agregados — esto SÍ tiene daily breakdown ---
    df = _fetch_instagram_media_aggregated(
        ig_id, date_from, date_to, page_token
    )

    # Si no hay media en el período, crear estructura mínima
    if df.empty:
        df = pd.DataFrame({
            "fecha": [pd.Timestamp(date_to)],
            "n_posts": [0], "likes": [0], "comentarios": [0],
        })

    df["compartidos"] = 0  # IG no expone shares totales en API pública
    df["engagement"] = df.get("likes", 0) + df.get("comentarios", 0)
    df["seguidores"] = period_totals.get("seguidores_actual", 0)

    # Inyectar totales del período en el ÚLTIMO día (para que sum() en
    # compute_period_kpis los recoja una sola vez como total).
    if not df.empty:
        df = df.sort_values("fecha").reset_index(drop=True)
        last_idx = df.index[-1]
        df.at[last_idx, "alcance"] = period_totals.get("alcance_periodo", 0)
        df.at[last_idx, "vistas_perfil"] = period_totals.get("vistas_perfil_periodo", 0)
        df.at[last_idx, "clicks_web"] = period_totals.get("clicks_web_periodo", 0)
        df.at[last_idx, "cuentas_engagement"] = period_totals.get("cuentas_engagement_periodo", 0)
        df.at[last_idx, "nuevos_seguidores_total"] = period_totals.get("nuevos_seguidores_periodo", 0)

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
