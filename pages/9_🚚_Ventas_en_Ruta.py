# -*- coding: utf-8 -*-
"""
Página: Ventas en Ruta — Vendedores externos.

Análisis enfocado a los vendedores que visitan clientes en territorio
(por defecto: Luis Felipe Hurtado, Yarley Vanessa). Métricas que un
gerente comercial necesita para coachear al equipo de calle:

  - KPIs de ventas (igual que el informe general pero filtrado).
  - Cobertura: % de clientes asignados atendidos en el período.
  - Frecuencia de visita por cliente.
  - Análisis por ciudad / departamento.
  - Mapa de georeferencia con puntos de clientes.
  - Zonificación automática (clusters geográficos).
  - Oportunidades: clientes inactivos y en caída de ventas.

Anclado a `invoice_date`. SOAT/ANTCL excluidos.
"""
from __future__ import annotations

import io
from datetime import date, timedelta

import numpy as np
import pandas as pd
import plotly.express as px
import streamlit as st

from src.auth import logout_button, require_auth
from src.data_loader import compute_full_analysis, load_invoice_lines
from src.route_sales import (
    build_geo_dataframe,
    compute_coverage_kpis,
    compute_sales_by_city,
    compute_visit_frequency,
    detect_opportunities,
    get_assigned_partners,
    get_partners_by_team,
    get_team_sellers,
    zonify_partners,
)
from src.sales_analyzer import (
    EXCLUDED_SALES_DEFAULT_CODES,
    compute_sales_growth_from_lines,
    compute_sales_kpis_from_lines,
)
from src.ui_components import render_company_context, render_sidebar_filters

st.set_page_config(
    page_title="Ventas en Ruta | Cartera",
    page_icon="🚚",
    layout="wide",
)

require_auth()
logout_button()

st.title("🚚 Ventas en Ruta")
st.caption(
    "Reporte enfocado a vendedores externos (los que visitan clientes en "
    "territorio). Métricas de cobertura, frecuencia de visita, análisis "
    "geográfico y oportunidades de venta. **Visita = factura emitida** "
    "(no manejamos `crm.lead.activity`). **Subtotal sin IVA**, excluyendo "
    "recaudos a terceros: " + ", ".join(f"`{c}`" for c in EXCLUDED_SALES_DEFAULT_CODES)
    + "."
)

filters = render_sidebar_filters()
if filters["company_ids"] is not None and len(filters["company_ids"]) == 0:
    st.warning("Selecciona al menos una empresa en el sidebar.")
    st.stop()

# ---------------------------------------------------------------------------
# Cargar datos
# ---------------------------------------------------------------------------
data = compute_full_analysis(
    months_back=filters["months_back"],
    rotation_period_days=filters["period_days"],
    company_ids=filters["company_ids"],
    exclude_cash_sales=filters["exclude_cash_sales"],
    analysis_window_days=filters.get("analysis_window_days"),
)
render_company_context(data.get("companies"), filters["company_ids"])

partners_all = data.get("raw_partners")
if partners_all is None or partners_all.empty:
    st.error("No se pudieron cargar los clientes. Revisa la conexión con Odoo.")
    st.stop()

try:
    invoice_lines_all = load_invoice_lines(
        months_back=filters["months_back"],
        company_ids=filters["company_ids"],
    )
except Exception as exc:  # noqa: BLE001
    st.error(f"No se pudieron cargar las líneas de factura: {exc}")
    st.stop()

# ---------------------------------------------------------------------------
# Selección de equipo de ventas
# ---------------------------------------------------------------------------
DEFAULT_TEAM = "Lubricantes"

st.markdown("### 🏷️ Equipo de ventas")

# Lista de equipos disponibles (de los partners cargados)
if "team_name" in partners_all.columns:
    team_options = sorted(
        partners_all["team_name"]
        .dropna().astype(str).str.strip().replace("", pd.NA).dropna()
        .unique().tolist()
    )
else:
    team_options = []

if not team_options:
    st.error(
        "Ningún cliente tiene equipo de ventas asignado en Odoo "
        "(`res.partner.team_id`). Asigna los clientes a un equipo (ej. "
        "'Lubricantes') desde Contactos en Odoo o usa el filtro "
        "manual de vendedores más abajo."
    )
    st.stop()

# Default: Lubricantes (si existe)
default_team_idx = 0
for i, t in enumerate(team_options):
    if DEFAULT_TEAM.lower() in t.lower():
        default_team_idx = i
        break

selected_team = st.selectbox(
    "Equipo a analizar",
    options=team_options,
    index=default_team_idx,
    help=(
        "Filtra por `crm.team` (equipo de ventas asignado al cliente "
        "en Odoo). Por defecto: Lubricantes — donde están Luis Felipe "
        "Hurtado y Yarley Vanessa."
    ),
)

# Clientes del equipo
assigned_partners = get_partners_by_team(partners_all, selected_team)
if assigned_partners.empty:
    st.warning(
        f"Ningún cliente está asignado al equipo '{selected_team}' "
        f"(`res.partner.team_id`). Revisa la asignación en Odoo."
    )
    st.stop()

# Vendedores del equipo (derivados de los partners asignados)
team_sellers = get_team_sellers(partners_all, selected_team)
seller_names_str = ", ".join(team_sellers.values()) if team_sellers else "—"
selected_user_ids = list(team_sellers.keys())
selected_names = list(team_sellers.values())

st.caption(
    f"📋 **{len(assigned_partners):,}** clientes asignados al equipo "
    f"**{selected_team}** · vendedores: **{seller_names_str}**"
)

# ---------------------------------------------------------------------------
# Período del informe
# ---------------------------------------------------------------------------
st.markdown("### 🗓️ Período")
col_p1, col_p2, col_p3 = st.columns([1, 1, 2])

cutoff = data["cutoff_date"]
default_to = pd.Timestamp(cutoff).date() if cutoff else date.today()
default_from = default_to.replace(day=1)

with col_p1:
    fecha_desde = st.date_input(
        "Desde", value=default_from, max_value=default_to, key="ruta_desde",
    )
with col_p2:
    fecha_hasta = st.date_input(
        "Hasta", value=default_to, max_value=default_to, key="ruta_hasta",
    )
with col_p3:
    quick = st.radio(
        "Atajos",
        options=["Personalizado", "Mes actual", "Mes anterior",
                 "Trimestre actual", "Año a la fecha", "Últimos 30 días", "Últimos 90 días"],
        index=1, horizontal=False, key="ruta_atajo",
    )

def _resolve_quick(today: date, choice: str):
    if choice == "Mes actual":
        return today.replace(day=1), today
    if choice == "Mes anterior":
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        return last_prev.replace(day=1), last_prev
    if choice == "Trimestre actual":
        q_first_month = ((today.month - 1) // 3) * 3 + 1
        return today.replace(month=q_first_month, day=1), today
    if choice == "Año a la fecha":
        return today.replace(month=1, day=1), today
    if choice == "Últimos 30 días":
        return today - timedelta(days=30), today
    if choice == "Últimos 90 días":
        return today - timedelta(days=90), today
    return None, None

if quick != "Personalizado":
    f, t = _resolve_quick(default_to, quick)
    if f and t:
        fecha_desde, fecha_hasta = f, t

if fecha_desde > fecha_hasta:
    st.error("La fecha 'Desde' debe ser anterior o igual a 'Hasta'.")
    st.stop()

st.caption(
    f"Período: **{fecha_desde.strftime('%d %b %Y')}** → "
    f"**{fecha_hasta.strftime('%d %b %Y')}** "
    f"({(fecha_hasta - fecha_desde).days + 1} días)"
)

# ---------------------------------------------------------------------------
# Filtrar invoice_lines a clientes del equipo
# ---------------------------------------------------------------------------
asig_ids = set(assigned_partners["id"].astype(int).tolist())
lines_team = invoice_lines_all[
    invoice_lines_all["partner_id"].isin(asig_ids)
].copy() if not invoice_lines_all.empty else invoice_lines_all

# ---------------------------------------------------------------------------
# KPIs de ventas + comparativo
# ---------------------------------------------------------------------------
st.markdown("### 📊 KPIs de ventas")
growth = compute_sales_growth_from_lines(
    invoice_lines=lines_team,
    date_from=fecha_desde, date_to=fecha_hasta,
    company_ids=filters["company_ids"],
)
kpis = growth["actual"]
kpis_prev = growth["anterior"]

def _fmt_money(v): return f"${v:,.0f}"
def _fmt_pct(v):
    if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))):
        return "—"
    return f"{v:+.1f}%"

c1, c2, c3, c4 = st.columns(4)
c1.metric("💰 Ventas netas", _fmt_money(kpis.ventas_netas),
          _fmt_pct(growth["var_ventas_pct"]),
          help=f"Anterior: {_fmt_money(kpis_prev.ventas_netas)}")
c2.metric("🧾 # Facturas", f"{kpis.n_facturas:,}",
          _fmt_pct(growth["var_facturas_pct"]),
          help=f"Anterior: {kpis_prev.n_facturas:,}")
c3.metric("🎫 Ticket promedio", _fmt_money(kpis.ticket_promedio),
          _fmt_pct(growth["var_ticket_pct"]))
c4.metric("👥 Clientes atendidos", f"{kpis.n_clientes_unicos:,}",
          help="Clientes únicos con factura en el período.")

# ---------------------------------------------------------------------------
# Cobertura del territorio
# ---------------------------------------------------------------------------
st.markdown("### 🎯 Cobertura del territorio")
cov = compute_coverage_kpis(
    invoice_lines=lines_team,
    assigned_partners=assigned_partners,
    date_from=fecha_desde, date_to=fecha_hasta,
    company_ids=filters["company_ids"],
)

cov_c1, cov_c2, cov_c3, cov_c4 = st.columns(4)
cov_c1.metric(
    "Clientes asignados", f"{cov['n_clientes_asignados']:,}",
    help="Total de clientes en la base del equipo (`res.partner.user_id`)."
)
cov_c2.metric(
    "Cobertura del período",
    f"{cov['cobertura_pct']:.1f}%",
    help=f"{cov['n_clientes_atendidos']:,} de {cov['n_clientes_asignados']:,} clientes recibieron al menos 1 factura.",
)
cov_c3.metric(
    "🆕 Clientes nuevos", f"{cov['n_clientes_nuevos']:,}",
    help="Clientes cuya PRIMERA factura está en el período (clientes ganados).",
)
cov_c4.metric(
    "😴 Inactivos > 60 días", f"{cov['n_clientes_inactivos_60d']:,}",
    help=(
        f"Clientes asignados que NO compran hace más de 60 días.\n\n"
        f"30d: {cov['n_clientes_inactivos_30d']:,} · "
        f"90d: {cov['n_clientes_inactivos_90d']:,} · "
        f"jamás: {cov['n_clientes_jamas_comprado']:,}"
    ),
    delta_color="inverse",
)

# ---------------------------------------------------------------------------
# Frecuencia de visita
# ---------------------------------------------------------------------------
st.markdown("### 🔁 Frecuencia de visita")
freq = compute_visit_frequency(
    invoice_lines=lines_team,
    assigned_partners=assigned_partners,
    date_from=fecha_desde, date_to=fecha_hasta,
    company_ids=filters["company_ids"],
)

if freq.empty:
    st.info("No hay visitas registradas en el período.")
else:
    f_c1, f_c2, f_c3 = st.columns(3)
    f_c1.metric(
        "Visitas totales", f"{int(freq['num_visitas'].sum()):,}",
        help="Suma de facturas emitidas a clientes del equipo en el período.",
    )
    f_c2.metric(
        "Visitas por cliente prom.", f"{freq['num_visitas'].mean():.1f}",
        help="Promedio de facturas por cliente atendido.",
    )
    media_dias = freq["dias_entre_visitas_prom"].dropna().mean()
    f_c3.metric(
        "Días entre visitas prom.",
        f"{media_dias:.0f}" if pd.notna(media_dias) else "—",
        help=(
            "Promedio de días entre facturas consecutivas para clientes con "
            "más de 1 visita en el período."
        ),
    )
    st.dataframe(
        freq.rename(columns={
            "partner_name": "Cliente",
            "num_visitas": "# Visitas",
            "dias_entre_visitas_prom": "Días entre visitas",
            "ultima_visita": "Última visita",
            "dias_desde_ultima": "Días desde última",
            "ventas_periodo": "Ventas período",
        })[[
            "Cliente", "# Visitas", "Días entre visitas",
            "Última visita", "Días desde última", "Ventas período",
        ]],
        column_config={
            "Ventas período": st.column_config.NumberColumn(format="$ %,.0f"),
            "Días entre visitas": st.column_config.NumberColumn(format="%.1f"),
            "Días desde última": st.column_config.NumberColumn(format="%.0f"),
            "Última visita": st.column_config.DateColumn(format="DD/MM/YYYY"),
        },
        use_container_width=True, hide_index=True, height=320,
    )

# ---------------------------------------------------------------------------
# Análisis por ciudad
# ---------------------------------------------------------------------------
st.markdown("### 🏙️ Análisis por ciudad")
by_city = compute_sales_by_city(
    invoice_lines=lines_team,
    assigned_partners=assigned_partners,
    date_from=fecha_desde, date_to=fecha_hasta,
    company_ids=filters["company_ids"],
)
if by_city.empty:
    st.info("No hay datos por ciudad en el período.")
else:
    col_g, col_t = st.columns([2, 3])
    with col_g:
        fig_c = px.bar(
            by_city.head(15),
            x="ventas_netas", y="city", orientation="h",
            color="ventas_netas", color_continuous_scale="Blues",
            labels={"city": "Ciudad", "ventas_netas": "Ventas netas $"},
        )
        fig_c.update_layout(
            height=420, margin=dict(l=0, r=0, t=10, b=0),
            yaxis=dict(autorange="reversed"), coloraxis_showscale=False,
        )
        st.plotly_chart(fig_c, use_container_width=True)
    with col_t:
        rename_map = {
            "city": "Ciudad", "state_name": "Departamento",
            "n_clientes": "# Clientes", "n_facturas": "# Fact.",
            "ventas_netas": "Ventas netas", "ticket_promedio": "Ticket prom.",
            "participacion_pct": "% del total",
        }
        col_show = [c for c in rename_map if c in by_city.columns]
        st.dataframe(
            by_city[col_show].rename(columns=rename_map),
            column_config={
                "Ventas netas": st.column_config.NumberColumn(format="$ %,.0f"),
                "Ticket prom.": st.column_config.NumberColumn(format="$ %,.0f"),
                "% del total": st.column_config.NumberColumn(format="%.1f%%"),
            },
            use_container_width=True, hide_index=True, height=420,
        )

# ---------------------------------------------------------------------------
# Mapa de georeferencia + Zonificación
# ---------------------------------------------------------------------------
st.markdown("### 🗺️ Mapa de clientes y zonificación")
geo_df = build_geo_dataframe(
    assigned_partners=assigned_partners,
    invoice_lines=lines_team,
    date_from=fecha_desde, date_to=fecha_hasta,
    company_ids=filters["company_ids"],
)

if geo_df.empty:
    st.warning(
        "Ningún cliente asignado tiene coordenadas GPS válidas en Odoo "
        "(`partner_latitude` / `partner_longitude`). Para activar el mapa, "
        "geolocaliza los clientes en Odoo (Contactos → acción Geolocalizar)."
    )
else:
    map_c1, map_c2 = st.columns([1, 3])
    with map_c1:
        n_zones = st.slider("Número de zonas", 2, 10, 5, key="ruta_n_zonas")
        modo_color = st.radio(
            "Colorear por", ["Zona", "Atendido / No atendido", "Ventas"],
            index=0, key="ruta_modo_color",
        )
    geo_df = zonify_partners(geo_df, n_zones=n_zones)

    with map_c2:
        if modo_color == "Zona" and "zona" in geo_df.columns:
            color_col = "zona"
        elif modo_color == "Atendido / No atendido":
            geo_df["estado"] = geo_df["es_atendido"].map(
                {True: "✅ Atendido", False: "❌ Sin atender"}
            )
            color_col = "estado"
        else:
            color_col = "ventas_periodo"

        # Tamaño del punto: ventas (con piso para los $0)
        size_col = "ventas_periodo"
        geo_df["_size"] = geo_df[size_col].clip(lower=0).fillna(0)
        # Mínimo visual
        max_v = float(geo_df["_size"].max() or 1)
        geo_df["_size"] = geo_df["_size"].apply(lambda v: max(v, max_v * 0.05))

        fig_map = px.scatter_mapbox(
            geo_df, lat="lat", lon="lon",
            color=color_col,
            size="_size",
            size_max=25,
            hover_name="partner_name",
            hover_data={
                "city": True if "city" in geo_df.columns else False,
                "ventas_periodo": ":,.0f",
                "num_visitas": True,
                "lat": False, "lon": False, "_size": False,
            },
            zoom=6,
            height=550,
            color_continuous_scale="Viridis" if color_col == "ventas_periodo" else None,
        )
        fig_map.update_layout(
            mapbox_style="open-street-map",
            margin=dict(l=0, r=0, t=0, b=0),
        )
        st.plotly_chart(fig_map, use_container_width=True)

    # Resumen por zona
    if "zona" in geo_df.columns:
        st.markdown("##### 📍 Resumen por zona")
        zona_stats = (
            geo_df.groupby("zona")
            .agg(
                n_clientes=("partner_id", "count"),
                n_atendidos=("es_atendido", "sum"),
                ventas=("ventas_periodo", "sum"),
                visitas=("num_visitas", "sum"),
                lat_centro=("lat", "mean"),
                lon_centro=("lon", "mean"),
            )
            .reset_index()
        )
        zona_stats["cobertura_pct"] = (
            zona_stats["n_atendidos"] / zona_stats["n_clientes"] * 100
        ).round(1)
        zona_stats["ticket_prom"] = (
            zona_stats["ventas"] / zona_stats["visitas"].replace(0, np.nan)
        ).fillna(0)
        st.dataframe(
            zona_stats[[
                "zona", "n_clientes", "n_atendidos", "cobertura_pct",
                "ventas", "visitas", "ticket_prom",
            ]].rename(columns={
                "zona": "Zona", "n_clientes": "# Clientes",
                "n_atendidos": "# Atendidos", "cobertura_pct": "Cobertura %",
                "ventas": "Ventas período", "visitas": "Visitas",
                "ticket_prom": "Ticket prom.",
            }),
            column_config={
                "Ventas período": st.column_config.NumberColumn(format="$ %,.0f"),
                "Ticket prom.": st.column_config.NumberColumn(format="$ %,.0f"),
                "Cobertura %": st.column_config.NumberColumn(format="%.1f%%"),
            },
            use_container_width=True, hide_index=True,
        )

# ---------------------------------------------------------------------------
# Oportunidades de venta
# ---------------------------------------------------------------------------
st.markdown("### 🎯 Oportunidades de venta")
op_c1, op_c2 = st.columns(2)
with op_c1:
    inact_days = st.slider(
        "Umbral inactividad (días)", 30, 180, 60, step=15, key="ruta_inact_days",
    )
with op_c2:
    drop_pct = st.slider(
        "Caída de ventas (%) — alerta", 10, 80, 30, step=5, key="ruta_drop_pct",
    )

opps = detect_opportunities(
    invoice_lines=lines_team,
    assigned_partners=assigned_partners,
    cutoff=fecha_hasta,
    company_ids=filters["company_ids"],
    inactivity_threshold_days=inact_days,
    drop_threshold_pct=float(drop_pct),
)

tab_inact, tab_caida = st.tabs(["😴 Clientes inactivos", "📉 Clientes en caída"])

with tab_inact:
    inactivos = opps["inactivos"]
    if inactivos is None or inactivos.empty:
        st.success(f"✅ No hay clientes inactivos por más de {inact_days} días.")
    else:
        st.caption(
            f"{len(inactivos):,} clientes asignados al equipo NO compran hace "
            f"más de **{inact_days} días**. Ordenados por valor histórico (los "
            "más valiosos primero — son los que más urge reactivar)."
        )
        cols = [c for c in [
            "partner_name", "city", "ultima_factura", "dias_desde_ultima",
            "ventas_historicas",
        ] if c in inactivos.columns]
        rename = {
            "partner_name": "Cliente", "city": "Ciudad",
            "ultima_factura": "Última factura",
            "dias_desde_ultima": "Días sin comprar",
            "ventas_historicas": "Ventas históricas",
        }
        st.dataframe(
            inactivos[cols].rename(columns=rename),
            column_config={
                "Ventas históricas": st.column_config.NumberColumn(format="$ %,.0f"),
                "Días sin comprar": st.column_config.NumberColumn(format="%.0f"),
                "Última factura": st.column_config.DateColumn(format="DD/MM/YYYY"),
            },
            use_container_width=True, hide_index=True, height=400,
        )

with tab_caida:
    caida = opps["en_caida"]
    if caida is None or caida.empty:
        st.success(
            f"✅ Ningún cliente tiene caída de ventas mayor a {drop_pct}% "
            "vs el mes anterior."
        )
    else:
        st.caption(
            f"{len(caida):,} clientes con caída de ventas mayor a **{drop_pct}%** "
            "vs mes anterior. Los primeros son los que más bajaron en pesos."
        )
        cols = [c for c in [
            "partner_name", "city", "ventas_anterior",
            "ventas_actual", "var_pct", "caida_abs",
        ] if c in caida.columns]
        rename = {
            "partner_name": "Cliente", "city": "Ciudad",
            "ventas_anterior": "Mes anterior",
            "ventas_actual": "Mes actual",
            "var_pct": "Variación %",
            "caida_abs": "Caída ($)",
        }
        st.dataframe(
            caida[cols].rename(columns=rename),
            column_config={
                "Mes anterior": st.column_config.NumberColumn(format="$ %,.0f"),
                "Mes actual": st.column_config.NumberColumn(format="$ %,.0f"),
                "Caída ($)": st.column_config.NumberColumn(format="$ %,.0f"),
                "Variación %": st.column_config.NumberColumn(format="%+.1f%%"),
            },
            use_container_width=True, hide_index=True, height=400,
        )

# ---------------------------------------------------------------------------
# Export Excel
# ---------------------------------------------------------------------------
st.markdown("---")
buf = io.BytesIO()
with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
    pd.DataFrame([{
        "Vendedores": ", ".join(selected_names),
        "Período desde": fecha_desde,
        "Período hasta": fecha_hasta,
        "Ventas netas": kpis.ventas_netas,
        "Clientes asignados": cov["n_clientes_asignados"],
        "Clientes atendidos": cov["n_clientes_atendidos"],
        "Cobertura %": cov["cobertura_pct"],
        "Clientes nuevos": cov["n_clientes_nuevos"],
        "Inactivos > 60d": cov["n_clientes_inactivos_60d"],
    }]).to_excel(writer, sheet_name="Resumen", index=False)
    if not freq.empty:
        freq.to_excel(writer, sheet_name="Frecuencia visita", index=False)
    if not by_city.empty:
        by_city.to_excel(writer, sheet_name="Por ciudad", index=False)
    if not geo_df.empty:
        geo_df.drop(columns=[c for c in ["_size"] if c in geo_df.columns])\
              .to_excel(writer, sheet_name="Mapa clientes", index=False)
    if not opps["inactivos"].empty:
        opps["inactivos"].to_excel(writer, sheet_name="Inactivos", index=False)
    if not opps["en_caida"].empty:
        opps["en_caida"].to_excel(writer, sheet_name="En caida", index=False)

st.download_button(
    "⬇️ Descargar Excel — Ventas en Ruta",
    data=buf.getvalue(),
    file_name=f"ventas_ruta_{fecha_desde}_{fecha_hasta}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)
