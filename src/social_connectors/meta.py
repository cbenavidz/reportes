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


def diagnose_meta_connection() -> dict:
    """
    Diagnóstico de la conexión Meta. Hace una llamada de prueba al Page
    y devuelve el resultado o el error EXACTO (no redactado).

    Devuelve dict con:
      - ok: bool
      - mensaje: str descriptivo
      - detalle: respuesta o error crudo de la API
      - token_preview: primeros/últimos chars del token (para verificar)
    """
    import requests

    cfg = get_secret_dict("meta") or {}
    token = cfg.get("access_token", "")
    page_id = cfg.get("facebook_page_id", "")
    ig_id = cfg.get("instagram_user_id", "")

    if not token:
        return {
            "ok": False,
            "mensaje": "No hay access_token configurado en Streamlit Secrets.",
            "detalle": "Falta la sección [meta] o el campo access_token.",
            "token_preview": "(vacío)",
        }
    if not page_id:
        return {
            "ok": False,
            "mensaje": "No hay facebook_page_id configurado.",
            "detalle": "Falta el campo facebook_page_id en [meta].",
            "token_preview": f"{token[:12]}...{token[-6:]}",
        }

    # Llamada de prueba: traer nombre y seguidores del Page
    try:
        r = requests.get(
            f"{BASE_URL}/{page_id}",
            params={
                "fields": "name,fan_count,followers_count",
                "access_token": token,
            },
            timeout=30,
        )
        data = r.json()
        if r.status_code == 200 and "name" in data:
            return {
                "ok": True,
                "mensaje": (
                    f"✅ Token válido. Página: {data.get('name')} · "
                    f"{data.get('followers_count', data.get('fan_count', 0)):,} seguidores."
                ),
                "detalle": data,
                "token_preview": f"{token[:12]}...{token[-6:]}",
            }
        # Error de la API — extraer el mensaje real
        error_obj = data.get("error", {})
        return {
            "ok": False,
            "mensaje": (
                f"❌ La API rechazó el token. "
                f"Código: {error_obj.get('code', '?')} · "
                f"Tipo: {error_obj.get('type', '?')}"
            ),
            "detalle": error_obj.get("message", str(data)),
            "token_preview": f"{token[:12]}...{token[-6:]}",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "mensaje": f"❌ Error de red al contactar la API: {type(exc).__name__}",
            "detalle": str(exc),
            "token_preview": f"{token[:12]}...{token[-6:]}",
        }


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

    IMPORTANTE: Meta limita Instagram insights a 30 días máximo entre
    since y until. Si el rango pedido es mayor, paginamos en chunks
    de 30 días y SUMAMOS los resultados.

    Esto es correcto para métricas acumulativas (reach, views, likes,
    comments, shares, total_interactions). Para `follower_count` también
    suma (nuevos seguidores del período).
    """
    total_days = (date_to - date_from).days + 1
    if total_days <= 30:
        return _fetch_metric_total_value_chunk(
            endpoint, metric, date_from, date_to, token
        )

    # Paginar en bloques de 30 días
    chunk_size = 30
    total: int = 0
    found_any = False
    current = date_from
    while current <= date_to:
        chunk_end = min(current + timedelta(days=chunk_size - 1), date_to)
        val = _fetch_metric_total_value_chunk(
            endpoint, metric, current, chunk_end, token
        )
        if val is not None:
            total += val
            found_any = True
        current = chunk_end + timedelta(days=1)
    return total if found_any else None


def _fetch_metric_total_value_chunk(
    endpoint: str,
    metric: str,
    date_from: date,
    date_to: date,
    token: str,
) -> int | None:
    """Helper: llamada cruda a /insights para un rango de máximo 30 días."""
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


def _classify_facebook_post_type(post: dict) -> str:
    """
    Clasifica un post de FB en: Reel, Video, Foto, Álbum, Enlace, Texto.

    Lógica:
      1. Mira attachments[0].type (más confiable)
      2. Fallback a status_type
      3. Default: Texto
    """
    attachments = (post.get("attachments") or {}).get("data") or []
    if attachments:
        att = attachments[0]
        att_type = (att.get("type") or "").lower()
        media_type = (att.get("media_type") or "").lower()

        # Mapeo de tipos de Meta a etiquetas legibles
        if "reel" in att_type or "reel" in media_type:
            return "Reel"
        if att_type in ("video_inline", "video_autoplay", "video_share_youtube"):
            return "Video"
        if media_type == "video":
            return "Video"
        if att_type in ("photo",):
            return "Foto"
        if att_type in ("album",):
            return "Álbum"
        if att_type in ("share", "link", "external"):
            return "Enlace"
        if media_type == "photo":
            # Si hay múltiples subattachments, es álbum
            sub = att.get("subattachments", {}).get("data", [])
            if len(sub) > 1:
                return "Álbum"
            return "Foto"

    # Fallback a status_type
    status_type = (post.get("status_type") or "").lower()
    if status_type == "added_video":
        return "Video"
    if status_type in ("added_photos", "mobile_status_update"):
        return "Foto"
    if status_type == "shared_story":
        return "Compartido"
    if status_type == "created_note":
        return "Nota"

    return "Texto"


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
    # attachments: para clasificar tipo de post (foto/video/reel/álbum/etc.)
    fields = (
        "id,created_time,message,permalink_url,status_type,"
        "attachments{media_type,type,subattachments{media_type}},"
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
    # Paginar para soportar períodos largos (12 meses → 200-500 posts)
    all_data = []
    next_url_path = f"/{page_id}/published_posts"
    next_params = params
    pages_fetched = 0
    MAX_PAGES = 20  # safety: 20×100 = 2000 posts
    while next_url_path and pages_fetched < MAX_PAGES:
        data = _try_get(next_url_path, next_params, token=token)
        if not data and pages_fetched == 0:
            # Fallback al endpoint /posts si /published_posts no funciona
            data = _try_get(f"/{page_id}/posts", next_params, token=token)
        if not data:
            break
        all_data.extend(data.get("data", []))
        # Pagination via cursor
        paging = data.get("paging", {})
        cursors = paging.get("cursors", {})
        after = cursors.get("after")
        if not after or not data.get("data"):
            break
        next_params = {**params, "after": after}
        pages_fetched += 1

    if not all_data:
        return pd.DataFrame(columns=[
            "fecha", "post_id", "mensaje", "url",
            "likes", "comentarios", "compartidos", "reacciones_total",
        ])
    # Reformat para reusar el loop existente
    data = {"data": all_data}
    rows: list[dict] = []
    for p in data.get("data", []):
        created_full = p.get("created_time", "")
        created = created_full[:10]
        if not created:
            continue
        # Hora del post (UTC): created_time tiene formato 2024-04-15T14:30:00+0000
        hora = None
        try:
            if "T" in created_full:
                hora_str = created_full[11:13]
                hora = int(hora_str)
        except (ValueError, IndexError):
            hora = None
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
        # Clasificar tipo de post desde attachments + status_type
        tipo_post = _classify_facebook_post_type(p)
        rows.append({
            "fecha": created,
            "hora": hora,
            "post_id": p.get("id"),
            "tipo": tipo_post,
            "mensaje": (p.get("message") or "")[:200],
            "url": p.get("permalink_url", ""),
            "likes": likes_total,
            "comentarios": comments_total,
            "compartidos": shares_total,
            "reacciones_total": reactions_total,
            "impresiones_total": 0,
            "impresiones_pagadas": 0,
            "alcance_pagado": 0,
            "es_pagado": False,
        })
    if not rows:
        return pd.DataFrame(columns=[
            "fecha", "post_id", "mensaje", "url",
            "likes", "comentarios", "compartidos", "reacciones_total",
        ])
    df = pd.DataFrame(rows)
    df["fecha"] = pd.to_datetime(df["fecha"], errors="coerce")
    df["engagement"] = df["likes"] + df["comentarios"] + df["compartidos"]

    # ── Enriquecer con Post Insights (impresiones, alcance, video views) ──
    post_ids = df["post_id"].dropna().astype(str).tolist()
    if post_ids:
        insights = _fetch_facebook_posts_insights(post_ids, token)
        if insights:
            df["impresiones_total"] = df["post_id"].map(
                lambda pid: insights.get(str(pid), {}).get("post_impressions", 0)
            ).fillna(0)
            df["impresiones_pagadas"] = df["post_id"].map(
                lambda pid: insights.get(str(pid), {}).get("post_impressions_paid", 0)
            ).fillna(0)
            df["alcance_total"] = df["post_id"].map(
                lambda pid: insights.get(str(pid), {}).get("post_impressions_unique", 0)
            ).fillna(0)
            df["alcance_pagado"] = df["post_id"].map(
                lambda pid: insights.get(str(pid), {}).get("post_impressions_paid_unique", 0)
            ).fillna(0)
            df["alcance_organico"] = (
                df["alcance_total"] - df["alcance_pagado"]
            ).clip(lower=0)
            df["video_views"] = df["post_id"].map(
                lambda pid: insights.get(str(pid), {}).get("post_video_views", 0)
            ).fillna(0)
            df["es_pagado"] = df["impresiones_pagadas"] > 0

    return df.sort_values("engagement", ascending=False).reset_index(drop=True)


def _fetch_facebook_posts_insights(
    post_ids: list[str],
    token: str,
    batch_size: int = 50,
) -> dict:
    """
    Trae insights para una lista de post_ids vía batch requests.

    Métricas: post_impressions, post_impressions_paid, post_impressions_unique,
    post_impressions_paid_unique, post_video_views.

    Devuelve dict: {post_id: {metric_name: value}}
    """
    import json
    import requests
    if not post_ids:
        return {}
    metrics = (
        "post_impressions,post_impressions_paid,"
        "post_impressions_unique,post_impressions_paid_unique,"
        "post_video_views"
    )
    out: dict[str, dict] = {}
    # Procesar en lotes para evitar URLs demasiado largas
    for i in range(0, len(post_ids), batch_size):
        chunk = post_ids[i:i + batch_size]
        batch = [
            {
                "method": "GET",
                "relative_url": f"{pid}/insights?metric={metrics}",
            }
            for pid in chunk
        ]
        try:
            resp = requests.post(
                f"{BASE_URL}/",
                data={
                    "access_token": token,
                    "batch": json.dumps(batch),
                },
                timeout=30,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Batch insights failed: %s", exc)
            continue
        if resp.status_code != 200:
            logger.warning(
                "Batch insights HTTP %s: %s",
                resp.status_code, resp.text[:200],
            )
            continue
        try:
            batch_result = resp.json()
        except Exception:  # noqa: BLE001
            continue
        # Cada item del batch es {"code":200, "body":"json_str"}
        for pid, item in zip(chunk, batch_result):
            if not isinstance(item, dict):
                continue
            if item.get("code") != 200:
                continue
            try:
                body = json.loads(item.get("body", "{}"))
            except Exception:  # noqa: BLE001
                continue
            metrics_map: dict[str, float] = {}
            for d in body.get("data", []):
                name = d.get("name")
                values = d.get("values", [])
                if name and values:
                    v = values[0].get("value", 0)
                    if isinstance(v, (int, float)):
                        metrics_map[name] = v
            if metrics_map:
                out[str(pid)] = metrics_map
    return out


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


def fetch_meta_ads_insights(
    date_from: date,
    date_to: date,
) -> dict:
    """
    Trae insights agregados de TODAS las campañas activas en el período.

    Requiere permiso `ads_read`. Devuelve dict con keys:
        - tiene_acceso: bool (False si no hay ads_read o no hay ad accounts)
        - ad_accounts: lista de ad accounts encontradas
        - resumen: dict con totales (spend, impressions, reach, clicks, cpc, ctr, cpm)
        - por_account: lista de dicts con métricas por cuenta
        - top_campañas: lista de top 10 campañas por gasto
    """
    cfg = get_secret_dict("meta") or {}
    user_token = cfg.get("access_token")  # Page Token funciona si tiene ads_read
    page_id = cfg.get("facebook_page_id")
    if not user_token:
        return {"tiene_acceso": False, "error": "No hay token configurado"}

    # 1. Listar ad accounts del usuario
    accounts_data = _try_get(
        "/me/adaccounts",
        {"fields": "id,name,account_status,currency,timezone_name", "limit": 25},
        token=user_token,
    )
    if not accounts_data or not accounts_data.get("data"):
        # Probar en el contexto del Business
        accounts_data = _try_get(
            f"/{page_id}/connected_instagram_account",
            {"fields": "id"},
            token=user_token,
        )
        return {
            "tiene_acceso": False,
            "error": (
                "No se pudo listar ad accounts. Verifica que el token "
                "tenga permiso ads_read y que tu usuario tenga acceso "
                "a algún ad account."
            ),
        }

    ad_accounts = accounts_data.get("data", [])

    # 2. Para cada ad account activa, fetchear insights del período
    since_str = str(date_from)
    until_str = str(date_to)
    resumen = {
        "spend": 0, "impressions": 0, "reach": 0, "clicks": 0,
        "cpc_promedio": 0, "ctr_promedio": 0, "cpm_promedio": 0,
        "frequency_promedio": 0,
        "n_accounts": 0,
    }
    por_account = []
    top_campañas = []

    for acc in ad_accounts:
        acc_id = acc.get("id")  # formato: act_<id>
        acc_name = acc.get("name", "Sin nombre")
        currency = acc.get("currency", "USD")
        if not acc_id:
            continue

        # Insights del ad account agregados
        insights = _try_get(
            f"/{acc_id}/insights",
            {
                "fields": "spend,impressions,reach,clicks,cpc,ctr,cpm,frequency",
                "time_range": f'{{"since":"{since_str}","until":"{until_str}"}}',
                "level": "account",
            },
            token=user_token,
        )
        if not insights or not insights.get("data"):
            continue

        for ins in insights.get("data", []):
            spend = float(ins.get("spend", 0) or 0)
            imps = int(float(ins.get("impressions", 0) or 0))
            reach = int(float(ins.get("reach", 0) or 0))
            clicks = int(float(ins.get("clicks", 0) or 0))
            cpc = float(ins.get("cpc", 0) or 0)
            ctr = float(ins.get("ctr", 0) or 0)
            cpm = float(ins.get("cpm", 0) or 0)
            freq = float(ins.get("frequency", 0) or 0)
            por_account.append({
                "account_name": acc_name,
                "account_id": acc_id,
                "currency": currency,
                "spend": spend,
                "impressions": imps,
                "reach": reach,
                "clicks": clicks,
                "cpc": cpc,
                "ctr": ctr,
                "cpm": cpm,
                "frequency": freq,
            })
            resumen["spend"] += spend
            resumen["impressions"] += imps
            resumen["reach"] += reach
            resumen["clicks"] += clicks
            resumen["n_accounts"] += 1

        # Top campañas de esta account
        camp_insights = _try_get(
            f"/{acc_id}/insights",
            {
                "fields": "campaign_name,spend,impressions,reach,clicks,cpc,ctr,cpm",
                "time_range": f'{{"since":"{since_str}","until":"{until_str}"}}',
                "level": "campaign",
                "limit": 50,
            },
            token=user_token,
        )
        if camp_insights:
            for c in camp_insights.get("data", []):
                top_campañas.append({
                    "account": acc_name,
                    "campaña": c.get("campaign_name", "Sin nombre"),
                    "spend": float(c.get("spend", 0) or 0),
                    "impressions": int(float(c.get("impressions", 0) or 0)),
                    "reach": int(float(c.get("reach", 0) or 0)),
                    "clicks": int(float(c.get("clicks", 0) or 0)),
                    "cpc": float(c.get("cpc", 0) or 0),
                    "ctr": float(c.get("ctr", 0) or 0),
                    "cpm": float(c.get("cpm", 0) or 0),
                })

    # Calcular promedios ponderados
    if resumen["clicks"] > 0:
        resumen["cpc_promedio"] = resumen["spend"] / resumen["clicks"]
    if resumen["impressions"] > 0:
        resumen["ctr_promedio"] = (resumen["clicks"] / resumen["impressions"]) * 100
        resumen["cpm_promedio"] = (resumen["spend"] / resumen["impressions"]) * 1000

    # Sort top campaigns by spend
    top_campañas.sort(key=lambda x: x["spend"], reverse=True)

    return {
        "tiene_acceso": True,
        "ad_accounts": ad_accounts,
        "resumen": resumen,
        "por_account": por_account,
        "top_campañas": top_campañas[:10],
        "currency": ad_accounts[0].get("currency", "USD") if ad_accounts else "USD",
    }


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
    Evolución mensual de IG basada en posts (/media endpoint) +
    reconstrucción del histórico de seguidores desde el snapshot actual
    restando los nuevos seguidores ganados cada mes.
    """
    from calendar import monthrange as _monthrange

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

    # Construir el DF mensual base
    today_d = date.today()
    if rows:
        df = pd.DataFrame(rows)
        df["fecha"] = pd.to_datetime(df["fecha"])
        df["mes"] = df["fecha"].dt.to_period("M").dt.to_timestamp()
        monthly = df.groupby("mes", as_index=False).agg({
            "n_posts": "sum",
            "likes": "sum",
            "comentarios": "sum",
        })
    else:
        monthly = pd.DataFrame(columns=["mes", "n_posts", "likes", "comentarios"])

    # Asegurar que TODOS los meses del rango aparezcan (aunque sin posts)
    cutoff = today_d - timedelta(days=months * 31)
    months_idx = []
    y, m = cutoff.year, cutoff.month
    while True:
        first = date(y, m, 1)
        if first > today_d:
            break
        months_idx.append(pd.Timestamp(first))
        m += 1
        if m > 12:
            m = 1
            y += 1
    months_df = pd.DataFrame({"mes": months_idx})
    monthly = months_df.merge(monthly, on="mes", how="left").fillna({
        "n_posts": 0, "likes": 0, "comentarios": 0,
    })

    monthly["engagement"] = monthly["likes"] + monthly["comentarios"]

    # --- RECONSTRUIR HISTÓRICO DE SEGUIDORES ---
    # Para cada mes calendario, pedir follower_count (nuevos seguidores del mes)
    # Luego: seguidores_fin_mes_actual = snapshot - sum(nuevos seguidores meses futuros)
    nuevos_por_mes: dict[pd.Timestamp, int] = {}
    monthly_sorted = monthly.sort_values("mes").reset_index(drop=True)
    for ts in monthly_sorted["mes"]:
        mes_date = ts.date()
        y_, m_ = mes_date.year, mes_date.month
        first_of_month = date(y_, m_, 1)
        last_of_month = date(y_, m_, _monthrange(y_, m_)[1])
        # Limitar al rango con datos (no pedir futuro)
        rng_start = first_of_month
        rng_end = min(last_of_month, today_d)
        if rng_start > today_d:
            continue
        try:
            val = _fetch_metric_total_value_chunk(
                f"/{ig_id}", "follower_count",
                rng_start, rng_end, page_token,
            )
            if val is not None:
                nuevos_por_mes[ts] = val
        except Exception:  # noqa: BLE001
            pass

    # Reconstrucción hacia atrás:
    # seguidores_fin_mes = snapshot - sum(nuevos_seguidores_meses_posteriores)
    saldo_acumulado = seguidores_actuales
    seguidores_fin_mes_dict = {}
    # Recorrer del MÁS RECIENTE al más antiguo
    for ts in monthly_sorted["mes"].iloc[::-1]:
        seguidores_fin_mes_dict[ts] = saldo_acumulado
        # Para el mes anterior, restar los nuevos del mes actual
        saldo_acumulado -= int(nuevos_por_mes.get(ts, 0))

    monthly["seguidores_fin_mes"] = monthly["mes"].map(seguidores_fin_mes_dict)
    monthly["nuevos_seguidores_mes"] = monthly["mes"].map(
        lambda t: int(nuevos_por_mes.get(t, 0))
    ).fillna(0)

    return monthly.sort_values("mes").tail(months).reset_index(drop=True)


def _classify_instagram_post_type(media: dict) -> str:
    """
    Clasifica un media de IG en: Reel, Foto, Video, Carrusel, Story.
    """
    product_type = (media.get("media_product_type") or "").upper()
    media_type = (media.get("media_type") or "").upper()

    if product_type == "REELS":
        return "Reel"
    if product_type == "STORY":
        return "Story"
    if media_type == "CAROUSEL_ALBUM":
        return "Carrusel"
    if media_type == "VIDEO":
        return "Video"
    if media_type == "IMAGE":
        return "Foto"
    return media_type or "Otro"


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
    fields = (
        "id,timestamp,media_type,media_product_type,media_url,permalink,"
        "caption,like_count,comments_count"
    )
    params = {"fields": fields, "limit": 100}
    data = _try_get(f"/{ig_id}/media", params, token=page_token)
    if not data:
        return pd.DataFrame()
    rows = []
    for m in data.get("data", []):
        ts_full = m.get("timestamp", "")
        ts = ts_full[:10]
        if not ts:
            continue
        media_date = datetime.strptime(ts, "%Y-%m-%d").date()
        if media_date < date_from or media_date > date_to:
            continue
        # Extraer hora
        hora = None
        try:
            if "T" in ts_full:
                hora = int(ts_full[11:13])
        except (ValueError, IndexError):
            hora = None
        likes = int(m.get("like_count", 0) or 0)
        comments = int(m.get("comments_count", 0) or 0)
        rows.append({
            "fecha": ts,
            "hora": hora,
            "post_id": m.get("id"),
            "tipo": _classify_instagram_post_type(m),
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

    # Enriquecer con insights (reach, plays, saved, impressions)
    media_ids = df["post_id"].dropna().astype(str).tolist()
    if media_ids:
        ig_insights = _fetch_instagram_media_insights(
            media_ids, page_token, df_types=dict(zip(df["post_id"].astype(str), df["tipo"])),
        )
        if ig_insights:
            df["alcance_total"] = df["post_id"].map(
                lambda pid: ig_insights.get(str(pid), {}).get("reach", 0)
            ).fillna(0)
            df["impresiones_total"] = df["post_id"].map(
                lambda pid: ig_insights.get(str(pid), {}).get("impressions", 0)
            ).fillna(0)
            df["saved"] = df["post_id"].map(
                lambda pid: ig_insights.get(str(pid), {}).get("saved", 0)
            ).fillna(0)
            df["video_views"] = df["post_id"].map(
                lambda pid: (
                    ig_insights.get(str(pid), {}).get("plays", 0)
                    or ig_insights.get(str(pid), {}).get("video_views", 0)
                )
            ).fillna(0)

    return df.sort_values("engagement", ascending=False).head(n).reset_index(drop=True)


def _fetch_instagram_media_insights(
    media_ids: list[str],
    token: str,
    df_types: dict | None = None,
    batch_size: int = 50,
) -> dict:
    """
    Trae insights de IG media (posts, reels, carruseles) vía batch.

    Métricas dependen del tipo:
      - IMAGE/CAROUSEL: reach, impressions, saved
      - VIDEO/REEL: reach, plays, saved
      - STORY: reach, impressions, replies, taps_forward, taps_back, exits

    Devuelve dict: {media_id: {metric: value}}.
    """
    import json
    import requests
    if not media_ids:
        return {}

    df_types = df_types or {}

    def _metrics_for(mid: str) -> str:
        t = (df_types.get(mid) or "").lower()
        if "reel" in t or "video" in t:
            return "reach,plays,saved,total_interactions"
        if "story" in t:
            return "reach,impressions,replies,taps_forward,taps_back,exits"
        # Default: imagen/carrusel
        return "reach,impressions,saved,total_interactions"

    out: dict[str, dict] = {}
    for i in range(0, len(media_ids), batch_size):
        chunk = media_ids[i:i + batch_size]
        batch = [
            {
                "method": "GET",
                "relative_url": f"{mid}/insights?metric={_metrics_for(mid)}",
            }
            for mid in chunk
        ]
        try:
            resp = requests.post(
                f"{BASE_URL}/",
                data={"access_token": token, "batch": json.dumps(batch)},
                timeout=30,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Batch IG insights failed: %s", exc)
            continue
        if resp.status_code != 200:
            logger.warning(
                "Batch IG insights HTTP %s: %s",
                resp.status_code, resp.text[:200],
            )
            continue
        try:
            batch_result = resp.json()
        except Exception:  # noqa: BLE001
            continue
        for mid, item in zip(chunk, batch_result):
            if not isinstance(item, dict) or item.get("code") != 200:
                continue
            try:
                body = json.loads(item.get("body", "{}"))
            except Exception:  # noqa: BLE001
                continue
            metrics_map: dict[str, float] = {}
            for d in body.get("data", []):
                name = d.get("name")
                values = d.get("values", [])
                if name and values:
                    v = values[0].get("value", 0)
                    if isinstance(v, (int, float)):
                        metrics_map[name] = v
            if metrics_map:
                out[str(mid)] = metrics_map
    return out


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
    # `impressions` fue removida; v25 usa `views` (vistas del perfil + contenido).
    # Meta limita las llamadas a 30 días → pedimos POR MES calendario para
    # tener serie histórica correcta y no inyectar todo en un solo día.
    from calendar import monthrange as _monthrange

    metrics_to_fetch = [
        ("reach", "alcance"),
        ("views", "impresiones"),
        ("accounts_engaged", "cuentas_engagement"),
        ("profile_views", "vistas_perfil"),
        ("website_clicks", "clicks_web"),
        ("follower_count", "nuevos_seguidores_total"),
        ("likes", "likes_cuenta"),
        ("comments", "comentarios_cuenta"),
        ("shares", "compartidos_cuenta"),
        ("saves", "guardados"),
        ("total_interactions", "interacciones_totales"),
    ]

    # Generar lista de meses calendario en el rango [date_from, date_to]
    monthly_ranges: list[tuple[date, date]] = []
    y, m = date_from.year, date_from.month
    while True:
        first_of_month = date(y, m, 1)
        last_day_of_month = _monthrange(y, m)[1]
        last_of_month = date(y, m, last_day_of_month)
        chunk_start = max(first_of_month, date_from)
        chunk_end = min(last_of_month, date_to)
        if chunk_start > date_to:
            break
        monthly_ranges.append((chunk_start, chunk_end))
        m += 1
        if m > 12:
            m = 1
            y += 1
        if y > date_to.year + 1:
            break

    # Pedir métricas para CADA MES — almacenar tanto el total del período
    # como el desglose por mes
    period_totals: dict[str, int] = {}
    by_month: dict[date, dict[str, int]] = {}  # {mes_fin: {alias: value}}
    for mr_start, mr_end in monthly_ranges:
        month_key = mr_end  # el ÚLTIMO día del mes (o date_to si es el último mes)
        by_month.setdefault(month_key, {})
        for metric, alias in metrics_to_fetch:
            val = _fetch_metric_total_value_chunk(
                f"/{ig_id}", metric, mr_start, mr_end, page_token
            )
            if val is not None:
                period_totals[alias + "_periodo"] = (
                    period_totals.get(alias + "_periodo", 0) + val
                )
                by_month[month_key][alias] = val

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

    df["compartidos"] = 0  # se llena abajo con métrica agregada de cuenta
    df["engagement"] = df.get("likes", 0) + df.get("comentarios", 0)
    df["seguidores"] = period_totals.get("seguidores_actual", 0)

    # Inyectar métricas POR MES: agregar una fila virtual por cada mes
    # (último día del mes) con los totales mensuales del API. Esto evita
    # que todo el alcance/impresiones aparezca en un solo día y permite
    # que el agrupamiento mensual sea correcto.
    if not df.empty and by_month:
        df = df.sort_values("fecha").reset_index(drop=True)

        # Verificar si la métrica agregada `likes` de cuenta es mayor que
        # la suma por media — si sí, usar el agregado de cuenta como fuente
        likes_total = sum(b.get("likes_cuenta", 0) for b in by_month.values())
        comments_total = sum(b.get("comentarios_cuenta", 0) for b in by_month.values())
        shares_total = sum(b.get("compartidos_cuenta", 0) for b in by_month.values())
        use_account_likes = likes_total > df["likes"].sum()
        use_account_comments = comments_total > df["comentarios"].sum()
        use_account_shares = shares_total > 0

        if use_account_likes:
            df["likes"] = 0
        if use_account_comments:
            df["comentarios"] = 0
        if use_account_shares:
            df["compartidos"] = 0

        # Construir filas mensuales y agregarlas al df
        new_rows = []
        for month_end_date, vals in by_month.items():
            ts = pd.Timestamp(month_end_date)
            new_row = {"fecha": ts, "n_posts": 0}
            new_row["alcance"] = vals.get("alcance", 0)
            new_row["impresiones"] = vals.get("impresiones", 0)
            new_row["vistas_perfil"] = vals.get("vistas_perfil", 0)
            new_row["clicks_web"] = vals.get("clicks_web", 0)
            new_row["cuentas_engagement"] = vals.get("cuentas_engagement", 0)
            new_row["nuevos_seguidores_total"] = vals.get("nuevos_seguidores_total", 0)
            new_row["guardados"] = vals.get("guardados", 0)
            new_row["interacciones_totales"] = vals.get("interacciones_totales", 0)
            # Si usamos métricas de cuenta para likes/comments/shares, inyectar
            if use_account_likes:
                new_row["likes"] = vals.get("likes_cuenta", 0)
            if use_account_comments:
                new_row["comentarios"] = vals.get("comentarios_cuenta", 0)
            if use_account_shares:
                new_row["compartidos"] = vals.get("compartidos_cuenta", 0)
            new_rows.append(new_row)

        if new_rows:
            df_metrics_monthly = pd.DataFrame(new_rows)
            # Concatenar al df principal (las filas de métricas mensuales
            # tienen fecha = último día del mes y NO duplican posts)
            df = pd.concat([df, df_metrics_monthly], ignore_index=True)

        # Recalcular engagement con los valores actualizados
        df["engagement"] = (
            df["likes"].fillna(0)
            + df["comentarios"].fillna(0)
            + df["compartidos"].fillna(0)
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


# ===========================================================================
# Stories (Facebook e Instagram)
# ===========================================================================


def fetch_meta_facebook_stories(
    date_from: date,
    date_to: date,
) -> pd.DataFrame:
    """
    Trae Stories de Facebook Page con sus métricas básicas.

    NOTA: Facebook ha restringido mucho la API de Stories. El endpoint
    /{page_id}/stories puede devolver vacío o requerir permisos especiales.
    Si no funciona devuelve un DataFrame vacío sin error.
    """
    cfg = get_secret_dict("meta") or {}
    page_id = cfg.get("facebook_page_id")
    if not page_id:
        return pd.DataFrame()
    page_token = _get_page_access_token() or cfg.get("access_token")

    since_unix = int(datetime.combine(date_from, datetime.min.time()).timestamp())
    until_unix = int(datetime.combine(
        date_to + timedelta(days=1), datetime.min.time()
    ).timestamp())

    fields = "id,creation_time,status,url,media_type"
    params = {
        "fields": fields,
        "since": since_unix,
        "until": until_unix,
        "limit": 100,
    }
    data = _try_get(f"/{page_id}/stories", params, token=page_token)
    if not data or not data.get("data"):
        return pd.DataFrame(columns=[
            "fecha", "story_id", "tipo", "url", "alcance",
            "impresiones", "respuestas",
        ])

    rows = []
    for s in data.get("data", []):
        ts_full = s.get("creation_time", "")
        ts = ts_full[:10]
        if not ts:
            continue
        rows.append({
            "fecha": ts,
            "story_id": s.get("id"),
            "tipo": "Story",
            "url": s.get("url", ""),
            "media_type": s.get("media_type", ""),
        })
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["fecha"] = pd.to_datetime(df["fecha"], errors="coerce")

    # Insights por story
    story_ids = df["story_id"].dropna().astype(str).tolist()
    if story_ids:
        story_insights = _fetch_facebook_story_insights(story_ids, page_token)
        if story_insights:
            df["alcance"] = df["story_id"].map(
                lambda sid: story_insights.get(str(sid), {}).get("post_impressions_unique", 0)
            ).fillna(0)
            df["impresiones"] = df["story_id"].map(
                lambda sid: story_insights.get(str(sid), {}).get("post_impressions", 0)
            ).fillna(0)
    return df


def _fetch_facebook_story_insights(
    story_ids: list[str],
    token: str,
    batch_size: int = 50,
) -> dict:
    """Batch insights de stories de Facebook."""
    import json
    import requests
    if not story_ids:
        return {}
    out: dict[str, dict] = {}
    metrics = "post_impressions,post_impressions_unique"
    for i in range(0, len(story_ids), batch_size):
        chunk = story_ids[i:i + batch_size]
        batch = [
            {"method": "GET", "relative_url": f"{sid}/insights?metric={metrics}"}
            for sid in chunk
        ]
        try:
            resp = requests.post(
                f"{BASE_URL}/",
                data={"access_token": token, "batch": json.dumps(batch)},
                timeout=30,
            )
        except Exception:  # noqa: BLE001
            continue
        if resp.status_code != 200:
            continue
        try:
            batch_result = resp.json()
        except Exception:  # noqa: BLE001
            continue
        for sid, item in zip(chunk, batch_result):
            if not isinstance(item, dict) or item.get("code") != 200:
                continue
            try:
                body = json.loads(item.get("body", "{}"))
            except Exception:  # noqa: BLE001
                continue
            mm: dict[str, float] = {}
            for d in body.get("data", []):
                name = d.get("name")
                values = d.get("values", [])
                if name and values:
                    v = values[0].get("value", 0)
                    if isinstance(v, (int, float)):
                        mm[name] = v
            if mm:
                out[str(sid)] = mm
    return out


def fetch_meta_instagram_stories(
    date_from: date,
    date_to: date,
) -> pd.DataFrame:
    """
    Trae Stories de Instagram con sus métricas.

    IMPORTANTE: Las stories de Instagram solo existen 24h. Después de eso
    NO se pueden obtener vía API. Para histórico hay que descargarlas
    diariamente o usar el campo `stories` del IG user (que también solo
    da las activas).

    Si se quiere histórico, debe correrse esta función como cron diario.
    """
    cfg = get_secret_dict("meta") or {}
    ig_id = cfg.get("instagram_user_id")
    if not ig_id:
        return pd.DataFrame()
    page_token = _get_page_access_token() or cfg.get("access_token")

    # /{ig_id}/stories devuelve las stories ACTIVAS (últimas 24h)
    fields = "id,timestamp,media_type,media_url,permalink"
    params = {"fields": fields, "limit": 100}
    data = _try_get(f"/{ig_id}/stories", params, token=page_token)
    if not data or not data.get("data"):
        return pd.DataFrame(columns=[
            "fecha", "story_id", "tipo", "url", "alcance",
            "impresiones", "respuestas", "exits", "taps_forward",
        ])

    rows = []
    for s in data.get("data", []):
        ts_full = s.get("timestamp", "")
        ts = ts_full[:10]
        if not ts:
            continue
        sd = datetime.strptime(ts, "%Y-%m-%d").date()
        if sd < date_from or sd > date_to:
            continue
        rows.append({
            "fecha": ts,
            "story_id": s.get("id"),
            "tipo": "Story",
            "url": s.get("permalink", ""),
            "media_type": s.get("media_type", ""),
        })
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["fecha"] = pd.to_datetime(df["fecha"], errors="coerce")

    # Insights
    story_ids = df["story_id"].dropna().astype(str).tolist()
    if story_ids:
        si = _fetch_instagram_media_insights(
            story_ids, page_token,
            df_types={sid: "Story" for sid in story_ids},
        )
        if si:
            df["alcance"] = df["story_id"].map(
                lambda sid: si.get(str(sid), {}).get("reach", 0)
            ).fillna(0)
            df["impresiones"] = df["story_id"].map(
                lambda sid: si.get(str(sid), {}).get("impressions", 0)
            ).fillna(0)
            df["respuestas"] = df["story_id"].map(
                lambda sid: si.get(str(sid), {}).get("replies", 0)
            ).fillna(0)
            df["exits"] = df["story_id"].map(
                lambda sid: si.get(str(sid), {}).get("exits", 0)
            ).fillna(0)
            df["taps_forward"] = df["story_id"].map(
                lambda sid: si.get(str(sid), {}).get("taps_forward", 0)
            ).fillna(0)
            df["taps_back"] = df["story_id"].map(
                lambda sid: si.get(str(sid), {}).get("taps_back", 0)
            ).fillna(0)
    return df
