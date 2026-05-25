# -*- coding: utf-8 -*-
"""
Conector TikTok — Display API (developers.tiktok.com).

Trae las estadísticas ORGÁNICAS de la cuenta propia: stats del perfil
(seguidores, likes totales, # videos) y la lista de videos con sus
métricas (vistas, likes, comentarios, compartidos).

──────────────────────────────────────────────────────────────────────
SETUP (lo hace el usuario en developers.tiktok.com):
  1. Registrarse como desarrollador y crear una app.
  2. Agregar el producto "Login Kit".
  3. Activar los scopes:
       user.info.basic, user.info.stats, video.list
  4. Configurar un Redirect URI.
  5. Enviar la app a revisión (TikTok tarda 2-4 semanas).
  6. Tras aprobación: hacer OAuth una vez para autorizar la cuenta y
     obtener access_token + refresh_token.

CREDENCIALES en st.secrets:
    [tiktok]
    client_key    = "aw..."        # de la app
    client_secret = "..."          # de la app
    refresh_token = "rft.xxx..."   # del OAuth (válido ~365 días)
    access_token  = "act.xxx..."   # opcional (se refresca solo)

El access_token de TikTok dura ~24h. Como st.secrets es de solo lectura,
el conector usa el refresh_token para obtener un access_token fresco en
cada carga. El refresh_token dura ~1 año.
──────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import logging
from datetime import date, datetime

import numpy as np
import pandas as pd

from ..secrets_loader import get_secret_dict

logger = logging.getLogger(__name__)

BASE_URL = "https://open.tiktokapis.com/v2"
TOKEN_URL = f"{BASE_URL}/oauth/token/"
USER_FIELDS = (
    "open_id,union_id,avatar_url,display_name,"
    "follower_count,following_count,likes_count,video_count"
)
VIDEO_FIELDS = (
    "id,create_time,title,view_count,like_count,"
    "comment_count,share_count"
)


def is_tiktok_configured() -> bool:
    """True si hay credenciales mínimas para usar la API."""
    cfg = get_secret_dict("tiktok")
    if not cfg:
        return False
    # Modo recomendado: refresh_token + client_key + client_secret.
    tiene_refresh = bool(
        cfg.get("refresh_token")
        and cfg.get("client_key")
        and cfg.get("client_secret")
    )
    return tiene_refresh or bool(cfg.get("access_token"))


def _refresh_access_token(cfg: dict) -> str:
    """Cambia el refresh_token por un access_token fresco."""
    import requests

    resp = requests.post(
        TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key": cfg["client_key"],
            "client_secret": cfg["client_secret"],
            "grant_type": "refresh_token",
            "refresh_token": cfg["refresh_token"],
        },
        timeout=30,
    )
    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(
            f"TikTok no devolvió access_token al refrescar: {data}"
        )
    return data["access_token"]


def _get_access_token(cfg: dict) -> str:
    """
    Devuelve un access_token utilizable. Si hay refresh_token + client
    credentials, refresca (lo más confiable porque el access_token de
    TikTok caduca en ~24h). Si no, usa el access_token guardado tal cual.
    """
    if cfg.get("refresh_token") and cfg.get("client_key") and cfg.get("client_secret"):
        return _refresh_access_token(cfg)
    token = cfg.get("access_token")
    if not token:
        raise RuntimeError("TikTok no está configurado (sin token).")
    return str(token)


def fetch_tiktok_account_stats() -> dict:
    """
    Snapshot del perfil: seguidores, likes totales, # de videos.
    Devuelve un dict (vacío si no está configurado o falla).
    """
    import requests

    cfg = get_secret_dict("tiktok")
    if not cfg:
        return {}
    try:
        token = _get_access_token(cfg)
        resp = requests.get(
            f"{BASE_URL}/user/info/",
            headers={"Authorization": f"Bearer {token}"},
            params={"fields": USER_FIELDS},
            timeout=30,
        )
        data = resp.json()
        user = (data.get("data") or {}).get("user") or {}
        return {
            "display_name": user.get("display_name", ""),
            "seguidores": int(user.get("follower_count", 0) or 0),
            "siguiendo": int(user.get("following_count", 0) or 0),
            "likes_totales": int(user.get("likes_count", 0) or 0),
            "n_videos": int(user.get("video_count", 0) or 0),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_tiktok_account_stats falló: %s", exc)
        return {}


def _fetch_all_videos(token: str, max_videos: int = 200) -> list[dict]:
    """Lista videos de la cuenta paginando con cursor."""
    import requests

    videos: list[dict] = []
    cursor = None
    while len(videos) < max_videos:
        body: dict = {"max_count": 20}
        if cursor is not None:
            body["cursor"] = cursor
        resp = requests.post(
            f"{BASE_URL}/video/list/",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            params={"fields": VIDEO_FIELDS},
            json=body,
            timeout=30,
        )
        data = resp.json()
        d = data.get("data") or {}
        batch = d.get("videos") or []
        videos.extend(batch)
        if not d.get("has_more") or not batch:
            break
        cursor = d.get("cursor")
        if cursor is None:
            break
    return videos[:max_videos]


def fetch_tiktok_data(date_from: date, date_to: date) -> pd.DataFrame:
    """
    Trae los videos publicados y los agrega por fecha de publicación,
    devolviendo un DataFrame con el MISMO formato que `parse_tiktok_csv`:
      fecha, impresiones, likes, comentarios, compartidos, seguidores,
      engagement, engagement_rate.

    NOTA: la Display API entrega métricas POR VIDEO (no una serie diaria
    de la cuenta). Aquí se agregan por día de publicación. `seguidores`
    es el snapshot actual del perfil (la API no da histórico).
    """
    cfg = get_secret_dict("tiktok")
    if not cfg:
        raise RuntimeError("TikTok no está configurado.")

    token = _get_access_token(cfg)
    videos = _fetch_all_videos(token)

    cols = ["fecha", "impresiones", "likes", "comentarios",
            "compartidos", "seguidores", "engagement", "engagement_rate"]
    if not videos:
        return pd.DataFrame(columns=cols)

    rows = []
    for v in videos:
        ts = v.get("create_time")
        try:
            fecha = datetime.utcfromtimestamp(int(ts)).date() if ts else None
        except (TypeError, ValueError, OSError):
            fecha = None
        rows.append({
            "fecha": fecha,
            "impresiones": int(v.get("view_count", 0) or 0),
            "likes": int(v.get("like_count", 0) or 0),
            "comentarios": int(v.get("comment_count", 0) or 0),
            "compartidos": int(v.get("share_count", 0) or 0),
        })
    df = pd.DataFrame(rows)
    df["fecha"] = pd.to_datetime(df["fecha"], errors="coerce")
    df = df.dropna(subset=["fecha"])

    # Filtrar al rango pedido
    df = df[
        (df["fecha"] >= pd.Timestamp(date_from))
        & (df["fecha"] <= pd.Timestamp(date_to))
    ]
    if df.empty:
        return pd.DataFrame(columns=cols)

    # Agregar por día de publicación
    diario = df.groupby("fecha", as_index=False).agg(
        impresiones=("impresiones", "sum"),
        likes=("likes", "sum"),
        comentarios=("comentarios", "sum"),
        compartidos=("compartidos", "sum"),
    )

    # Seguidores = snapshot actual del perfil (mismo valor en todas las filas)
    stats = fetch_tiktok_account_stats()
    diario["seguidores"] = int(stats.get("seguidores", 0) or 0)

    diario["engagement"] = (
        diario["likes"] + diario["comentarios"] + diario["compartidos"]
    )
    diario["engagement_rate"] = np.where(
        diario["impresiones"] > 0,
        diario["engagement"] / diario["impresiones"].replace(0, np.nan) * 100,
        0,
    )
    return diario.sort_values("fecha").reset_index(drop=True)


def fetch_tiktok_videos(max_videos: int = 200) -> pd.DataFrame:
    """
    Lista los videos de la cuenta con sus métricas, UNA FILA POR VIDEO
    (sin agregar por día). Ideal para tablas de detalle, rankings y
    resúmenes mensuales.

    Devuelve un DataFrame con columnas:
      id, fecha, titulo, vistas, likes, comentarios, compartidos,
      engagement, engagement_rate
    Vacío si TikTok no está configurado o si la API falla.
    """
    cols = ["id", "fecha", "titulo", "vistas", "likes", "comentarios",
            "compartidos", "engagement", "engagement_rate"]
    cfg = get_secret_dict("tiktok")
    if not cfg:
        return pd.DataFrame(columns=cols)
    try:
        token = _get_access_token(cfg)
        videos = _fetch_all_videos(token, max_videos=max_videos)
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_tiktok_videos falló: %s", exc)
        return pd.DataFrame(columns=cols)

    if not videos:
        return pd.DataFrame(columns=cols)

    rows = []
    for v in videos:
        ts = v.get("create_time")
        try:
            fecha = datetime.utcfromtimestamp(int(ts)) if ts else None
        except (TypeError, ValueError, OSError):
            fecha = None
        vistas = int(v.get("view_count", 0) or 0)
        likes = int(v.get("like_count", 0) or 0)
        coment = int(v.get("comment_count", 0) or 0)
        comp = int(v.get("share_count", 0) or 0)
        eng = likes + coment + comp
        rows.append({
            "id": str(v.get("id", "")),
            "fecha": fecha,
            "titulo": (v.get("title") or "").strip(),
            "vistas": vistas,
            "likes": likes,
            "comentarios": coment,
            "compartidos": comp,
            "engagement": eng,
            "engagement_rate": (eng / vistas * 100.0) if vistas > 0 else 0.0,
        })

    df = pd.DataFrame(rows, columns=cols)
    df["fecha"] = pd.to_datetime(df["fecha"], errors="coerce")
    return df.sort_values("fecha", ascending=False).reset_index(drop=True)
