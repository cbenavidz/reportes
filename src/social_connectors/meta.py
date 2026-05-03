# -*- coding: utf-8 -*-
"""
Conector Meta Graph API — Facebook Pages + Instagram Business.

Setup (1 hora + tiempo de App Review):
  1. https://developers.facebook.com → Crear app (tipo: Business).
  2. App Review: solicitar permisos:
       - pages_read_engagement
       - pages_show_list
       - instagram_basic
       - instagram_manage_insights
       - read_insights
  3. Generar token de acceso:
       - Graph API Explorer: graph.facebook.com/v19.0
       - Seleccionar tu app + tu Page + permisos
       - Generar User Token → cambiar a Long-Lived (60 días)
       - Para producción: convertir a Page Token (no expira)
  4. Anotar Page ID y, si tienes IG, IG Business Account ID:
       GET /me/accounts → page_id
       GET /{page_id}?fields=instagram_business_account → ig_user_id

Credenciales en st.secrets:
    [meta]
    access_token = "EAAxxx..."
    facebook_page_id = "1234567890"
    instagram_user_id = "1789..."  # opcional

NOTA: este conector usa requests directos a Graph API. Si en el futuro
la app crece, se puede migrar a `facebook-business` SDK.
"""
from __future__ import annotations

import logging
from datetime import date

import pandas as pd

from ..secrets_loader import get_secret_dict

logger = logging.getLogger(__name__)

GRAPH_VERSION = "v19.0"
BASE_URL = f"https://graph.facebook.com/{GRAPH_VERSION}"


def is_meta_configured() -> bool:
    """True si hay credenciales válidas para Meta."""
    cfg = get_secret_dict("meta")
    if not cfg:
        return False
    return bool(cfg.get("access_token"))


def _get(endpoint: str, params: dict | None = None) -> dict:
    """Helper GET a Graph API."""
    import requests
    cfg = get_secret_dict("meta")
    if not cfg or not cfg.get("access_token"):
        raise RuntimeError("Meta no está configurado.")
    params = dict(params or {})
    params["access_token"] = cfg["access_token"]
    r = requests.get(f"{BASE_URL}{endpoint}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_meta_facebook_data(
    date_from: date,
    date_to: date,
) -> pd.DataFrame:
    """
    Trae insights diarios de la Page de Facebook.

    Métricas: page_impressions, page_post_engagements, page_fans, etc.
    """
    cfg = get_secret_dict("meta") or {}
    page_id = cfg.get("facebook_page_id")
    if not page_id:
        raise RuntimeError("Falta facebook_page_id en secrets.")

    metrics = [
        "page_impressions",
        "page_impressions_unique",  # alcance
        "page_post_engagements",
        "page_fans",
        "page_views_total",
    ]
    params = {
        "metric": ",".join(metrics),
        "period": "day",
        "since": str(date_from),
        "until": str(date_to),
    }
    data = _get(f"/{page_id}/insights", params)
    rows: dict[str, dict] = {}
    for m in data.get("data", []):
        name = m["name"]
        for v in m.get("values", []):
            fecha = v.get("end_time", "")[:10]
            if not fecha:
                continue
            rows.setdefault(fecha, {"fecha": fecha})[name] = v.get("value", 0)

    df = pd.DataFrame(list(rows.values()))
    if df.empty:
        return df
    df["fecha"] = pd.to_datetime(df["fecha"], errors="coerce")
    # Esquema común
    df = df.rename(columns={
        "page_impressions": "impresiones",
        "page_impressions_unique": "alcance",
        "page_post_engagements": "engagement",
        "page_fans": "seguidores",
        "page_views_total": "vistas_pagina",
    })
    return df.sort_values("fecha").reset_index(drop=True)


def fetch_meta_instagram_data(
    date_from: date,
    date_to: date,
) -> pd.DataFrame:
    """
    Trae insights diarios del perfil de Instagram Business.

    Métricas: impressions, reach, profile_views, follower_count.
    """
    cfg = get_secret_dict("meta") or {}
    ig_id = cfg.get("instagram_user_id")
    if not ig_id:
        raise RuntimeError("Falta instagram_user_id en secrets.")

    # IG insights es por endpoint distinto a FB.
    metrics = [
        "impressions", "reach", "profile_views", "follower_count",
    ]
    params = {
        "metric": ",".join(metrics),
        "period": "day",
        "since": str(date_from),
        "until": str(date_to),
    }
    data = _get(f"/{ig_id}/insights", params)
    rows: dict[str, dict] = {}
    for m in data.get("data", []):
        name = m["name"]
        for v in m.get("values", []):
            fecha = v.get("end_time", "")[:10]
            if not fecha:
                continue
            rows.setdefault(fecha, {"fecha": fecha})[name] = v.get("value", 0)

    df = pd.DataFrame(list(rows.values()))
    if df.empty:
        return df
    df["fecha"] = pd.to_datetime(df["fecha"], errors="coerce")
    df = df.rename(columns={
        "impressions": "impresiones",
        "reach": "alcance",
        "profile_views": "vistas_perfil",
        "follower_count": "seguidores",
    })
    return df.sort_values("fecha").reset_index(drop=True)
