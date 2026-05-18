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
    # Engagement rate solo válido si tenemos alcance o impresiones REALES.
    # Sin base, devolver 0 (no inflar con division por 1).
    base = out["alcance"] or out["impresiones"]
    if base > 0:
        out["engagement_rate"] = out["engagement"] / base * 100
    else:
        out["engagement_rate"] = 0.0
    return out


def compute_recommendations(
    posts_df: pd.DataFrame | None,
    period_kpis: dict,
    period_kpis_prev: dict | None = None,
    platform_name: str = "Facebook",
) -> list[dict]:
    """
    Genera recomendaciones inteligentes basadas en los datos.

    Devuelve lista de dicts con keys:
        - icon: emoji
        - tipo: 'positivo' | 'alerta' | 'oportunidad' | 'info'
        - titulo: str (corto, headline)
        - texto: str (recomendación detallada)
        - dato: str (la métrica que sustenta la reco, opcional)

    Heurísticas implementadas:
        1. Tendencia de engagement vs período anterior
        2. Frecuencia de publicación
        3. Tipo de contenido con mejor performance
        4. Mejor día de la semana
        5. Mejor franja horaria
        6. Variabilidad de engagement (consistencia)
        7. Top performer outliers
    """
    recs: list[dict] = []

    # ---- 1. Tendencia vs período anterior ----
    if period_kpis_prev:
        eng_actual = period_kpis.get("engagement", 0)
        eng_prev = period_kpis_prev.get("engagement", 0)
        if eng_prev > 0:
            delta_pct = (eng_actual - eng_prev) / eng_prev * 100
            if delta_pct > 20:
                recs.append({
                    "icon": "🚀",
                    "tipo": "positivo",
                    "titulo": "Engagement subiendo fuerte",
                    "texto": (
                        f"Tu engagement creció {delta_pct:+.0f}% vs el período "
                        f"anterior. Lo que estés haciendo está funcionando — "
                        "identifica qué cambió y duplica."
                    ),
                    "dato": f"{int(eng_actual):,} vs {int(eng_prev):,}",
                })
            elif delta_pct < -15:
                recs.append({
                    "icon": "⚠️",
                    "tipo": "alerta",
                    "titulo": "Engagement cayendo",
                    "texto": (
                        f"Tu engagement bajó {abs(delta_pct):.0f}% vs el período "
                        "anterior. Revisa qué cambió: cadencia, formatos, "
                        "horarios o tipo de contenido."
                    ),
                    "dato": f"{int(eng_actual):,} vs {int(eng_prev):,}",
                })

    # ---- 2. Frecuencia de publicación ----
    if posts_df is not None and not posts_df.empty:
        n_posts = len(posts_df)
        # Calcular días del período si tenemos fecha
        if "fecha" in posts_df.columns:
            try:
                fechas = pd.to_datetime(posts_df["fecha"])
                rango_dias = max((fechas.max() - fechas.min()).days, 1)
                posts_per_week = n_posts / rango_dias * 7
            except Exception:
                posts_per_week = n_posts / 4.3  # asumir mes
        else:
            posts_per_week = n_posts / 4.3

        if posts_per_week < 2:
            recs.append({
                "icon": "📅",
                "tipo": "alerta",
                "titulo": "Cadencia baja de publicación",
                "texto": (
                    f"Estás publicando {posts_per_week:.1f} posts/semana. "
                    f"Para {platform_name} se recomienda mínimo 3-5/semana "
                    "para mantener engagement. Considera un calendario "
                    "editorial."
                ),
                "dato": f"{n_posts} posts en el período",
            })
        elif posts_per_week > 14:
            recs.append({
                "icon": "🔥",
                "tipo": "alerta",
                "titulo": "Posiblemente publicas demasiado",
                "texto": (
                    f"Estás publicando {posts_per_week:.1f} posts/semana. "
                    "Demasiada frecuencia puede saturar a tu audiencia y "
                    "diluir el alcance. Prueba reducir a 5-10/semana y "
                    "enfócate en calidad."
                ),
                "dato": f"{n_posts} posts",
            })

        # ---- 3. Tipo de contenido con mejor performance ----
        if "tipo" in posts_df.columns and "engagement" in posts_df.columns:
            by_tipo = posts_df.groupby("tipo").agg(
                n=("post_id", "count") if "post_id" in posts_df.columns else ("tipo", "count"),
                engagement_promedio=("engagement", "mean"),
                engagement_total=("engagement", "sum"),
            ).reset_index()
            by_tipo = by_tipo[by_tipo["n"] >= 2]  # mínimo 2 posts del tipo
            if len(by_tipo) >= 2:
                by_tipo = by_tipo.sort_values("engagement_promedio", ascending=False)
                mejor = by_tipo.iloc[0]
                peor = by_tipo.iloc[-1]
                if mejor["engagement_promedio"] > peor["engagement_promedio"] * 1.5:
                    multiplo = mejor["engagement_promedio"] / max(peor["engagement_promedio"], 1)
                    recs.append({
                        "icon": "🎯",
                        "tipo": "oportunidad",
                        "titulo": f"Los {mejor['tipo']} funcionan mejor",
                        "texto": (
                            f"Tus posts tipo **{mejor['tipo']}** tienen "
                            f"{multiplo:.1f}× más engagement promedio que "
                            f"los **{peor['tipo']}**. Considera publicar "
                            f"más {mejor['tipo'].lower()}."
                        ),
                        "dato": (
                            f"{mejor['tipo']}: {mejor['engagement_promedio']:.0f} "
                            f"avg ({int(mejor['n'])} posts) · "
                            f"{peor['tipo']}: {peor['engagement_promedio']:.0f} "
                            f"avg ({int(peor['n'])} posts)"
                        ),
                    })

        # ---- 4. Mejor día de la semana ----
        if "fecha" in posts_df.columns and "engagement" in posts_df.columns:
            try:
                df_dia = posts_df.copy()
                df_dia["fecha"] = pd.to_datetime(df_dia["fecha"])
                df_dia["dia_semana"] = df_dia["fecha"].dt.day_name()
                by_dia = df_dia.groupby("dia_semana")["engagement"].agg([
                    ("n", "count"), ("avg", "mean"),
                ]).reset_index()
                by_dia = by_dia[by_dia["n"] >= 2]
                if len(by_dia) >= 3:
                    by_dia = by_dia.sort_values("avg", ascending=False)
                    mejor_dia = by_dia.iloc[0]
                    dias_es = {
                        "Monday": "lunes", "Tuesday": "martes",
                        "Wednesday": "miércoles", "Thursday": "jueves",
                        "Friday": "viernes", "Saturday": "sábado",
                        "Sunday": "domingo",
                    }
                    dia_es = dias_es.get(mejor_dia["dia_semana"], mejor_dia["dia_semana"])
                    recs.append({
                        "icon": "📆",
                        "tipo": "info",
                        "titulo": f"Tu mejor día: {dia_es}",
                        "texto": (
                            f"Los posts publicados en **{dia_es}** tienen "
                            f"el mejor engagement promedio. Considera "
                            "concentrar tus mejores contenidos ese día."
                        ),
                        "dato": (
                            f"{mejor_dia['avg']:.0f} engagement promedio "
                            f"({int(mejor_dia['n'])} posts)"
                        ),
                    })
            except Exception:
                pass

        # ---- 5. Mejor franja horaria ----
        if "hora" in posts_df.columns and "engagement" in posts_df.columns:
            try:
                df_hora = posts_df.dropna(subset=["hora"]).copy()
                if len(df_hora) >= 5:
                    df_hora["hora"] = df_hora["hora"].astype(int)
                    df_hora["franja"] = pd.cut(
                        df_hora["hora"],
                        bins=[-1, 6, 11, 14, 18, 23],
                        labels=[
                            "Madrugada (0-6)", "Mañana (7-11)",
                            "Mediodía (12-14)", "Tarde (15-18)",
                            "Noche (19-23)",
                        ],
                    )
                    by_franja = df_hora.groupby("franja", observed=True)[
                        "engagement"
                    ].agg([("n", "count"), ("avg", "mean")]).reset_index()
                    by_franja = by_franja[by_franja["n"] >= 2]
                    if len(by_franja) >= 2:
                        by_franja = by_franja.sort_values("avg", ascending=False)
                        mejor_franja = by_franja.iloc[0]
                        recs.append({
                            "icon": "🕐",
                            "tipo": "info",
                            "titulo": f"Mejor hora: {mejor_franja['franja']}",
                            "texto": (
                                f"Los posts publicados en **{mejor_franja['franja']}** "
                                "obtienen mejor engagement. Considera programar "
                                "tus posts más importantes en esa franja."
                            ),
                            "dato": (
                                f"{mejor_franja['avg']:.0f} engagement promedio "
                                f"({int(mejor_franja['n'])} posts)"
                            ),
                        })
            except Exception:
                pass

        # ---- 6. Top performer outlier ----
        if "engagement" in posts_df.columns and len(posts_df) >= 3:
            engs = posts_df["engagement"].sort_values(ascending=False)
            if engs.iloc[0] > engs.iloc[1:].mean() * 5 and engs.iloc[1:].mean() > 0:
                ratio = engs.iloc[0] / engs.iloc[1:].mean()
                recs.append({
                    "icon": "⭐",
                    "tipo": "oportunidad",
                    "titulo": "Tienes un post viral",
                    "texto": (
                        f"Tu mejor post tuvo {ratio:.0f}× más engagement que "
                        "el promedio del resto. Estudia qué lo hizo único "
                        "(formato, mensaje, horario, hashtags) y replica."
                    ),
                    "dato": (
                        f"{int(engs.iloc[0])} vs {int(engs.iloc[1:].mean())} "
                        "promedio del resto"
                    ),
                })

    # ---- 7. Engagement rate vs benchmarks ----
    er = period_kpis.get("engagement_rate", 0)
    if er > 0:
        if platform_name == "Facebook":
            if er < 1:
                recs.append({
                    "icon": "📉",
                    "tipo": "alerta",
                    "titulo": "Engagement rate bajo",
                    "texto": (
                        f"Tu engagement rate ({er:.2f}%) está debajo del "
                        "benchmark de Facebook (1-2%). Mejora con: "
                        "preguntas en captions, contenido nativo (no solo "
                        "links externos), CTAs claros."
                    ),
                    "dato": f"{er:.2f}% (benchmark: 1-2%)",
                })
            elif er > 5:
                recs.append({
                    "icon": "🌟",
                    "tipo": "positivo",
                    "titulo": "Engagement rate excepcional",
                    "texto": (
                        f"Tu engagement rate ({er:.2f}%) está MUY por encima "
                        "del benchmark de Facebook (1-2%). Tu audiencia está "
                        "muy comprometida. Considera invertir en pauta para "
                        "amplificar."
                    ),
                    "dato": f"{er:.2f}% (benchmark: 1-2%)",
                })
        elif platform_name == "Instagram":
            if er < 1:
                recs.append({
                    "icon": "📉",
                    "tipo": "alerta",
                    "titulo": "Engagement rate bajo",
                    "texto": (
                        f"Tu engagement rate ({er:.2f}%) está debajo del "
                        "benchmark de IG (1-3%). Mejora con: más reels, "
                        "carruseles, hashtags relevantes, stickers en "
                        "stories."
                    ),
                    "dato": f"{er:.2f}% (benchmark: 1-3%)",
                })

    return recs


def compute_post_breakdown_by_type(
    posts_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Agrupa posts por tipo y calcula métricas por categoría.

    Returns DataFrame con columnas:
        tipo, n_posts, total_likes, total_comentarios,
        total_compartidos, total_engagement, engagement_promedio
    """
    if posts_df is None or posts_df.empty or "tipo" not in posts_df.columns:
        return pd.DataFrame()

    agg_dict = {"post_id": "count"} if "post_id" in posts_df.columns else {}
    for col in ["likes", "comentarios", "compartidos", "engagement"]:
        if col in posts_df.columns:
            agg_dict[col] = "sum"

    if not agg_dict:
        return pd.DataFrame()

    by_tipo = posts_df.groupby("tipo").agg(agg_dict).reset_index()
    if "post_id" in by_tipo.columns:
        by_tipo = by_tipo.rename(columns={"post_id": "n_posts"})
    if "engagement" in by_tipo.columns and "n_posts" in by_tipo.columns:
        by_tipo["engagement_promedio"] = (
            by_tipo["engagement"] / by_tipo["n_posts"].replace(0, 1)
        )
    return by_tipo.sort_values("engagement", ascending=False).reset_index(drop=True)


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


# ===========================================================================
# Análisis ampliado por tipo de contenido (post / reel / story / etc.)
# ===========================================================================


def compute_engagement_rate_by_type(posts_df: pd.DataFrame) -> pd.DataFrame:
    """
    Desglose por tipo con métricas completas incluyendo engagement rate.

    Engagement rate = engagement / max(alcance, impresiones, 1) × 100
      - Si hay alcance_organico/total: lo usa
      - Si no, usa impresiones_total
      - Si no, cae a engagement/n_posts como aproximación

    Devuelve DataFrame con columnas:
        tipo, n_posts, share_posts_pct,
        total_likes, total_comentarios, total_compartidos, total_engagement,
        engagement_promedio, share_engagement_pct,
        total_impresiones, total_alcance, total_video_views,
        engagement_rate, ranking
    """
    if posts_df is None or posts_df.empty or "tipo" not in posts_df.columns:
        return pd.DataFrame()

    df = posts_df.copy()
    # Garantizar columnas necesarias
    for col in [
        "likes", "comentarios", "compartidos", "engagement",
        "impresiones_total", "impresiones_pagadas",
        "alcance_pagado", "alcance_organico", "alcance_total",
        "video_views", "saved",
    ]:
        if col not in df.columns:
            df[col] = 0
    if "engagement" not in df.columns or df["engagement"].sum() == 0:
        df["engagement"] = (
            df["likes"].fillna(0)
            + df["comentarios"].fillna(0)
            + df["compartidos"].fillna(0)
        )

    agg_dict: dict = {
        "post_id": "count" if "post_id" in df.columns else (lambda s: len(s)),
        "likes": "sum",
        "comentarios": "sum",
        "compartidos": "sum",
        "engagement": "sum",
        "impresiones_total": "sum",
        "impresiones_pagadas": "sum",
        "alcance_pagado": "sum",
        "alcance_organico": "sum",
        "alcance_total": "sum",
        "video_views": "sum",
        "saved": "sum",
    }
    if "post_id" not in df.columns:
        df["post_id"] = range(len(df))
    by_tipo = df.groupby("tipo").agg(agg_dict).reset_index()
    by_tipo = by_tipo.rename(columns={
        "post_id": "n_posts",
        "likes": "total_likes",
        "comentarios": "total_comentarios",
        "compartidos": "total_compartidos",
        "engagement": "total_engagement",
        "impresiones_total": "total_impresiones",
        "alcance_total": "total_alcance",
        "video_views": "total_video_views",
        "saved": "total_saved",
    })

    total_posts = by_tipo["n_posts"].sum() or 1
    total_eng = by_tipo["total_engagement"].sum() or 1
    by_tipo["share_posts_pct"] = by_tipo["n_posts"] / total_posts * 100
    by_tipo["share_engagement_pct"] = (
        by_tipo["total_engagement"] / total_eng * 100
    )
    by_tipo["engagement_promedio"] = (
        by_tipo["total_engagement"] / by_tipo["n_posts"].replace(0, 1)
    )

    # Engagement rate: usar alcance > impresiones > engagement promedio
    def _rate(row):
        eng = row["total_engagement"]
        if row.get("total_alcance", 0) > 0:
            return eng / row["total_alcance"] * 100
        if row.get("total_impresiones", 0) > 0:
            return eng / row["total_impresiones"] * 100
        # Sin denominador real: 0 (no podemos calcular rate verdadero)
        return 0.0
    by_tipo["engagement_rate"] = by_tipo.apply(_rate, axis=1)

    by_tipo = by_tipo.sort_values(
        "total_engagement", ascending=False
    ).reset_index(drop=True)
    by_tipo["ranking"] = range(1, len(by_tipo) + 1)
    return by_tipo


def compute_monthly_evolution_by_type(
    posts_df: pd.DataFrame,
    metric: str = "engagement",
) -> pd.DataFrame:
    """
    Evolución mensual de una métrica por tipo de contenido.

    Devuelve DataFrame ancho con columnas: mes, tipo1, tipo2, ...
    donde cada celda es la suma de la métrica para ese mes y tipo.
    """
    if (
        posts_df is None or posts_df.empty
        or "tipo" not in posts_df.columns
        or "fecha" not in posts_df.columns
    ):
        return pd.DataFrame()
    df = posts_df.copy()
    df["fecha"] = pd.to_datetime(df["fecha"], errors="coerce")
    df = df.dropna(subset=["fecha"])
    if df.empty:
        return pd.DataFrame()
    if metric not in df.columns:
        if metric == "n_posts":
            df["n_posts"] = 1
        else:
            return pd.DataFrame()

    df["mes"] = df["fecha"].dt.to_period("M").dt.to_timestamp()
    by_month = df.groupby(["mes", "tipo"], as_index=False)[metric].sum()
    pivot = by_month.pivot(index="mes", columns="tipo", values=metric).fillna(0)
    pivot = pivot.reset_index()
    return pivot


def compute_top_types_ranking(
    breakdown: pd.DataFrame,
    min_posts: int = 2,
) -> dict:
    """
    Devuelve 3 rankings:
      - mas_publicado: top 3 por # posts
      - mejor_engagement: top 3 por engagement promedio
      - mejor_rate: top 3 por engagement rate (solo tipos con denominador real)

    Cada uno con (tipo, valor) en orden descendente.
    """
    if breakdown is None or breakdown.empty:
        return {"mas_publicado": [], "mejor_engagement": [], "mejor_rate": []}

    elegibles = breakdown[breakdown["n_posts"] >= min_posts]
    if elegibles.empty:
        elegibles = breakdown

    mas_publicado = elegibles.nlargest(3, "n_posts")[
        ["tipo", "n_posts"]
    ].to_dict("records") if "n_posts" in elegibles.columns else []

    mejor_engagement = elegibles.nlargest(3, "engagement_promedio")[
        ["tipo", "engagement_promedio"]
    ].to_dict("records") if "engagement_promedio" in elegibles.columns else []

    rate_elegibles = elegibles[elegibles.get("engagement_rate", 0) > 0]
    mejor_rate = rate_elegibles.nlargest(3, "engagement_rate")[
        ["tipo", "engagement_rate"]
    ].to_dict("records") if not rate_elegibles.empty else []

    return {
        "mas_publicado": mas_publicado,
        "mejor_engagement": mejor_engagement,
        "mejor_rate": mejor_rate,
    }


def compute_type_recommendations(
    breakdown: pd.DataFrame,
    min_posts_for_signal: int = 3,
) -> list[dict]:
    """
    Genera recomendaciones accionables comparando volumen vs engagement
    por tipo de contenido.

    Cada recomendación: {tipo, prioridad, titulo, detalle, accion}.
    """
    recs: list[dict] = []
    if breakdown is None or breakdown.empty:
        return recs
    if "engagement_promedio" not in breakdown.columns:
        return recs

    elegibles = breakdown[breakdown["n_posts"] >= min_posts_for_signal].copy()
    if elegibles.empty:
        return recs

    eng_global = elegibles["engagement_promedio"].mean()
    if eng_global <= 0:
        return recs

    # 1. Tipos con alto engagement pero bajo volumen → publicar más
    candidatos_aumentar = elegibles[
        (elegibles["engagement_promedio"] > eng_global * 1.3)
        & (elegibles["share_posts_pct"] < 30)
    ].sort_values("engagement_promedio", ascending=False)
    for _, r in candidatos_aumentar.head(3).iterrows():
        recs.append({
            "tipo": "🚀 Publicar más",
            "prioridad": "alta",
            "titulo": (
                f"{r['tipo']}: {r['engagement_promedio']:.0f} engagement/post "
                f"(vs promedio {eng_global:.0f})"
            ),
            "detalle": (
                f"Solo {r['n_posts']:.0f} posts ({r['share_posts_pct']:.0f}% "
                f"del total) pero genera engagement por encima del promedio. "
                f"Tipo infrautilizado."
            ),
            "accion": (
                f"Incrementa publicaciones de tipo {r['tipo']} a 30-40% del mix."
            ),
        })

    # 2. Tipos con alto volumen pero bajo engagement → reducir o mejorar
    candidatos_reducir = elegibles[
        (elegibles["engagement_promedio"] < eng_global * 0.7)
        & (elegibles["share_posts_pct"] > 25)
    ].sort_values("share_posts_pct", ascending=False)
    for _, r in candidatos_reducir.head(2).iterrows():
        recs.append({
            "tipo": "📉 Reducir o mejorar",
            "prioridad": "media",
            "titulo": (
                f"{r['tipo']}: {r['share_posts_pct']:.0f}% del feed pero "
                f"solo {r['engagement_promedio']:.0f} engagement/post"
            ),
            "detalle": (
                f"Publicas {r['n_posts']:.0f} {r['tipo']} pero rinden menos "
                "que el promedio. Estás saturando el feed con contenido de "
                "bajo desempeño."
            ),
            "accion": (
                "Reduce frecuencia, o mejora calidad (hook visual, copy, "
                "CTA claro) antes de seguir publicando este tipo."
            ),
        })

    # 3. Tipo más rentable absoluto
    if not elegibles.empty:
        top = elegibles.nlargest(1, "engagement_promedio").iloc[0]
        share_pct = top["share_engagement_pct"]
        recs.append({
            "tipo": "🥇 Tipo más rentable",
            "prioridad": "baja",
            "titulo": (
                f"{top['tipo']}: genera {share_pct:.0f}% del engagement total"
            ),
            "detalle": (
                f"Con {top['n_posts']:.0f} posts capturó "
                f"{top['share_engagement_pct']:.0f}% del engagement. "
                f"Promedio: {top['engagement_promedio']:.0f} engagement/post."
            ),
            "accion": (
                "Mantén la calidad y frecuencia. Analiza qué hace bien y "
                "replícalo en otros tipos."
            ),
        })

    prio_order = {"alta": 0, "media": 1, "baja": 2}
    recs.sort(key=lambda r: prio_order.get(r.get("prioridad", "baja"), 3))
    return recs


def compute_cross_platform_comparison(
    fb_breakdown: pd.DataFrame,
    ig_breakdown: pd.DataFrame,
) -> pd.DataFrame:
    """
    Tabla comparativa de mismos tipos entre Facebook e Instagram.

    Hace match por tipo (case-insensitive). Solo conserva tipos presentes
    en al menos una de las dos plataformas.
    """
    if fb_breakdown is None or fb_breakdown.empty:
        fb = pd.DataFrame(columns=[
            "tipo", "n_posts", "engagement_promedio", "engagement_rate",
        ])
    else:
        fb = fb_breakdown[[
            c for c in [
                "tipo", "n_posts", "engagement_promedio",
                "engagement_rate", "total_engagement",
            ] if c in fb_breakdown.columns
        ]].copy()
    fb["tipo"] = fb["tipo"].astype(str).str.title()

    if ig_breakdown is None or ig_breakdown.empty:
        ig = pd.DataFrame(columns=[
            "tipo", "n_posts", "engagement_promedio", "engagement_rate",
        ])
    else:
        ig = ig_breakdown[[
            c for c in [
                "tipo", "n_posts", "engagement_promedio",
                "engagement_rate", "total_engagement",
            ] if c in ig_breakdown.columns
        ]].copy()
    ig["tipo"] = ig["tipo"].astype(str).str.title()

    fb = fb.rename(columns={
        "n_posts": "fb_n_posts",
        "engagement_promedio": "fb_engagement_promedio",
        "engagement_rate": "fb_engagement_rate",
        "total_engagement": "fb_total_engagement",
    })
    ig = ig.rename(columns={
        "n_posts": "ig_n_posts",
        "engagement_promedio": "ig_engagement_promedio",
        "engagement_rate": "ig_engagement_rate",
        "total_engagement": "ig_total_engagement",
    })
    merged = fb.merge(ig, on="tipo", how="outer").fillna(0)

    # Diferencial: cuál plataforma genera más engagement promedio por tipo
    def _ganador(row):
        fbv = row.get("fb_engagement_promedio", 0)
        igv = row.get("ig_engagement_promedio", 0)
        if fbv > igv * 1.2:
            return "Facebook"
        if igv > fbv * 1.2:
            return "Instagram"
        if fbv == 0 and igv == 0:
            return "—"
        return "Empate"
    merged["mejor_plataforma"] = merged.apply(_ganador, axis=1)
    return merged.sort_values(
        ["fb_total_engagement", "ig_total_engagement"],
        ascending=False,
    ).reset_index(drop=True)
