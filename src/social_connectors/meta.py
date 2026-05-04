# -*- coding: utf-8 -*-
"""
Conector Meta Graph API — Facebook Pages + Instagram Business.

Setup:
  1. https://developers.facebook.com → Crear app (tipo: Business).
  2. Permisos: pages_read_engagement, pages_show_list, instagram_basic,
     instagram_manage_insights, read_insights, business_management.
  3. Generar User Token en Graph API Explorer.
  4. Anotar Page ID y, si tienes IG, IG Business Account ID:
       GET /me/accounts → page_id (+ access_token de la página)
       GET /{page_id}?fields=instagram_business_account → ig_user_id

Credenciales en st.secrets:
    [meta]
    access_token = "EAAxxx..."          # User Token, suficiente para empezar
    facebook_page_id = "1234567890"
    instagram_user_id = "1789..."        # opcional

NOTA importante:
  - Los insights de Página NORMALMENTE requieren un Page Access Token.
    Este conector lo obtiene automáticamente vía /me/accounts si recibe
    un User Token con permiso pages_show_list.
  - Métricas: Meta deprecó varias en 2024. Intentamos una lista
    conservadora y descartamos las que devuelven error individualmente.
"""
from __future__ import annotations

import logging
from datetime import date

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
    """Como _get pero retorna None si falla (en lugar de excepción)."""
    import requests
    try:
        return _get(endpoint, params, token=token)
    except requests.HTTPError as exc:
        logger.warning("Graph API call failed: %s — %s", endpoint, exc)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Graph API call error: %s — %s", endpoint, exc)
        return None


def _get_page_access_token() -> str | None:
    """
    Obtiene el Page Access Token de la página configurada.

    Usa el User Token para llamar /me/accounts y extraer el access_token
    específico de la página. El Page Token tiene mejor acceso a Insights
    y suele durar más (60 días si proviene de un User Token long-lived).
    """
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
    base_endpoint: str,
    metric: str,
    period: str,
    date_from: date,
    date_to: date,
    token: str,
) -> list[dict]:
    """Llama insights con UNA métrica. Retorna lista de values o []."""
    params = {
        "metric": metric,
        "period": period,
        "since": str(date_from),
        "until": str(date_to),
    }
    data = _try_get(f"{base_endpoint}/insights", params, token=token)
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


def fetch_meta_facebook_data(
    date_from: date,
    date_to: date,
) -> pd.DataFrame:
    """
    Trae insights diarios de la Page de Facebook.

    Estrategia: usa Page Access Token si está disponible. Itera métricas
    una por una para que si alguna está deprecada el resto siga
    funcionando.
    """
    cfg = get_secret_dict("meta") or {}
    page_id = cfg.get("facebook_page_id")
    if not page_id:
        raise RuntimeError("Falta facebook_page_id en secrets.")

    page_token = _get_page_access_token() or cfg.get("access_token")

    # Métricas vigentes en v23.0 (post-deprecaciones 2024).
    metrics_day = [
        ("page_impressions", "impresiones"),
        ("page_impressions_unique", "alcance"),
        ("page_post_engagements", "engagement"),
        ("page_fan_adds", "nuevos_seguidores"),
        ("page_actions_post_reactions_total", "reacciones"),
        ("page_video_views", "vistas_video"),
        ("page_views_total", "vistas_pagina"),
    ]
    # page_fans es lifetime metric — solo period=day, valor acumulado.
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

    df = pd.DataFrame(list(rows.values()))
    if df.empty:
        return df

    df["fecha"] = pd.to_datetime(df["fecha"], errors="coerce")
    return df.sort_values("fecha").reset_index(drop=True)


def fetch_meta_instagram_data(
    date_from: date,
    date_to: date,
) -> pd.DataFrame:
    """
    Trae insights diarios del perfil de Instagram Business.

    Las métricas IG cambiaron mucho en 2024:
      - `impressions`, `profile_views` deprecadas/restringidas
      - Reemplazos: `views`, `reach`, `accounts_engaged`
    Estrategia: probar las nuevas y caer a las viejas si fallan.
    """
    cfg = get_secret_dict("meta") or {}
    ig_id = cfg.get("instagram_user_id")
    if not ig_id:
        raise RuntimeError("Falta instagram_user_id en secrets.")

    # IG insights pueden funcionar con User Token o Page Token.
    page_token = _get_page_access_token() or cfg.get("access_token")

    # IG insights: en v23.0 hay que pedir metric_type=total_value para
    # algunas. Probamos métricas estándar individualmente.
    metrics = [
        ("reach", "alcance", "day"),
        ("follower_count", "nuevos_seguidores", "day"),
        ("profile_views", "vistas_perfil", "day"),
        ("website_clicks", "clicks_web", "day"),
        ("accounts_engaged", "cuentas_engagement", "day"),
    ]

    rows: dict[str, dict] = {}
    for metric, alias, period in metrics:
        for v in _fetch_metric_safe(
            f"/{ig_id}", metric, period, date_from, date_to, page_token
        ):
            fecha = v["fecha"]
            value = v["value"]
            # Algunas métricas IG vienen como dict {"value": N} en
            # vez de int — normalizamos.
            if isinstance(value, dict):
                value = value.get("value", 0)
            rows.setdefault(fecha, {"fecha": fecha})[alias] = value

    df = pd.DataFrame(list(rows.values()))
    if df.empty:
        return df

    df["fecha"] = pd.to_datetime(df["fecha"], errors="coerce")
    return df.sort_values("fecha").reset_index(drop=True)
