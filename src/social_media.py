# -*- coding: utf-8 -*-
"""
Análisis de Redes Sociales y Google Analytics.

Por ahora trabaja con CSVs exportados manualmente desde cada plataforma.
Cuando se tengan credenciales de API, se reemplazarán los `parse_*_csv`
por funciones que descargan en vivo.

Plataformas soportadas:
  - Facebook (Meta Business Suite)
  - Instagram (Meta Business Suite — usa el mismo formato que FB)
  - TikTok (TikTok Business)
  - Google Analytics 4

Cada plataforma tiene su propio formato de export. El módulo intenta
detectar el formato automáticamente y normalizar a un esquema común:
  - fecha
  - métrica (impresiones, alcance, engagement, seguidores, etc.)
  - valor
  - plataforma
"""
from __future__ import annotations

import io
import logging
from datetime import date
from typing import Iterable

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# =============================================================================
# Detección automática de formato
# =============================================================================

def detect_csv_format(df: pd.DataFrame) -> str:
    """
    Detecta a qué plataforma pertenece un CSV exportado.

    Devuelve: 'meta_post' | 'meta_audiencia' | 'tiktok' | 'ga4' | 'unknown'.
    """
    cols = {c.lower().strip() for c in df.columns}

    # Meta Business Suite — exports comunes
    meta_signals = {"impresiones", "impressions", "alcance", "reach"}
    if cols & meta_signals:
        if "tipo de publicación" in cols or "post type" in cols:
            return "meta_post"
        if "fans" in cols or "seguidores" in cols or "followers" in cols:
            return "meta_audiencia"
        return "meta_post"

    # TikTok Business
    if cols & {"video views", "vistas de video", "video plays"}:
        return "tiktok"

    # Google Analytics 4
    ga4_signals = {"sesiones", "sessions", "usuarios", "users", "active users"}
    if cols & ga4_signals:
        return "ga4"

    return "unknown"


# =============================================================================
# Parseo de CSVs por plataforma
# =============================================================================

def _parse_date(s: pd.Series) -> pd.Series:
    """Intenta varios formatos de fecha comunes."""
    return pd.to_datetime(s, errors="coerce", dayfirst=False)


def parse_meta_csv(df: pd.DataFrame) -> pd.DataFrame:
    """
    Normaliza un CSV de Meta Business Suite (Facebook o Instagram).
    Columnas típicas (en español o inglés):
      - Fecha / Date
      - Impresiones / Impressions
      - Alcance / Reach
      - Reacciones / Reactions / Likes
      - Comentarios / Comments
      - Compartidos / Shares
      - Tipo de publicación (FB/IG distinguibles por valores)
    """
    df = df.copy()
    # Renombrar a esquema común
    rename_map = {
        "Fecha": "fecha", "Date": "fecha",
        "Impresiones": "impresiones", "Impressions": "impresiones",
        "Alcance": "alcance", "Reach": "alcance",
        "Reacciones": "reacciones", "Reactions": "reacciones",
        "Me gusta": "likes", "Likes": "likes",
        "Comentarios": "comentarios", "Comments": "comentarios",
        "Compartidos": "compartidos", "Shares": "compartidos",
        "Clics": "clics", "Clicks": "clics",
        "Tipo de publicación": "tipo", "Post type": "tipo",
        "Permalink": "url", "URL del post": "url",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})

    if "fecha" not in df.columns:
        # Buscar la primera columna de tipo fecha
        for c in df.columns:
            if "fecha" in c.lower() or "date" in c.lower():
                df = df.rename(columns={c: "fecha"})
                break

    df["fecha"] = _parse_date(df.get("fecha", pd.Series([pd.NaT] * len(df))))

    # Tipos numéricos
    for col in ["impresiones", "alcance", "reacciones", "likes",
                "comentarios", "compartidos", "clics"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    # Engagement total
    df["engagement"] = (
        df.get("likes", 0)
        + df.get("comentarios", 0)
        + df.get("compartidos", 0)
        + df.get("reacciones", 0)
    )
    # Engagement rate sobre alcance
    if "alcance" in df.columns:
        df["engagement_rate"] = np.where(
            df["alcance"] > 0,
            df["engagement"] / df["alcance"].replace(0, np.nan) * 100,
            0,
        )

    return df


def parse_tiktok_csv(df: pd.DataFrame) -> pd.DataFrame:
    """
    Normaliza un CSV de TikTok Business / Creator analytics.
    """
    df = df.copy()
    rename_map = {
        "Date": "fecha", "Fecha": "fecha",
        "Video Views": "impresiones", "Vistas de video": "impresiones",
        "Likes": "likes", "Me gusta": "likes",
        "Comments": "comentarios", "Comentarios": "comentarios",
        "Shares": "compartidos", "Compartidos": "compartidos",
        "Profile views": "alcance", "Vistas de perfil": "alcance",
        "Followers": "seguidores", "Seguidores": "seguidores",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})

    df["fecha"] = _parse_date(df.get("fecha", pd.Series([pd.NaT] * len(df))))

    for col in ["impresiones", "alcance", "likes", "comentarios",
                "compartidos", "seguidores"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    df["engagement"] = (
        df.get("likes", 0) + df.get("comentarios", 0) + df.get("compartidos", 0)
    )
    if "impresiones" in df.columns:
        df["engagement_rate"] = np.where(
            df["impresiones"] > 0,
            df["engagement"] / df["impresiones"].replace(0, np.nan) * 100,
            0,
        )
    return df


def parse_ga4_csv(df: pd.DataFrame) -> pd.DataFrame:
    """
    Normaliza un CSV de Google Analytics 4 (export de Reportes).
    Columnas típicas: Fecha, Sesiones, Usuarios, Páginas vistas, Tasa de
    rebote, Duración de sesión, Conversiones.
    """
    df = df.copy()
    rename_map = {
        "Fecha": "fecha", "Date": "fecha",
        "Sesiones": "sesiones", "Sessions": "sesiones",
        "Usuarios": "usuarios", "Users": "usuarios", "Active users": "usuarios",
        "Páginas vistas": "paginas_vistas", "Page views": "paginas_vistas",
        "Tasa de rebote": "tasa_rebote", "Bounce rate": "tasa_rebote",
        "Duración de sesión": "duracion_sesion", "Session duration": "duracion_sesion",
        "Conversiones": "conversiones", "Conversions": "conversiones",
        "Ingresos": "ingresos", "Revenue": "ingresos",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})

    df["fecha"] = _parse_date(df.get("fecha", pd.Series([pd.NaT] * len(df))))

    for col in ["sesiones", "usuarios", "paginas_vistas",
                "conversiones", "ingresos"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    # Tasa de rebote típicamente viene en % o decimal
    if "tasa_rebote" in df.columns:
        df["tasa_rebote"] = pd.to_numeric(
            df["tasa_rebote"].astype(str).str.replace("%", "").str.replace(",", "."),
            errors="coerce",
        ).fillna(0)
    return df


def parse_csv_auto(file_or_df) -> tuple[pd.DataFrame, str]:
    """
    Parsea un CSV automáticamente detectando la plataforma.
    Devuelve (DataFrame normalizado, plataforma).
    """
    if isinstance(file_or_df, pd.DataFrame):
        df = file_or_df
    else:
        # Try varias separaciones (Meta usa coma, TikTok puede usar tab)
        try:
            df = pd.read_csv(file_or_df)
        except Exception:
            file_or_df.seek(0)
            try:
                df = pd.read_csv(file_or_df, sep="\t")
            except Exception:
                file_or_df.seek(0)
                df = pd.read_csv(file_or_df, sep=";")

    fmt = detect_csv_format(df)
    if fmt in ("meta_post", "meta_audiencia"):
        return parse_meta_csv(df), fmt
    if fmt == "tiktok":
        return parse_tiktok_csv(df), fmt
    if fmt == "ga4":
        return parse_ga4_csv(df), fmt
    return df, "unknown"


# =============================================================================
# KPIs comunes
# =============================================================================

def compute_period_kpis(
    df: pd.DataFrame,
    date_from: date | pd.Timestamp | None = None,
    date_to: date | pd.Timestamp | None = None,
) -> dict:
    """KPIs agregados sobre el período."""
    if df is None or df.empty:
        return {"impresiones": 0, "alcance": 0, "engagement": 0,
                "engagement_rate": 0.0, "n_dias": 0}

    sub = df.copy()
    if "fecha" in sub.columns:
        sub = sub.dropna(subset=["fecha"])
        if date_from is not None:
            sub = sub[sub["fecha"] >= pd.Timestamp(date_from)]
        if date_to is not None:
            sub = sub[sub["fecha"] <= pd.Timestamp(date_to)]

    if sub.empty:
        return {"impresiones": 0, "alcance": 0, "engagement": 0,
                "engagement_rate": 0.0, "n_dias": 0}

    out = {
        "impresiones": float(sub.get("impresiones", pd.Series([0])).sum()),
        "alcance": float(sub.get("alcance", pd.Series([0])).sum()),
        "engagement": float(sub.get("engagement", pd.Series([0])).sum()),
        "n_dias": int(sub["fecha"].dt.date.nunique()) if "fecha" in sub.columns else 0,
        "likes": float(sub.get("likes", pd.Series([0])).sum()),
        "comentarios": float(sub.get("comentarios", pd.Series([0])).sum()),
        "compartidos": float(sub.get("compartidos", pd.Series([0])).sum()),
        "clics": float(sub.get("clics", pd.Series([0])).sum()),
    }
    base = out["alcance"] or out["impresiones"] or 1
    out["engagement_rate"] = (out["engagement"] / base * 100) if base else 0.0
    return out


def compute_daily_aggregation(
    df: pd.DataFrame,
    metric: str = "engagement",
) -> pd.DataFrame:
    """Agregación diaria de una métrica para gráfica de tendencia."""
    if df is None or df.empty or "fecha" not in df.columns:
        return pd.DataFrame(columns=["fecha", metric])
    if metric not in df.columns:
        return pd.DataFrame(columns=["fecha", metric])
    sub = df.dropna(subset=["fecha"]).copy()
    sub["fecha_dia"] = sub["fecha"].dt.date
    agg = sub.groupby("fecha_dia")[metric].sum().reset_index()
    agg = agg.rename(columns={"fecha_dia": "fecha"})
    return agg
