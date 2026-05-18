# -*- coding: utf-8 -*-
"""
Página: Redes Sociales y Google Analytics.

Reporte enriquecido con:
  - KPIs principales del período + comparativa vs período anterior
  - Histórico mensual (12 meses) de seguidores, engagement, posts, alcance
  - Top posts del período con preview y links
  - Tabla diaria de detalle
  - Soporte API (Meta, GA4) + fallback CSV manual
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from src.auth import logout_button, require_auth
from src.social_connectors import (
    diagnose_meta_connection,
    fetch_ga4_data,
    fetch_meta_ads_insights,
    fetch_meta_facebook_data,
    fetch_meta_facebook_monthly_evolution,
    fetch_meta_facebook_top_posts,
    fetch_meta_instagram_data,
    fetch_meta_instagram_monthly_evolution,
    fetch_meta_instagram_top_posts,
    is_ga4_configured,
    is_meta_configured,
    is_tiktok_configured,
)
# Importar funciones extras directamente del módulo (los Stories no están
# necesariamente exportados en __init__)
try:
    from src.social_connectors.meta import (
        fetch_meta_facebook_stories,
        fetch_meta_instagram_stories,
    )
except ImportError:
    fetch_meta_facebook_stories = None  # type: ignore
    fetch_meta_instagram_stories = None  # type: ignore

from src.social_media import (
    compute_cross_platform_comparison,
    compute_daily_aggregation,
    compute_engagement_rate_by_type,
    compute_monthly_evolution_by_type,
    compute_period_kpis,
    compute_post_breakdown_by_type,
    compute_recommendations,
    compute_top_types_ranking,
    compute_type_recommendations,
    parse_csv_auto,
)

st.set_page_config(
    page_title="Redes Sociales | Cartera",
    page_icon="📱",
    layout="wide",
)

require_auth()
logout_button()

st.title("📱 Redes Sociales y Google Analytics")
st.caption(
    "Análisis unificado con KPIs, evolución mensual, top posts y comparativa "
    "vs período anterior. Conexión vía API a Meta + GA4. CSV manual como fallback."
)

# Inicializar storage
if "social_data" not in st.session_state:
    st.session_state["social_data"] = {
        "facebook": None, "facebook_monthly": None, "facebook_top": None,
        "instagram": None, "instagram_monthly": None, "instagram_top": None,
        "tiktok": None,
        "ga4": None,
    }


# ---------------------------------------------------------------------------
# Filtro de período común
# ---------------------------------------------------------------------------
st.markdown("### 🗓️ Período de análisis")
col_p1, col_p2, col_p3 = st.columns([1, 1, 2])

today = date.today()
default_from = today - timedelta(days=30)

with col_p1:
    fecha_desde = st.date_input("Desde", value=default_from, key="rs_desde")
with col_p2:
    fecha_hasta = st.date_input("Hasta", value=today, key="rs_hasta")
with col_p3:
    quick = st.radio(
        "Atajos",
        options=["Personalizado", "Últimos 7 días", "Últimos 30 días",
                 "Últimos 90 días", "Últimos 12 meses",
                 "Mes actual", "Mes anterior"],
        index=2, horizontal=False, key="rs_atajo",
    )

if quick != "Personalizado":
    if quick == "Últimos 7 días":
        fecha_desde, fecha_hasta = today - timedelta(days=7), today
    elif quick == "Últimos 30 días":
        fecha_desde, fecha_hasta = today - timedelta(days=30), today
    elif quick == "Últimos 90 días":
        fecha_desde, fecha_hasta = today - timedelta(days=90), today
    elif quick == "Últimos 12 meses":
        fecha_desde, fecha_hasta = today - timedelta(days=365), today
    elif quick == "Mes actual":
        fecha_desde, fecha_hasta = today.replace(day=1), today
    elif quick == "Mes anterior":
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        fecha_desde, fecha_hasta = last_prev.replace(day=1), last_prev


# Agrupación temporal (Día / Semana / Mes / Trimestre)
periodo_dias_total = (fecha_hasta - fecha_desde).days + 1
default_group = (
    "Mes" if periodo_dias_total > 120
    else "Semana" if periodo_dias_total > 35
    else "Día"
)
group_options = ["Día", "Semana", "Mes", "Trimestre"]
group_idx = group_options.index(default_group)
agrupacion = st.radio(
    "Agrupar por",
    options=group_options,
    index=group_idx,
    horizontal=True,
    key="rs_agrupacion",
    help=(
        "Si tu período es largo (varios meses), agrupa por Semana o Mes "
        "para ver la evolución."
    ),
)
GROUP_FREQ = {
    "Día": "D", "Semana": "W-MON", "Mes": "MS", "Trimestre": "QS",
}[agrupacion]


# Período anterior (para comparativa)
periodo_dias = (fecha_hasta - fecha_desde).days + 1
fecha_desde_prev = fecha_desde - timedelta(days=periodo_dias)
fecha_hasta_prev = fecha_desde - timedelta(days=1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _delta_pct(actual: float, anterior: float) -> str | None:
    """Calcula % cambio. Retorna None si no se puede comparar."""
    if anterior is None or anterior == 0:
        return None
    return ((actual - anterior) / anterior) * 100


def _fmt_delta(actual: float, anterior: float | None) -> str:
    """Formato '↑ +12.3%' o '↓ -5.1%' o ''."""
    if anterior is None or anterior == 0:
        return ""
    pct = ((actual - anterior) / anterior) * 100
    arrow = "↑" if pct >= 0 else "↓"
    return f"{arrow} {pct:+.1f}%"


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
tab_fb, tab_ig, tab_tt, tab_ga, tab_cmp = st.tabs([
    "📘 Facebook", "📸 Instagram", "🎵 TikTok",
    "📊 Google Analytics", "🆚 Comparativo",
])


def _render_kpi_grid(
    df: pd.DataFrame,
    df_prev: pd.DataFrame | None,
    icon: str,
    is_ig: bool = False,
):
    """Renderiza grid de 8 KPIs con comparativa al período anterior."""
    k = compute_period_kpis(df, fecha_desde, fecha_hasta)
    k_prev = (
        compute_period_kpis(df_prev, fecha_desde_prev, fecha_hasta_prev)
        if df_prev is not None and not df_prev.empty
        else {"alcance": 0, "engagement": 0, "likes": 0, "comentarios": 0,
              "compartidos": 0, "impresiones": 0, "engagement_rate": 0, "n_dias": 0}
    )

    # Seguidores: tomamos el último valor de la columna 'seguidores' si existe
    seguidores_actual = 0
    seguidores_prev = 0
    n_posts_actual = 0
    n_posts_prev = 0
    if not df.empty:
        if "seguidores" in df.columns:
            sub = df.dropna(subset=["fecha"])
            sub = sub[(sub["fecha"] >= pd.Timestamp(fecha_desde))
                      & (sub["fecha"] <= pd.Timestamp(fecha_hasta))]
            seguidores_actual = int(sub["seguidores"].dropna().iloc[-1]) if not sub["seguidores"].dropna().empty else 0
        if "n_posts" in df.columns:
            sub = df.dropna(subset=["fecha"])
            sub = sub[(sub["fecha"] >= pd.Timestamp(fecha_desde))
                      & (sub["fecha"] <= pd.Timestamp(fecha_hasta))]
            n_posts_actual = int(sub["n_posts"].fillna(0).sum())
    if df_prev is not None and not df_prev.empty:
        if "seguidores" in df_prev.columns:
            sub = df_prev.dropna(subset=["fecha"])
            sub = sub[(sub["fecha"] >= pd.Timestamp(fecha_desde_prev))
                      & (sub["fecha"] <= pd.Timestamp(fecha_hasta_prev))]
            seguidores_prev = int(sub["seguidores"].dropna().iloc[-1]) if not sub["seguidores"].dropna().empty else 0
        if "n_posts" in df_prev.columns:
            sub = df_prev.dropna(subset=["fecha"])
            sub = sub[(sub["fecha"] >= pd.Timestamp(fecha_desde_prev))
                      & (sub["fecha"] <= pd.Timestamp(fecha_hasta_prev))]
            n_posts_prev = int(sub["n_posts"].fillna(0).sum())

    st.markdown(f"##### 📊 KPIs del período ({fecha_desde} → {fecha_hasta})")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric(
        "👥 Seguidores",
        f"{seguidores_actual:,}",
        delta=_fmt_delta(seguidores_actual, seguidores_prev) or None,
    )
    c2.metric(
        "📝 Posts publicados",
        f"{n_posts_actual:,}",
        delta=_fmt_delta(n_posts_actual, n_posts_prev) or None,
    )
    c3.metric(
        f"{icon} Alcance",
        f"{int(k['alcance']):,}",
        delta=_fmt_delta(k["alcance"], k_prev["alcance"]) or None,
    )
    c4.metric(
        "👁️ Impresiones",
        f"{int(k['impresiones']):,}",
        delta=_fmt_delta(k["impresiones"], k_prev["impresiones"]) or None,
    )

    c5, c6, c7, c8 = st.columns(4)
    c5.metric(
        "❤️ Engagement",
        f"{int(k['engagement']):,}",
        delta=_fmt_delta(k["engagement"], k_prev["engagement"]) or None,
    )
    c6.metric(
        "👍 Likes",
        f"{int(k['likes']):,}",
        delta=_fmt_delta(k["likes"], k_prev["likes"]) or None,
    )
    c7.metric(
        "💬 Comentarios",
        f"{int(k['comentarios']):,}",
        delta=_fmt_delta(k["comentarios"], k_prev["comentarios"]) or None,
    )
    c8.metric(
        "📈 Engagement rate",
        f"{k['engagement_rate']:.2f}%",
        delta=_fmt_delta(k["engagement_rate"], k_prev["engagement_rate"]) or None,
    )


def _render_monthly_evolution(monthly_df: pd.DataFrame, color: str, title: str):
    """Renderiza evolución mensual con gráficas."""
    if monthly_df is None or monthly_df.empty:
        st.info("Aún no se ha cargado la evolución mensual. Click el botón arriba.")
        return

    df = monthly_df.copy()
    df["mes_label"] = pd.to_datetime(df["mes"]).dt.strftime("%Y-%m")

    st.markdown(f"##### 📈 Evolución mensual — {title}")

    # 4 gráficas: seguidores, engagement, alcance, posts
    col_a, col_b = st.columns(2)
    with col_a:
        if "seguidores_fin_mes" in df.columns and df["seguidores_fin_mes"].sum() > 0:
            fig = px.line(
                df, x="mes_label", y="seguidores_fin_mes",
                markers=True, color_discrete_sequence=[color],
                labels={"mes_label": "Mes", "seguidores_fin_mes": "Seguidores"},
                title="Seguidores totales (fin de mes)",
            )
            fig.update_layout(height=300, margin=dict(l=0, r=0, t=40, b=0))
            st.plotly_chart(fig, use_container_width=True)
        elif "alcance" in df.columns:
            fig = px.line(
                df, x="mes_label", y="alcance", markers=True,
                color_discrete_sequence=[color],
                labels={"mes_label": "Mes", "alcance": "Alcance"},
                title="Alcance mensual",
            )
            fig.update_layout(height=300, margin=dict(l=0, r=0, t=40, b=0))
            st.plotly_chart(fig, use_container_width=True)

    with col_b:
        if "nuevos_seguidores" in df.columns and df["nuevos_seguidores"].sum() > 0:
            fig = px.bar(
                df, x="mes_label", y="nuevos_seguidores",
                color_discrete_sequence=[color],
                labels={"mes_label": "Mes", "nuevos_seguidores": "Nuevos"},
                title="Nuevos seguidores por mes",
            )
            fig.update_layout(height=300, margin=dict(l=0, r=0, t=40, b=0))
            st.plotly_chart(fig, use_container_width=True)

    col_c, col_d = st.columns(2)
    with col_c:
        if "engagement" in df.columns and df["engagement"].sum() > 0:
            fig = px.bar(
                df, x="mes_label", y="engagement",
                color_discrete_sequence=[color],
                labels={"mes_label": "Mes", "engagement": "Engagement"},
                title="Engagement total por mes",
            )
            fig.update_layout(height=300, margin=dict(l=0, r=0, t=40, b=0))
            st.plotly_chart(fig, use_container_width=True)

    with col_d:
        if "n_posts" in df.columns and df["n_posts"].sum() > 0:
            fig = px.bar(
                df, x="mes_label", y="n_posts",
                color_discrete_sequence=[color],
                labels={"mes_label": "Mes", "n_posts": "Posts"},
                title="Posts publicados por mes",
            )
            fig.update_layout(height=300, margin=dict(l=0, r=0, t=40, b=0))
            st.plotly_chart(fig, use_container_width=True)

    # Tabla detalle mensual
    with st.expander("📋 Detalle mensual (tabla)", expanded=False):
        show_df = df.copy()
        show_df["mes"] = show_df["mes_label"]
        show_df = show_df.drop(columns=["mes_label"])
        st.dataframe(
            show_df, use_container_width=True, hide_index=True,
        )


def _render_top_posts(top_df: pd.DataFrame, title: str):
    """Renderiza tabla de top posts del período."""
    if top_df is None or top_df.empty:
        return
    st.markdown(f"##### 🔥 Top posts del período — {title}")
    show_df = top_df.copy()
    if "fecha" in show_df.columns:
        show_df["fecha"] = pd.to_datetime(show_df["fecha"]).dt.strftime("%Y-%m-%d")
    cols_order = [c for c in [
        "fecha", "tipo", "mensaje", "caption",
        "likes", "comentarios", "compartidos", "engagement", "url",
    ] if c in show_df.columns]
    show_df = show_df[cols_order]
    st.dataframe(
        show_df,
        column_config={
            "url": st.column_config.LinkColumn("Ver post", display_text="🔗"),
            "tipo": st.column_config.TextColumn("Tipo", width="small"),
            "mensaje": st.column_config.TextColumn("Mensaje", width="large"),
            "caption": st.column_config.TextColumn("Caption", width="large"),
            "likes": st.column_config.NumberColumn(format="%,d"),
            "comentarios": st.column_config.NumberColumn(format="%,d"),
            "compartidos": st.column_config.NumberColumn(format="%,d"),
            "engagement": st.column_config.NumberColumn(format="%,d"),
        },
        use_container_width=True, hide_index=True, height=400,
    )


def _render_breakdown_by_type(top_df: pd.DataFrame, color: str, title: str):
    """Renderiza desglose por tipo de post (Foto/Video/Reel/etc)."""
    if top_df is None or top_df.empty or "tipo" not in top_df.columns:
        return
    breakdown = compute_post_breakdown_by_type(top_df)
    if breakdown.empty:
        return
    st.markdown(f"##### 🗂️ Desglose por tipo de post — {title}")

    col1, col2 = st.columns([1, 2])
    with col1:
        # Pie chart por número de posts
        if "n_posts" in breakdown.columns:
            fig = px.pie(
                breakdown, values="n_posts", names="tipo",
                title="Distribución por tipo",
                color_discrete_sequence=px.colors.sequential.Blues_r
                if color == "#1877F2" else px.colors.sequential.Reds_r,
            )
            fig.update_layout(height=320, margin=dict(l=0, r=0, t=40, b=0))
            st.plotly_chart(fig, use_container_width=True)

    with col2:
        # Bar chart de engagement promedio por tipo
        if "engagement_promedio" in breakdown.columns:
            fig = px.bar(
                breakdown.sort_values("engagement_promedio", ascending=True),
                x="engagement_promedio", y="tipo",
                orientation="h", text="engagement_promedio",
                color_discrete_sequence=[color],
                title="Engagement promedio por tipo",
                labels={
                    "engagement_promedio": "Engagement promedio",
                    "tipo": "Tipo",
                },
            )
            fig.update_traces(texttemplate="%{text:.0f}", textposition="outside")
            fig.update_layout(height=320, margin=dict(l=0, r=0, t=40, b=0))
            st.plotly_chart(fig, use_container_width=True)

    # Tabla detallada
    show_breakdown = breakdown.copy()
    column_config = {
        "tipo": st.column_config.TextColumn("Tipo"),
    }
    if "n_posts" in show_breakdown.columns:
        column_config["n_posts"] = st.column_config.NumberColumn(
            "# Posts", format="%,d"
        )
    for c in ["likes", "comentarios", "compartidos", "engagement"]:
        if c in show_breakdown.columns:
            column_config[c] = st.column_config.NumberColumn(
                c.title(), format="%,d"
            )
    if "engagement_promedio" in show_breakdown.columns:
        column_config["engagement_promedio"] = st.column_config.NumberColumn(
            "Engagement avg", format="%.1f"
        )
    st.dataframe(
        show_breakdown,
        column_config=column_config,
        use_container_width=True, hide_index=True,
    )


def _render_advanced_by_type(top_df: pd.DataFrame, color: str, plataforma: str):
    """
    Análisis avanzado por tipo de contenido:
    - Top 3 más rentables (publicación / engagement / rate)
    - Evolución mensual por tipo
    - Recomendaciones automáticas
    """
    if top_df is None or top_df.empty or "tipo" not in top_df.columns:
        return
    breakdown = compute_engagement_rate_by_type(top_df)
    if breakdown.empty:
        return

    st.markdown(f"##### 📊 Análisis avanzado por tipo — {plataforma}")

    # --- Top 3 rankings ---
    rankings = compute_top_types_ranking(breakdown, min_posts=2)
    cR1, cR2, cR3 = st.columns(3)
    with cR1:
        st.markdown("**📊 Más publicado**")
        for i, r in enumerate(rankings["mas_publicado"][:3]):
            st.markdown(
                f"{['🥇', '🥈', '🥉'][i]} **{r['tipo']}** — "
                f"{int(r['n_posts'])} posts"
            )
        if not rankings["mas_publicado"]:
            st.caption("Sin datos")
    with cR2:
        st.markdown("**🔥 Mejor engagement promedio**")
        for i, r in enumerate(rankings["mejor_engagement"][:3]):
            st.markdown(
                f"{['🥇', '🥈', '🥉'][i]} **{r['tipo']}** — "
                f"{r['engagement_promedio']:.0f} / post"
            )
        if not rankings["mejor_engagement"]:
            st.caption("Sin datos")
    with cR3:
        st.markdown("**💯 Mejor engagement rate (%)**")
        if rankings["mejor_rate"]:
            for i, r in enumerate(rankings["mejor_rate"][:3]):
                st.markdown(
                    f"{['🥇', '🥈', '🥉'][i]} **{r['tipo']}** — "
                    f"{r['engagement_rate']:.2f}%"
                )
        else:
            st.caption(
                "Necesita datos de alcance/impresiones. "
                "Vuelve a recargar para que jale los insights."
            )

    # --- Tabla ampliada por tipo ---
    st.markdown("**📋 Detalle por tipo con engagement rate**")
    cols_show = [
        c for c in [
            "ranking", "tipo", "n_posts", "share_posts_pct",
            "total_engagement", "engagement_promedio",
            "share_engagement_pct", "engagement_rate",
            "total_alcance", "total_impresiones", "total_video_views",
        ] if c in breakdown.columns
    ]
    st.dataframe(
        breakdown[cols_show],
        column_config={
            "ranking": st.column_config.NumberColumn("#", format="%d"),
            "tipo": "Tipo",
            "n_posts": st.column_config.NumberColumn("# Posts", format="%d"),
            "share_posts_pct": st.column_config.NumberColumn(
                "% del feed", format="%.1f%%"
            ),
            "total_engagement": st.column_config.NumberColumn(
                "Engagement", format="%,d"
            ),
            "engagement_promedio": st.column_config.NumberColumn(
                "Eng / post", format="%.0f"
            ),
            "share_engagement_pct": st.column_config.NumberColumn(
                "% del eng", format="%.1f%%"
            ),
            "engagement_rate": st.column_config.NumberColumn(
                "Eng rate", format="%.2f%%"
            ),
            "total_alcance": st.column_config.NumberColumn(
                "Alcance", format="%,d"
            ),
            "total_impresiones": st.column_config.NumberColumn(
                "Impresiones", format="%,d"
            ),
            "total_video_views": st.column_config.NumberColumn(
                "Video views", format="%,d"
            ),
        },
        use_container_width=True, hide_index=True,
    )

    # --- Evolución mensual por tipo ---
    if "fecha" in top_df.columns:
        st.markdown("**📈 Evolución mensual de engagement por tipo**")
        evol = compute_monthly_evolution_by_type(top_df, metric="engagement")
        if not evol.empty and len(evol.columns) > 1:
            evol_long = evol.melt(
                id_vars="mes", var_name="tipo", value_name="engagement",
            )
            evol_long["mes_label"] = pd.to_datetime(
                evol_long["mes"]
            ).dt.strftime("%Y-%m")
            fig = px.line(
                evol_long, x="mes_label", y="engagement",
                color="tipo", markers=True,
            )
            fig.update_layout(
                height=380, margin=dict(l=0, r=0, t=10, b=0),
                yaxis=dict(tickformat=",.0f"),
                legend=dict(orientation="h", yanchor="bottom", y=1.05, x=0),
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.caption("Datos insuficientes para evolución mensual.")

    # --- Recomendaciones ---
    recs = compute_type_recommendations(breakdown)
    if recs:
        st.markdown("**💡 Recomendaciones automáticas**")
        prio_emoji = {"alta": "🔴", "media": "🟡", "baja": "🟢"}
        for r in recs:
            with st.container(border=True):
                cH1, cH2 = st.columns([4, 1])
                with cH1:
                    st.markdown(f"### {r['tipo']} — {r['titulo']}")
                with cH2:
                    p = r.get("prioridad", "baja")
                    st.markdown(f"**{prio_emoji.get(p, '🟢')} {p.upper()}**")
                st.markdown(f"**Diagnóstico:** {r['detalle']}")
                st.markdown(f"**Acción:** {r['accion']}")


def _render_cross_platform_by_type(fb_top: pd.DataFrame, ig_top: pd.DataFrame):
    """Comparativo FB vs IG por tipo de contenido."""
    fb_break = compute_engagement_rate_by_type(fb_top) if fb_top is not None else None
    ig_break = compute_engagement_rate_by_type(ig_top) if ig_top is not None else None
    if (fb_break is None or fb_break.empty) and (ig_break is None or ig_break.empty):
        return
    comp = compute_cross_platform_comparison(fb_break, ig_break)
    if comp.empty:
        return
    st.markdown("### 🔁 Comparativo FB vs IG por tipo de contenido")
    st.caption(
        "Mismo tipo de contenido (Reel, Foto, Video) en ambas plataformas. "
        "La columna 'Mejor plataforma' marca dónde rinde más cada tipo."
    )
    cols_show = [
        c for c in [
            "tipo",
            "fb_n_posts", "fb_engagement_promedio", "fb_engagement_rate",
            "ig_n_posts", "ig_engagement_promedio", "ig_engagement_rate",
            "mejor_plataforma",
        ] if c in comp.columns
    ]
    st.dataframe(
        comp[cols_show],
        column_config={
            "tipo": "Tipo",
            "fb_n_posts": st.column_config.NumberColumn("FB Posts", format="%d"),
            "fb_engagement_promedio": st.column_config.NumberColumn(
                "FB Eng/post", format="%.0f"
            ),
            "fb_engagement_rate": st.column_config.NumberColumn(
                "FB Rate", format="%.2f%%"
            ),
            "ig_n_posts": st.column_config.NumberColumn("IG Posts", format="%d"),
            "ig_engagement_promedio": st.column_config.NumberColumn(
                "IG Eng/post", format="%.0f"
            ),
            "ig_engagement_rate": st.column_config.NumberColumn(
                "IG Rate", format="%.2f%%"
            ),
            "mejor_plataforma": "Mejor plataforma",
        },
        use_container_width=True, hide_index=True,
    )


def _render_ads_panel(ads_data: dict | None, color: str):
    """Panel completo de Marketing API: gasto, CPC, CTR, top campañas."""
    if not ads_data:
        return
    if not ads_data.get("tiene_acceso"):
        st.info(
            "ℹ️ **Marketing API no disponible**: "
            f"{ads_data.get('error', 'Verifica el permiso ads_read en el token.')}"
        )
        return

    resumen = ads_data.get("resumen", {})
    if resumen.get("spend", 0) == 0:
        st.info(
            "ℹ️ No se encontró gasto en pauta para el período seleccionado. "
            "Si tuviste campañas activas, verifica que el ad account esté "
            "vinculado correctamente a tu usuario."
        )
        return

    currency = ads_data.get("currency", "USD")
    sym = "$" if currency in ("USD", "COP", "MXN", "ARS") else currency + " "

    st.markdown("##### 💰 Pauta de Meta Ads — Resumen del período")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("💵 Gasto total", f"{sym}{resumen['spend']:,.0f} {currency}")
    c2.metric("👁️ Impresiones", f"{resumen['impressions']:,}")
    c3.metric("🎯 Alcance", f"{resumen['reach']:,}")
    c4.metric("🖱️ Clicks", f"{resumen['clicks']:,}")

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("💲 CPC", f"{sym}{resumen['cpc_promedio']:.2f}", help="Costo por click")
    c6.metric("📊 CTR", f"{resumen['ctr_promedio']:.2f}%", help="Click-through rate")
    c7.metric("📈 CPM", f"{sym}{resumen['cpm_promedio']:.2f}", help="Costo por mil impresiones")
    c8.metric(
        "🎯 # Cuentas", f"{resumen['n_accounts']:,}",
        help="Ad accounts con datos en el período"
    )

    # Por ad account
    por_account = ads_data.get("por_account", [])
    if len(por_account) > 1:
        st.markdown("##### 📋 Por Ad Account")
        df_accs = pd.DataFrame(por_account)
        st.dataframe(
            df_accs,
            column_config={
                "spend": st.column_config.NumberColumn("Gasto", format=f"{sym}%,.2f"),
                "impressions": st.column_config.NumberColumn("Imp.", format="%,d"),
                "reach": st.column_config.NumberColumn("Alcance", format="%,d"),
                "clicks": st.column_config.NumberColumn("Clicks", format="%,d"),
                "cpc": st.column_config.NumberColumn("CPC", format=f"{sym}%.2f"),
                "ctr": st.column_config.NumberColumn("CTR", format="%.2f%%"),
                "cpm": st.column_config.NumberColumn("CPM", format=f"{sym}%.2f"),
                "frequency": st.column_config.NumberColumn("Freq.", format="%.2f"),
            },
            use_container_width=True, hide_index=True,
        )

    # Top campañas
    top = ads_data.get("top_campañas", [])
    if top:
        st.markdown("##### 🔥 Top campañas por gasto")
        df_top = pd.DataFrame(top)
        st.dataframe(
            df_top,
            column_config={
                "campaña": st.column_config.TextColumn("Campaña", width="large"),
                "account": st.column_config.TextColumn("Account", width="medium"),
                "spend": st.column_config.NumberColumn("Gasto", format=f"{sym}%,.2f"),
                "impressions": st.column_config.NumberColumn("Imp.", format="%,d"),
                "reach": st.column_config.NumberColumn("Alcance", format="%,d"),
                "clicks": st.column_config.NumberColumn("Clicks", format="%,d"),
                "cpc": st.column_config.NumberColumn("CPC", format=f"{sym}%.2f"),
                "ctr": st.column_config.NumberColumn("CTR", format="%.2f%%"),
                "cpm": st.column_config.NumberColumn("CPM", format=f"{sym}%.2f"),
            },
            use_container_width=True, hide_index=True,
        )


def _render_recommendations(
    top_df: pd.DataFrame,
    df_period: pd.DataFrame,
    df_prev: pd.DataFrame | None,
    title: str,
):
    """Renderiza panel de recomendaciones inteligentes."""
    period_kpis = compute_period_kpis(df_period, fecha_desde, fecha_hasta)
    period_kpis_prev = (
        compute_period_kpis(df_prev, fecha_desde_prev, fecha_hasta_prev)
        if df_prev is not None and not df_prev.empty else None
    )
    recs = compute_recommendations(
        posts_df=top_df,
        period_kpis=period_kpis,
        period_kpis_prev=period_kpis_prev,
        platform_name=title,
    )
    if not recs:
        return
    st.markdown(f"##### 💡 Recomendaciones inteligentes — {title}")
    st.caption(
        "Análisis automático del período. Estas son sugerencias basadas en "
        "los patrones de tus datos."
    )

    color_map = {
        "positivo": "#d1fae5", "alerta": "#fee2e2",
        "oportunidad": "#fef3c7", "info": "#dbeafe",
    }
    border_map = {
        "positivo": "#10b981", "alerta": "#ef4444",
        "oportunidad": "#f59e0b", "info": "#3b82f6",
    }

    for r in recs:
        bg = color_map.get(r["tipo"], "#f3f4f6")
        border = border_map.get(r["tipo"], "#9ca3af")
        st.markdown(
            f"""
<div style="background:{bg};border-left:4px solid {border};
            padding:12px 16px;border-radius:8px;margin-bottom:8px;">
<div style="font-size:14px;font-weight:600;color:#111827;">
{r['icon']} {r['titulo']}
</div>
<div style="font-size:13px;color:#374151;margin-top:4px;">
{r['texto']}
</div>
{f'<div style="font-size:12px;color:#6b7280;margin-top:6px;font-style:italic;">{r["dato"]}</div>' if r.get('dato') else ''}
</div>
""",
            unsafe_allow_html=True,
        )


def _aggregate_by_period(df: pd.DataFrame, freq: str) -> pd.DataFrame:
    """
    Agrupa el DataFrame por la frecuencia seleccionada (D/W-MON/MS/QS).
    - Métricas acumulables (likes, engagement, alcance, etc.) → SUMA
    - 'seguidores' (snapshot lifetime) → ÚLTIMO valor del período
    """
    if df is None or df.empty or "fecha" not in df.columns:
        return pd.DataFrame()
    sub = df.dropna(subset=["fecha"]).copy()
    sub = sub[(sub["fecha"] >= pd.Timestamp(fecha_desde))
              & (sub["fecha"] <= pd.Timestamp(fecha_hasta))]
    if sub.empty:
        return pd.DataFrame()
    sub = sub.set_index("fecha").sort_index()

    sum_cols = [c for c in sub.columns if c not in ("seguidores",)]
    last_cols = [c for c in sub.columns if c == "seguidores"]

    agg_dict = {}
    for c in sum_cols:
        if pd.api.types.is_numeric_dtype(sub[c]):
            agg_dict[c] = "sum"
    for c in last_cols:
        if pd.api.types.is_numeric_dtype(sub[c]):
            agg_dict[c] = "last"

    if not agg_dict:
        return pd.DataFrame()

    grouped = sub.resample(freq).agg(agg_dict).reset_index()
    return grouped


_MES_ES = {
    1: "Ene", 2: "Feb", 3: "Mar", 4: "Abr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Ago", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dic",
}


def _format_period_labels(df: pd.DataFrame, freq: str) -> pd.DataFrame:
    """Agrega columna 'periodo' con etiquetas legibles según freq."""
    if df is None or df.empty or "fecha" not in df.columns:
        return df
    out = df.copy()
    out["fecha"] = pd.to_datetime(out["fecha"])
    if freq == "MS":  # Mes
        out["periodo"] = out["fecha"].apply(
            lambda d: f"{_MES_ES[d.month]} {d.year}"
        )
    elif freq == "QS":  # Trimestre
        out["periodo"] = out["fecha"].apply(
            lambda d: f"Q{((d.month - 1) // 3) + 1} {d.year}"
        )
    elif freq == "W-MON":  # Semana
        out["periodo"] = out["fecha"].apply(
            lambda d: f"Sem {d.isocalendar().week:02d} ({d.strftime('%d-%b')})"
        )
    else:  # Día
        out["periodo"] = out["fecha"].dt.strftime("%Y-%m-%d")
    return out


def _render_tendency(df: pd.DataFrame, color: str, title: str):
    """Gráfica de engagement agrupada por la frecuencia seleccionada."""
    grouped = _aggregate_by_period(df, GROUP_FREQ)
    if grouped.empty or "engagement" not in grouped.columns:
        return
    grouped = _format_period_labels(grouped, GROUP_FREQ)
    st.markdown(f"##### 📈 Engagement por {agrupacion.lower()} — {title}")
    # Para Día usamos area (continuous), para Mes/Trimestre/Semana usamos bar
    if GROUP_FREQ == "D":
        fig = px.area(
            grouped, x="periodo", y="engagement",
            color_discrete_sequence=[color],
            labels={"periodo": "Fecha", "engagement": "Engagement"},
            markers=True,
        )
    else:
        fig = px.bar(
            grouped, x="periodo", y="engagement",
            color_discrete_sequence=[color],
            labels={"periodo": agrupacion, "engagement": "Engagement"},
            text="engagement",
        )
        fig.update_traces(texttemplate="%{text:,.0f}", textposition="outside")
    fig.update_layout(height=320, margin=dict(l=0, r=0, t=10, b=0))
    st.plotly_chart(fig, use_container_width=True)


def _render_kpis_grouped(df: pd.DataFrame, color: str, title: str):
    """
    Tabla + 4 gráficas de evolución de KPIs por período seleccionado
    (Día/Semana/Mes/Trimestre).
    """
    grouped = _aggregate_by_period(df, GROUP_FREQ)
    if grouped.empty:
        return
    grouped = _format_period_labels(grouped, GROUP_FREQ)
    st.markdown(f"##### 📊 KPIs por {agrupacion.lower()} — {title}")

    chart_kind = "bar" if GROUP_FREQ != "D" else "area"

    # 4 gráficas en grid
    metrics_to_chart = []
    for col, label in [
        ("alcance", "Alcance"),
        ("engagement", "Engagement"),
        ("likes", "Likes"),
        ("n_posts", "Posts publicados"),
    ]:
        if col in grouped.columns and grouped[col].sum() > 0:
            metrics_to_chart.append((col, label))

    if metrics_to_chart:
        cols_pair = st.columns(2)
        for i, (col, label) in enumerate(metrics_to_chart):
            with cols_pair[i % 2]:
                if chart_kind == "bar":
                    fig = px.bar(
                        grouped, x="periodo", y=col,
                        color_discrete_sequence=[color],
                        labels={"periodo": agrupacion, col: label},
                        title=label, text=col,
                    )
                    fig.update_traces(
                        texttemplate="%{text:,.0f}", textposition="outside"
                    )
                else:
                    fig = px.area(
                        grouped, x="periodo", y=col,
                        color_discrete_sequence=[color],
                        labels={"periodo": agrupacion, col: label},
                        title=label, markers=True,
                    )
                fig.update_layout(
                    height=280, margin=dict(l=0, r=0, t=40, b=0),
                )
                st.plotly_chart(fig, use_container_width=True)

    # Seguidores como línea (snapshot)
    if "seguidores" in grouped.columns and grouped["seguidores"].sum() > 0:
        fig = px.line(
            grouped, x="periodo", y="seguidores",
            color_discrete_sequence=[color],
            labels={"periodo": agrupacion, "seguidores": "Seguidores"},
            markers=True, title="Seguidores totales",
        )
        fig.update_layout(height=280, margin=dict(l=0, r=0, t=40, b=0))
        st.plotly_chart(fig, use_container_width=True)

    with st.expander(f"📋 Tabla por {agrupacion.lower()}", expanded=False):
        show_df = grouped.copy()
        # Reordenar para que 'periodo' sea la primera columna
        cols = ["periodo"] + [c for c in show_df.columns if c not in ("fecha", "periodo")]
        show_df = show_df[cols]
        st.dataframe(show_df, use_container_width=True, hide_index=True)


def _platform_tab_meta(
    platform_key: str,
    icon: str,
    title: str,
    color: str,
    fetch_data_fn,
    fetch_monthly_fn,
    fetch_top_fn,
    instrucciones: str,
    api_status: str = "no_configured",
):
    """Renderiza pestaña Facebook o Instagram con todo el detalle."""
    st.markdown(f"### {icon} {title}")

    if api_status == "configured":
        st.success("✅ API conectada — datos en vivo.")
        # Diagnóstico de conexión: hace una llamada de prueba y muestra
        # el error REAL si el token falla (no redactado).
        with st.expander("🔍 Verificar conexión Meta API", expanded=False):
            if st.button(
                "Probar token ahora", key=f"diag_{platform_key}",
            ):
                diag = diagnose_meta_connection()
                if diag["ok"]:
                    st.success(diag["mensaje"])
                else:
                    st.error(diag["mensaje"])
                    st.code(str(diag["detalle"]), language="text")
                    st.caption(
                        f"Token en uso: `{diag['token_preview']}`. "
                        "Si el token expiró o es inválido, hay que "
                        "regenerarlo y actualizarlo en Streamlit Secrets."
                    )
    else:
        st.info("ℹ️ API no configurada. Sube CSV manual.")

    with st.expander(f"📥 Setup API + cómo obtener CSV", expanded=False):
        st.markdown(instrucciones)

    # Botón único para obtener todo
    if api_status == "configured":
        if st.button(
            f"🔄 Obtener datos del período ({fecha_desde} → {fecha_hasta})",
            key=f"refresh_{platform_key}", use_container_width=True,
            type="primary",
        ):
            try:
                with st.spinner(
                    "Descargando KPIs del período, período anterior, y top posts..."
                ):
                    df = fetch_data_fn(fecha_desde, fecha_hasta)
                    df_prev = fetch_data_fn(fecha_desde_prev, fecha_hasta_prev)
                    # n=200 para que el desglose por tipo capture toda la
                    # diversidad de formatos (no solo el top 20).
                    top = fetch_top_fn(fecha_desde, fecha_hasta, n=200)
                    st.session_state["social_data"][platform_key] = df
                    st.session_state["social_data"][f"{platform_key}_prev"] = df_prev
                    st.session_state["social_data"][f"{platform_key}_top"] = top
                    # Marketing API (solo Facebook)
                    if platform_key == "facebook":
                        try:
                            ads = fetch_meta_ads_insights(
                                fecha_desde, fecha_hasta
                            )
                            st.session_state["social_data"]["facebook_ads"] = ads
                        except Exception:  # noqa: BLE001
                            st.session_state["social_data"]["facebook_ads"] = None
                    st.success(
                        f"✅ Listo: {len(df):,} días · {len(top):,} posts del período."
                    )
            except Exception as exc:  # noqa: BLE001
                st.error(f"Error: {exc}")

    # CSV upload (siempre disponible)
    uploaded = st.file_uploader(
        f"O sube CSV manual",
        type=["csv", "xlsx"],
        key=f"upload_{platform_key}",
    )
    if uploaded is not None:
        try:
            if uploaded.name.endswith(".xlsx"):
                df_raw = pd.read_excel(uploaded)
                df, fmt = parse_csv_auto(df_raw)
            else:
                df, fmt = parse_csv_auto(uploaded)
            st.session_state["social_data"][platform_key] = df
            st.success(f"✅ CSV cargado · {fmt} · {len(df):,} filas")
        except Exception as exc:  # noqa: BLE001
            st.error(f"No se pudo leer: {exc}")

    df = st.session_state["social_data"].get(platform_key)
    df_prev = st.session_state["social_data"].get(f"{platform_key}_prev")
    top = st.session_state["social_data"].get(f"{platform_key}_top")

    if df is None or df.empty:
        st.info(
            "Click los botones arriba para descargar datos. "
            "Empieza por **Datos del período**."
        )
        return

    # 1. KPIs con comparativa
    _render_kpi_grid(df, df_prev, icon)

    # 2. Recomendaciones inteligentes (al inicio, lo más valioso)
    if top is not None and not top.empty:
        st.markdown("---")
        _render_recommendations(top, df, df_prev, title)

    st.markdown("---")

    # 3. Tendencia agrupada (Día/Semana/Mes según selector)
    _render_tendency(df, color, title)

    st.markdown("---")

    # 4. KPIs agrupados (gráficas + tabla por período)
    _render_kpis_grouped(df, color, title)

    st.markdown("---")

    # 5. Desglose por tipo de post (Foto/Video/Reel/etc.)
    if top is not None and not top.empty:
        _render_breakdown_by_type(top, color, title)
        st.markdown("---")
        # 5b. Análisis avanzado: rankings, engagement rate, evolución, recomendaciones
        with st.expander("🔬 Análisis avanzado por tipo (rankings + recomendaciones)", expanded=False):
            _render_advanced_by_type(top, color, title)
        st.markdown("---")

    # 6. Marketing API: pauta, CPC, CTR, top campañas (solo FB)
    if platform_key == "facebook":
        ads_data = st.session_state["social_data"].get("facebook_ads")
        if ads_data is not None:
            _render_ads_panel(ads_data, color)
            st.markdown("---")

    # 7. Top posts
    _render_top_posts(top, title)

    if top is not None and not top.empty:
        st.markdown("---")

    # 8. Detalle diario (raw)
    with st.expander(f"📋 Detalle diario raw ({len(df):,} filas)", expanded=False):
        st.dataframe(df, use_container_width=True, hide_index=True, height=400)


# ---------------------------------------------------------------------------
# Facebook
# ---------------------------------------------------------------------------
with tab_fb:
    _platform_tab_meta(
        platform_key="facebook",
        icon="📘",
        title="Facebook",
        color="#1877F2",
        fetch_data_fn=fetch_meta_facebook_data,
        fetch_monthly_fn=fetch_meta_facebook_monthly_evolution,
        fetch_top_fn=fetch_meta_facebook_top_posts,
        api_status="configured" if is_meta_configured() else "no_configured",
        instrucciones="""
**Pasos para descargar el CSV de Facebook:**

1. Entra a [Meta Business Suite](https://business.facebook.com).
2. Selecciona tu Página de Facebook.
3. Menú lateral → **Insights** (Estadísticas).
4. Arriba elige el rango de fechas (recomendado: últimos 90 días).
5. Click **Exportar datos** → **CSV** o **Excel**.
6. Sube aquí el archivo.

**API:** ya configurado en secrets — usa los botones de descarga.
""",
    )


# ---------------------------------------------------------------------------
# Instagram
# ---------------------------------------------------------------------------
with tab_ig:
    _platform_tab_meta(
        platform_key="instagram",
        icon="📸",
        title="Instagram",
        color="#E4405F",
        fetch_data_fn=fetch_meta_instagram_data,
        fetch_monthly_fn=fetch_meta_instagram_monthly_evolution,
        fetch_top_fn=fetch_meta_instagram_top_posts,
        api_status="configured" if is_meta_configured() else "no_configured",
        instrucciones="""
**Pasos para descargar el CSV de Instagram:**

1. Entra a [Meta Business Suite](https://business.facebook.com).
2. Cambia a tu cuenta de Instagram.
3. Menú lateral → **Insights**.
4. **Exportar datos** → **CSV** o **Excel**.
5. Sube aquí.

**API:** ya configurado en secrets.
""",
    )


# ---------------------------------------------------------------------------
# TikTok (sin cambios — pendiente de App Review)
# ---------------------------------------------------------------------------
with tab_tt:
    st.markdown("### 🎵 TikTok")
    if is_tiktok_configured():
        st.warning("⏳ API en proceso de aprobación.")
    else:
        st.info("ℹ️ API no configurada. Sube CSV manual.")
    with st.expander("📥 Cómo obtener CSV de TikTok", expanded=False):
        st.markdown("""
**Pasos:**
1. Entra a [TikTok Studio](https://studio.tiktok.com) o TikTok Business.
2. Asegúrate de tener cuenta **Business** o **Creator**.
3. Menú → **Analytics**.
4. Ajusta rango de fechas.
5. **Descargar datos** → **CSV**.
6. Sube aquí.
""")
    uploaded = st.file_uploader("Sube CSV de TikTok", type=["csv", "xlsx"], key="upload_tiktok")
    if uploaded is not None:
        try:
            if uploaded.name.endswith(".xlsx"):
                df_raw = pd.read_excel(uploaded)
                df, fmt = parse_csv_auto(df_raw)
            else:
                df, fmt = parse_csv_auto(uploaded)
            st.session_state["social_data"]["tiktok"] = df
            st.success(f"✅ {len(df):,} filas")
        except Exception as exc:
            st.error(f"Error: {exc}")
    df_tt = st.session_state["social_data"].get("tiktok")
    if df_tt is not None and not df_tt.empty:
        k = compute_period_kpis(df_tt, fecha_desde, fecha_hasta)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Alcance", f"{int(k['alcance']):,}")
        c2.metric("Impresiones", f"{int(k['impresiones']):,}")
        c3.metric("Engagement", f"{int(k['engagement']):,}")
        c4.metric("Engagement rate", f"{k['engagement_rate']:.2f}%")


# ---------------------------------------------------------------------------
# Google Analytics 4
# ---------------------------------------------------------------------------
with tab_ga:
    st.markdown("### 📊 Google Analytics 4")
    if is_ga4_configured():
        st.success("✅ API conectada.")
    else:
        st.info("ℹ️ API no configurada. Sube CSV manual o configura en secrets.")
    with st.expander("📥 Setup API + CSV manual", expanded=False):
        st.markdown("""
**Setup API:** ver `SETUP_REDES_SOCIALES.md` (sección Google Analytics 4).

**CSV manual:**
1. Entra a [analytics.google.com](https://analytics.google.com).
2. **Informes → Adquisición** (o el reporte que prefieras).
3. Ajusta rango de fechas.
4. **Compartir / Descargar → CSV**.
5. Sube aquí.
""")
    if is_ga4_configured():
        if st.button("🔄 Descargar datos GA4", key="refresh_ga4"):
            try:
                with st.spinner("Descargando..."):
                    df_ga = fetch_ga4_data(fecha_desde, fecha_hasta)
                    st.session_state["social_data"]["ga4"] = df_ga
                    st.success(f"✅ {len(df_ga):,} días descargados.")
            except Exception as exc:
                st.error(f"Error: {exc}")
    uploaded = st.file_uploader("Sube CSV de GA4", type=["csv", "xlsx"], key="upload_ga4")
    if uploaded is not None:
        try:
            if uploaded.name.endswith(".xlsx"):
                df_raw = pd.read_excel(uploaded)
                df, fmt = parse_csv_auto(df_raw)
            else:
                df, fmt = parse_csv_auto(uploaded)
            st.session_state["social_data"]["ga4"] = df
            st.success(f"✅ {len(df):,} filas")
        except Exception as exc:
            st.error(f"Error: {exc}")
    df_ga = st.session_state["social_data"].get("ga4")
    if df_ga is not None and not df_ga.empty:
        st.dataframe(df_ga, use_container_width=True, hide_index=True, height=400)


# ---------------------------------------------------------------------------
# Comparativo entre plataformas
# ---------------------------------------------------------------------------
with tab_cmp:
    st.markdown("### 🆚 Comparativo entre plataformas")
    st.caption(
        "Métricas combinadas de las plataformas con datos cargados."
    )

    # IMPORTANTE: verificar k PRIMERO y que sea DataFrame, porque
    # social_data ahora tiene entries que NO son DataFrames (ej:
    # facebook_ads es un dict, *_prev y *_top son DataFrames auxiliares).
    cargadas = [
        (k, v) for k, v in st.session_state["social_data"].items()
        if k in ("facebook", "instagram", "tiktok", "ga4")
        and isinstance(v, pd.DataFrame) and not v.empty
    ]
    if not cargadas:
        st.info(
            "Aún no has cargado datos. Ve a las pestañas y descarga primero."
        )
    else:
        rows = []
        for plat, df in cargadas:
            k = compute_period_kpis(df, fecha_desde, fecha_hasta)
            rows.append({
                "Plataforma": plat.title(),
                "Alcance": k["alcance"],
                "Impresiones": k["impresiones"],
                "Engagement": k["engagement"],
                "Likes": k["likes"],
                "Comentarios": k["comentarios"],
                "Compartidos": k["compartidos"],
                "Engagement rate (%)": k["engagement_rate"],
            })
        cmp_df = pd.DataFrame(rows)
        st.dataframe(
            cmp_df,
            column_config={
                "Alcance": st.column_config.NumberColumn(format="%,.0f"),
                "Impresiones": st.column_config.NumberColumn(format="%,.0f"),
                "Engagement": st.column_config.NumberColumn(format="%,.0f"),
                "Likes": st.column_config.NumberColumn(format="%,.0f"),
                "Comentarios": st.column_config.NumberColumn(format="%,.0f"),
                "Compartidos": st.column_config.NumberColumn(format="%,.0f"),
                "Engagement rate (%)": st.column_config.NumberColumn(format="%.2f%%"),
            },
            use_container_width=True, hide_index=True,
        )

        if cmp_df["Engagement"].sum() > 0:
            st.markdown("##### 📊 Engagement por plataforma")
            fig = px.bar(
                cmp_df, x="Plataforma", y="Engagement",
                color="Plataforma", text="Engagement",
                color_discrete_map={
                    "Facebook": "#1877F2", "Instagram": "#E4405F",
                    "Tiktok": "#FE2C55", "Ga4": "#F9AB00",
                },
            )
            fig.update_traces(texttemplate="%{text:,.0f}", textposition="outside")
            fig.update_layout(
                height=380, margin=dict(l=0, r=0, t=10, b=0), showlegend=False,
            )
            st.plotly_chart(fig, use_container_width=True)

        # --- Comparativo cruzado FB vs IG por tipo de contenido ---
        fb_top = st.session_state["social_data"].get("facebook_top")
        ig_top = st.session_state["social_data"].get("instagram_top")
        if (
            (fb_top is not None and isinstance(fb_top, pd.DataFrame) and not fb_top.empty)
            or (ig_top is not None and isinstance(ig_top, pd.DataFrame) and not ig_top.empty)
        ):
            st.markdown("---")
            _render_cross_platform_by_type(fb_top, ig_top)
