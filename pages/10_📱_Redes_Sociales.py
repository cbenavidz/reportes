# -*- coding: utf-8 -*-
"""
Página: Redes Sociales y Google Analytics.

Versión inicial: cargas CSVs exportados manualmente desde cada plataforma.
La app detecta automáticamente el formato y calcula KPIs.

Plataformas soportadas:
  - Facebook (Meta Business Suite)
  - Instagram (Meta Business Suite)
  - TikTok (TikTok Business / Creator)
  - Google Analytics 4

Cuando se obtengan tokens de API, los conectores se reemplazarán por
descargas en vivo.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from src.auth import logout_button, require_auth
from src.social_connectors import (
    fetch_ga4_data,
    fetch_meta_facebook_data,
    fetch_meta_instagram_data,
    is_ga4_configured,
    is_meta_configured,
    is_tiktok_configured,
)
from src.social_media import (
    compute_daily_aggregation,
    compute_period_kpis,
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
    "Análisis unificado de Facebook, Instagram, TikTok y Google Analytics. "
    "Por ahora carga CSVs exportados manualmente desde cada plataforma. "
    "Próximamente: conexión vía API para actualización automática."
)

# Inicializar storage en session_state
if "social_data" not in st.session_state:
    st.session_state["social_data"] = {
        "facebook": None,
        "instagram": None,
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
                 "Últimos 90 días", "Mes actual", "Mes anterior"],
        index=2, horizontal=False, key="rs_atajo",
    )

if quick != "Personalizado":
    if quick == "Últimos 7 días":
        fecha_desde, fecha_hasta = today - timedelta(days=7), today
    elif quick == "Últimos 30 días":
        fecha_desde, fecha_hasta = today - timedelta(days=30), today
    elif quick == "Últimos 90 días":
        fecha_desde, fecha_hasta = today - timedelta(days=90), today
    elif quick == "Mes actual":
        fecha_desde, fecha_hasta = today.replace(day=1), today
    elif quick == "Mes anterior":
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        fecha_desde, fecha_hasta = last_prev.replace(day=1), last_prev


# ---------------------------------------------------------------------------
# Tabs por plataforma
# ---------------------------------------------------------------------------
tab_fb, tab_ig, tab_tt, tab_ga, tab_cmp = st.tabs([
    "📘 Facebook", "📸 Instagram", "🎵 TikTok",
    "📊 Google Analytics", "🆚 Comparativo",
])


def _platform_tab(
    platform_key: str,
    icon: str,
    title: str,
    instrucciones: str,
    color: str,
    api_status: str = "no_configured",  # 'configured' | 'no_configured' | 'pending'
    api_fetch_fn=None,
):
    """Renderiza una pestaña genérica de plataforma con soporte API + CSV."""
    st.markdown(f"### {icon} {title}")

    # Banner de estado de API
    if api_status == "configured":
        st.success(
            f"✅ API conectada — datos en vivo. "
            "Cambia el período arriba y los datos se actualizan."
        )
    elif api_status == "pending":
        st.warning(
            f"⏳ API en proceso de aprobación. Mientras tanto, sube CSV manual."
        )
    else:
        st.info(
            f"ℹ️ API no configurada. Sube CSV manual o configura las "
            f"credenciales para datos en vivo (ver guía de setup abajo)."
        )

    with st.expander(f"📥 Setup API + cómo obtener CSV de {title}", expanded=False):
        st.markdown(instrucciones)

    # Si hay API → fetch en vivo
    if api_status == "configured" and api_fetch_fn is not None:
        if st.button(f"🔄 Actualizar datos de {title}", key=f"refresh_{platform_key}"):
            try:
                with st.spinner(f"Descargando de {title}..."):
                    df = api_fetch_fn(fecha_desde, fecha_hasta)
                    st.session_state["social_data"][platform_key] = df
                    st.success(f"✅ {len(df):,} días descargados.")
            except Exception as exc:  # noqa: BLE001
                st.error(f"Error al descargar de {title}: {exc}")

    # Upload manual (siempre disponible)
    uploaded = st.file_uploader(
        f"O sube CSV manual de {title}",
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
            st.success(
                f"✅ CSV cargado · Formato detectado: `{fmt}` · "
                f"{len(df):,} filas"
            )
        except Exception as exc:  # noqa: BLE001
            st.error(f"No se pudo leer el archivo: {exc}")

    df = st.session_state["social_data"].get(platform_key)
    if df is None or df.empty:
        st.info(
            f"Sube un CSV de {title} para ver los KPIs. "
            "Mira la guía de arriba para saber cómo descargarlo."
        )
        return

    # KPIs del período
    kpis = compute_period_kpis(df, fecha_desde, fecha_hasta)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric(f"{icon} Alcance", f"{int(kpis['alcance']):,}")
    c2.metric("👁️ Impresiones", f"{int(kpis['impresiones']):,}")
    c3.metric("❤️ Engagement", f"{int(kpis['engagement']):,}")
    c4.metric("📈 Engagement rate", f"{kpis['engagement_rate']:.2f}%")

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("👍 Likes", f"{int(kpis['likes']):,}")
    c6.metric("💬 Comentarios", f"{int(kpis['comentarios']):,}")
    c7.metric("🔁 Compartidos", f"{int(kpis['compartidos']):,}")
    c8.metric("📅 Días con datos", f"{kpis['n_dias']:,}")

    # Tendencia diaria
    st.markdown(f"##### 📈 Tendencia diaria de Engagement — {title}")
    daily = compute_daily_aggregation(df, "engagement")
    daily = daily[
        (daily["fecha"] >= fecha_desde) & (daily["fecha"] <= fecha_hasta)
    ] if not daily.empty else daily
    if not daily.empty:
        fig = px.area(
            daily, x="fecha", y="engagement",
            color_discrete_sequence=[color],
            labels={"fecha": "Fecha", "engagement": "Engagement"},
        )
        fig.update_layout(height=300, margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Sin datos en el período seleccionado.")

    # Tabla de detalle
    with st.expander(f"📋 Detalle ({len(df):,} filas)", expanded=False):
        st.dataframe(df, use_container_width=True, hide_index=True, height=300)


# ---------------------------------------------------------------------------
# Facebook
# ---------------------------------------------------------------------------
with tab_fb:
    _platform_tab(
        platform_key="facebook",
        icon="📘",
        title="Facebook",
        color="#1877F2",
        api_status="configured" if is_meta_configured() else "no_configured",
        api_fetch_fn=fetch_meta_facebook_data if is_meta_configured() else None,
        instrucciones="""
**Pasos para descargar el CSV de Facebook:**

1. Entra a [Meta Business Suite](https://business.facebook.com).
2. Selecciona tu Página de Facebook.
3. Menú lateral → **Insights** (Estadísticas).
4. Arriba elige el rango de fechas (recomendado: últimos 90 días).
5. Click el botón **Exportar datos** (arriba a la derecha).
6. Selecciona:
   - Tipo de datos: **Datos de la página** o **Datos de publicaciones**.
   - Formato: **CSV** o **Excel**.
7. Descarga y sube aquí el archivo.

**Métricas que se procesan automáticamente:**
- Impresiones, alcance, engagement, likes, comentarios, compartidos.
- Engagement rate (calculado).

**Nota:** la primera vez puede tardar unos minutos en generar el CSV.
""",
    )


# ---------------------------------------------------------------------------
# Instagram
# ---------------------------------------------------------------------------
with tab_ig:
    _platform_tab(
        platform_key="instagram",
        icon="📸",
        title="Instagram",
        color="#E4405F",
        api_status="configured" if is_meta_configured() else "no_configured",
        api_fetch_fn=fetch_meta_instagram_data if is_meta_configured() else None,
        instrucciones="""
**Pasos para descargar el CSV de Instagram:**

1. Entra a [Meta Business Suite](https://business.facebook.com).
2. Cambia a tu cuenta de Instagram (selector arriba a la izquierda).
3. Menú lateral → **Insights** (Estadísticas).
4. Arriba elige el rango de fechas.
5. Click **Exportar datos** → **CSV** o **Excel**.
6. Sube aquí el archivo.

**Alternativa desde la app móvil de Instagram (más simple, datos limitados):**
- Perfil → Menú (☰) → **Insights** → toca cualquier métrica → swipe arriba para ver más datos.
- (No hay export directo desde móvil; usa Meta Business Suite en web.)

**Métricas que se procesan:**
- Reach, impressions, likes, comments, saves, shares, profile visits.
""",
    )


# ---------------------------------------------------------------------------
# TikTok
# ---------------------------------------------------------------------------
with tab_tt:
    _platform_tab(
        platform_key="tiktok",
        icon="🎵",
        title="TikTok",
        color="#FE2C55",
        api_status="pending" if is_tiktok_configured() else "no_configured",
        api_fetch_fn=None,  # esqueleto pendiente
        instrucciones="""
**Pasos para descargar el CSV de TikTok:**

1. Entra a [TikTok Studio](https://studio.tiktok.com) o [TikTok Business](https://business.tiktok.com).
2. Asegúrate de tener una cuenta **Business** o **Creator** (las personales no exportan datos).
3. Menú → **Analytics** (Análisis).
4. Arriba elige el rango de fechas (máximo 60 días en algunos planes).
5. Hay tres pestañas para descargar:
   - **Visión general** (Overview): vistas de perfil, seguidores, engagement total.
   - **Contenido** (Content): por video — vistas, likes, comments, shares.
   - **Seguidores** (Followers): demografía, crecimiento.
6. Click el botón **Descargar datos** (icono de descarga arriba a la derecha).
7. Selecciona **CSV** y descarga.
8. Sube aquí el archivo.

**Métricas que se procesan:**
- Video views, likes, comments, shares, profile visits, seguidores ganados.
""",
    )


# ---------------------------------------------------------------------------
# Google Analytics 4
# ---------------------------------------------------------------------------
with tab_ga:
    _platform_tab(
        platform_key="ga4",
        icon="📊",
        title="Google Analytics",
        color="#F9AB00",
        api_status="configured" if is_ga4_configured() else "no_configured",
        api_fetch_fn=fetch_ga4_data if is_ga4_configured() else None,
        instrucciones="""
**Pasos para descargar el CSV de Google Analytics 4:**

1. Entra a [analytics.google.com](https://analytics.google.com).
2. Selecciona tu propiedad (la cuenta de tu sitio web).
3. Menú lateral → **Informes** → **Adquisición** (o el reporte que prefieras).
4. Arriba ajusta el rango de fechas.
5. Click el icono de **Compartir / Descargar** (esquina superior derecha) → **Descargar archivo** → **CSV**.
6. Sube aquí el archivo.

**Reportes recomendados para empezar:**
- **Adquisición → Visión general del tráfico**: sesiones, usuarios por canal.
- **Engagement → Páginas y pantallas**: páginas más vistas.
- **Monetización → Visión general**: si tienes ecommerce, ingresos y conversiones.

**Métricas que se procesan:**
- Sesiones, usuarios, páginas vistas, tasa de rebote, conversiones, ingresos.

**Más adelante:** podemos conectar la API de GA4 directamente para no tener que exportar manualmente.
""",
    )


# ---------------------------------------------------------------------------
# Comparativo entre plataformas
# ---------------------------------------------------------------------------
with tab_cmp:
    st.markdown("### 🆚 Comparativo entre plataformas")
    st.caption(
        "Métricas combinadas de las plataformas que tengan datos cargados. "
        "Sube CSVs en cada pestaña primero."
    )

    cargadas = [
        (k, v) for k, v in st.session_state["social_data"].items()
        if v is not None and not v.empty
    ]
    if not cargadas:
        st.info(
            "Aún no has cargado ningún CSV. Ve a las pestañas de cada "
            "plataforma y sube los datos."
        )
    else:
        # KPIs por plataforma
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

        # Gráfica comparativa de engagement
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
                height=380, margin=dict(l=0, r=0, t=10, b=0),
                showlegend=False,
            )
            st.plotly_chart(fig, use_container_width=True)

        # Tendencia diaria comparativa
        st.markdown("##### 📈 Tendencia diaria comparativa de Engagement")
        all_daily = []
        for plat, df in cargadas:
            d = compute_daily_aggregation(df, "engagement")
            d = d[
                (d["fecha"] >= fecha_desde) & (d["fecha"] <= fecha_hasta)
            ] if not d.empty else d
            if not d.empty:
                d = d.copy()
                d["plataforma"] = plat.title()
                all_daily.append(d)
        if all_daily:
            daily_combined = pd.concat(all_daily, ignore_index=True)
            fig_d = px.line(
                daily_combined, x="fecha", y="engagement", color="plataforma",
                color_discrete_map={
                    "Facebook": "#1877F2", "Instagram": "#E4405F",
                    "Tiktok": "#FE2C55", "Ga4": "#F9AB00",
                },
                markers=True,
            )
            fig_d.update_layout(height=380, margin=dict(l=0, r=0, t=10, b=0))
            st.plotly_chart(fig_d, use_container_width=True)
