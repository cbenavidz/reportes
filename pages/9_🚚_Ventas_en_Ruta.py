# -*- coding: utf-8 -*-
"""
Página: Ventas en Ruta — vendedores externos.

Versión simplificada que NO depende de `crm.team`. Selección manual de
vendedores. Funciona con los datos disponibles en Odoo:
  - `account.move.line` (ventas, subtotal sin IVA, excluye SOAT/ANTCL).
  - `res.partner.user_id`, `city`, `partner_latitude`, `partner_longitude`.

Métricas: KPIs de ventas, evolución mensual de clientes atendidos,
análisis por ciudad, mapa GPS, frecuencia de visita, top clientes,
clientes inactivos.
"""
from __future__ import annotations

import io
from datetime import date, timedelta

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from src.auth import logout_button, require_auth
from src.data_loader import compute_full_analysis, load_invoice_lines
from src.route_sales import (
    build_geo_dataframe,
    compute_monthly_clients_kpi,
    compute_sales_by_city,
    compute_visit_frequency,
    detect_inactive_clients,
    get_partners_for_sellers,
)
from src.sales_analyzer import (
    EXCLUDED_SALES_DEFAULT_CODES,
    compute_sales_growth_from_lines,
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
    "Análisis para vendedores externos. **Subtotal sin IVA**, excluyendo "
    "recaudos a terceros: " + ", ".join(f"`{c}`" for c in EXCLUDED_SALES_DEFAULT_CODES)
    + ". Métricas: cobertura mensual, ventas por ciudad, mapa GPS, "
    "frecuencia de visita y clientes inactivos."
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
invoices_all = data.get("raw_invoices")
if partners_all is None or partners_all.empty:
    st.error("No se pudieron cargar los clientes.")
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
# Selección de vendedores externos
# ---------------------------------------------------------------------------
st.markdown("### 👤 Vendedores")

# Construir lista de vendedores desde facturas (más fiel) o partners (fallback)
seller_df = pd.DataFrame()
if (
    invoices_all is not None and not invoices_all.empty
    and "invoice_user_id" in invoices_all.columns
    and "invoice_user_id_name" in invoices_all.columns
):
    seller_df = (
        invoices_all[["invoice_user_id", "invoice_user_id_name"]]
        .dropna(subset=["invoice_user_id"])
        .drop_duplicates("invoice_user_id")
        .rename(columns={
            "invoice_user_id": "user_id",
            "invoice_user_id_name": "user_name",
        })
    )
if seller_df.empty and "user_id" in partners_all.columns:
    seller_df = (
        partners_all[["user_id", "user_name"]]
        .dropna(subset=["user_id"])
        .drop_duplicates("user_id")
    )
if seller_df.empty:
    st.error("No se pudo detectar ningún vendedor en facturas ni en clientes.")
    st.stop()
seller_df["user_id"] = seller_df["user_id"].astype(int)
seller_df = seller_df.sort_values("user_name").reset_index(drop=True)

seller_options = seller_df["user_name"].astype(str).tolist()
DEFAULTS = ["Yarley Vanessa", "Yarley", "Luis Felipe Hurtado", "Luis Felipe"]
default_names = [
    n for n in seller_options
    if any(d.lower() in n.lower() for d in DEFAULTS)
]

selected_names = st.multiselect(
    "Selecciona vendedores externos",
    options=seller_options,
    default=default_names if default_names else seller_options[:2],
    help=(
        "Por defecto: Yarley Vanessa y Luis Felipe Hurtado (vendedores "
        "externos del equipo Lubricantes). Puedes cambiar si tu equipo "
        "crece o si quieres analizar a otro vendedor."
    ),
)
if not selected_names:
    st.warning("Selecciona al menos un vendedor.")
    st.stop()
selected_user_ids = (
    seller_df.loc[seller_df["user_name"].isin(selected_names), "user_id"]
    .astype(int).tolist()
)

# Clientes asignados (por user_id en partner o por invoice_user_id en factura)
assigned_partners = get_partners_for_sellers(
    partners_all, invoices_all, selected_user_ids,
)
if assigned_partners.empty:
    st.warning(
        "Los vendedores seleccionados no tienen clientes asignados ni "
        "han emitido facturas. Revisa la selección."
    )
    st.stop()
asig_ids = set(assigned_partners["id"].astype(int).tolist())

st.caption(
    f"📋 **{len(assigned_partners):,}** clientes vinculados a "
    f"**{', '.join(selected_names)}** (asignación o facturación)."
)

# Filtrar invoice_lines a esos clientes
lines_team = invoice_lines_all[
    invoice_lines_all["partner_id"].isin(asig_ids)
].copy() if not invoice_lines_all.empty else invoice_lines_all

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
# KPIs principales
# ---------------------------------------------------------------------------
st.markdown("### 📊 KPIs")

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
          _fmt_pct(growth["var_facturas_pct"]))
c3.metric("👥 Clientes atendidos", f"{kpis.n_clientes_unicos:,}",
          help="Clientes únicos con factura en el período.")
c4.metric("🎫 Ticket promedio", _fmt_money(kpis.ticket_promedio),
          _fmt_pct(growth["var_ticket_pct"]))

cobertura = (
    kpis.n_clientes_unicos / len(assigned_partners) * 100
    if assigned_partners.shape[0] > 0 else 0
)
st.caption(
    f"📊 Cobertura: **{cobertura:.1f}%** "
    f"({kpis.n_clientes_unicos:,} de {len(assigned_partners):,} clientes "
    "vinculados al equipo recibieron al menos 1 factura en el período)."
)

# ---------------------------------------------------------------------------
# Evolución mensual de clientes atendidos
# ---------------------------------------------------------------------------
st.markdown("### 📈 Numérica mensual: clientes atendidos y ventas")
months_show = st.slider(
    "Meses a mostrar", 3, 24, 12, key="ruta_meses",
    help="Histórico de clientes atendidos mes a mes.",
)
monthly = compute_monthly_clients_kpi(
    invoice_lines=lines_team,
    months=months_show,
    cutoff_date=fecha_hasta,
    company_ids=filters["company_ids"],
)
if monthly.empty:
    st.info("Sin datos en el rango.")
else:
    fig_m = go.Figure()
    fig_m.add_trace(go.Bar(
        x=monthly["mes_label"], y=monthly["n_clientes_atendidos"],
        name="Clientes atendidos", marker_color="#3b82f6", yaxis="y",
        hovertemplate="<b>%{x}</b><br>%{y} clientes<extra></extra>",
    ))
    fig_m.add_trace(go.Scatter(
        x=monthly["mes_label"], y=monthly["ventas_netas"],
        name="Ventas netas $", mode="lines+markers",
        line=dict(color="#10b981", width=3), marker=dict(size=8),
        yaxis="y2",
        hovertemplate="<b>%{x}</b><br>$%{y:,.0f}<extra></extra>",
    ))
    fig_m.update_layout(
        height=420, margin=dict(l=0, r=0, t=10, b=0),
        yaxis=dict(title="# Clientes", side="left"),
        yaxis2=dict(title="Ventas $", overlaying="y", side="right",
                    showgrid=False),
        legend=dict(orientation="h", y=-0.2),
        hovermode="x unified",
    )
    st.plotly_chart(fig_m, use_container_width=True)

    st.dataframe(
        monthly[["mes_label", "n_clientes_atendidos", "n_facturas",
                 "ventas_netas", "ticket_promedio"]].rename(columns={
            "mes_label": "Mes",
            "n_clientes_atendidos": "Clientes atendidos",
            "n_facturas": "# Facturas",
            "ventas_netas": "Ventas netas",
            "ticket_promedio": "Ticket prom.",
        }),
        column_config={
            "Ventas netas": st.column_config.NumberColumn(format="$ %,.0f"),
            "Ticket prom.": st.column_config.NumberColumn(format="$ %,.0f"),
        },
        use_container_width=True, hide_index=True,
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
    st.info("Sin datos por ciudad en el período.")
else:
    cg, ct = st.columns([2, 3])
    with cg:
        fig_c = px.bar(
            by_city.head(15), x="ventas_netas", y="city", orientation="h",
            color="ventas_netas", color_continuous_scale="Blues",
            labels={"city": "Ciudad", "ventas_netas": "Ventas $"},
        )
        fig_c.update_layout(
            height=420, margin=dict(l=0, r=0, t=10, b=0),
            yaxis=dict(autorange="reversed"), coloraxis_showscale=False,
        )
        st.plotly_chart(fig_c, use_container_width=True)
    with ct:
        rename = {
            "city": "Ciudad", "state_name": "Departamento",
            "n_clientes": "# Clientes", "n_facturas": "# Fact.",
            "ventas_netas": "Ventas netas", "ticket_promedio": "Ticket prom.",
            "participacion_pct": "% del total",
        }
        st.dataframe(
            by_city[[c for c in rename if c in by_city.columns]].rename(columns=rename),
            column_config={
                "Ventas netas": st.column_config.NumberColumn(format="$ %,.0f"),
                "Ticket prom.": st.column_config.NumberColumn(format="$ %,.0f"),
                "% del total": st.column_config.NumberColumn(format="%.1f%%"),
            },
            use_container_width=True, hide_index=True, height=420,
        )

# ---------------------------------------------------------------------------
# Mapa de georeferencia
# ---------------------------------------------------------------------------
st.markdown("### 🗺️ Mapa de clientes")
geo_df = build_geo_dataframe(
    assigned_partners=assigned_partners,
    invoice_lines=lines_team,
    date_from=fecha_desde, date_to=fecha_hasta,
    company_ids=filters["company_ids"],
)
if geo_df.empty:
    st.warning(
        "Ningún cliente del equipo tiene coordenadas GPS válidas "
        "(`partner_latitude` / `partner_longitude` vacías o en cero). "
        "En Odoo: Contactos → seleccionar clientes → Acción → "
        "Geolocalizar partners."
    )
else:
    map_modo = st.radio(
        "Colorear por", ["Ventas", "Atendido / No atendido"],
        index=0, horizontal=True, key="mapa_modo",
    )
    if map_modo == "Atendido / No atendido":
        geo_df["estado"] = geo_df["es_atendido"].map(
            {True: "✅ Atendido", False: "❌ Sin atender"}
        )
        color_col = "estado"
        color_map = None
    else:
        color_col = "ventas_periodo"
        color_map = "Viridis"

    geo_df["_size"] = geo_df["ventas_periodo"].clip(lower=0).fillna(0)
    max_v = float(geo_df["_size"].max() or 1)
    geo_df["_size"] = geo_df["_size"].apply(lambda v: max(v, max_v * 0.05))

    fig_map = px.scatter_mapbox(
        geo_df, lat="lat", lon="lon",
        color=color_col, size="_size", size_max=22,
        hover_name="partner_name",
        hover_data={
            "city": True if "city" in geo_df.columns else False,
            "ventas_periodo": ":,.0f",
            "num_visitas": True,
            "lat": False, "lon": False, "_size": False,
        },
        zoom=6, height=550,
        color_continuous_scale=color_map if color_col == "ventas_periodo" else None,
    )
    fig_map.update_layout(
        mapbox_style="open-street-map", margin=dict(l=0, r=0, t=0, b=0),
    )
    st.plotly_chart(fig_map, use_container_width=True)
    st.caption(
        f"📍 {len(geo_df):,} clientes en el mapa "
        f"(de {len(assigned_partners):,} totales del equipo). "
        f"Tamaño del punto = ventas en el período."
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
    st.info("Sin visitas en el período.")
else:
    f_c1, f_c2, f_c3 = st.columns(3)
    f_c1.metric(
        "Visitas totales", f"{int(freq['num_visitas'].sum()):,}",
        help="Suma de facturas (visita = factura emitida).",
    )
    f_c2.metric(
        "Visitas por cliente", f"{freq['num_visitas'].mean():.1f}",
    )
    media_dias = freq["dias_entre_visitas_prom"].dropna().mean()
    f_c3.metric(
        "Días entre visitas", f"{media_dias:.0f}" if pd.notna(media_dias) else "—",
        help="Promedio sobre clientes con más de 1 visita en el período.",
    )
    st.dataframe(
        freq.rename(columns={
            "partner_name": "Cliente", "city": "Ciudad",
            "num_visitas": "# Visitas",
            "dias_entre_visitas_prom": "Días entre visitas",
            "ultima_visita": "Última visita",
            "dias_desde_ultima": "Días desde última",
            "ventas_periodo": "Ventas período",
        })[["Cliente", "Ciudad", "# Visitas", "Días entre visitas",
            "Última visita", "Días desde última", "Ventas período"]],
        column_config={
            "Ventas período": st.column_config.NumberColumn(format="$ %,.0f"),
            "Días entre visitas": st.column_config.NumberColumn(format="%.1f"),
            "Días desde última": st.column_config.NumberColumn(format="%.0f"),
            "Última visita": st.column_config.DateColumn(format="DD/MM/YYYY"),
        },
        use_container_width=True, hide_index=True, height=320,
    )

# ---------------------------------------------------------------------------
# Clientes inactivos
# ---------------------------------------------------------------------------
st.markdown("### 😴 Clientes inactivos")
threshold = st.slider(
    "Umbral de inactividad (días)", 30, 180, 60, step=15, key="ruta_inact",
)
inact = detect_inactive_clients(
    invoice_lines=lines_team,
    assigned_partners=assigned_partners,
    cutoff=fecha_hasta,
    company_ids=filters["company_ids"],
    threshold_days=threshold,
)
if inact.empty:
    st.success(f"✅ Ningún cliente inactivo por más de {threshold} días.")
else:
    st.caption(
        f"{len(inact):,} clientes del equipo NO compran hace más de "
        f"**{threshold} días**. Ordenados por valor histórico (los más "
        "valiosos primero — son los que más urge reactivar)."
    )
    cols = [c for c in [
        "partner_name", "city", "ultima_factura",
        "dias_desde_ultima", "ventas_historicas",
    ] if c in inact.columns]
    st.dataframe(
        inact[cols].rename(columns={
            "partner_name": "Cliente", "city": "Ciudad",
            "ultima_factura": "Última factura",
            "dias_desde_ultima": "Días sin comprar",
            "ventas_historicas": "Ventas históricas",
        }),
        column_config={
            "Ventas históricas": st.column_config.NumberColumn(format="$ %,.0f"),
            "Días sin comprar": st.column_config.NumberColumn(format="%.0f"),
            "Última factura": st.column_config.DateColumn(format="DD/MM/YYYY"),
        },
        use_container_width=True, hide_index=True, height=400,
    )

# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
st.markdown("---")
buf = io.BytesIO()
with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
    pd.DataFrame([{
        "Vendedores": ", ".join(selected_names),
        "Período desde": fecha_desde,
        "Período hasta": fecha_hasta,
        "Ventas netas": kpis.ventas_netas,
        "# Facturas": kpis.n_facturas,
        "Clientes atendidos": kpis.n_clientes_unicos,
        "Clientes vinculados": len(assigned_partners),
        "Cobertura %": cobertura,
    }]).to_excel(writer, sheet_name="Resumen", index=False)
    if not monthly.empty:
        monthly.drop(columns=["mes"]).to_excel(
            writer, sheet_name="Mensual", index=False)
    if not by_city.empty:
        by_city.to_excel(writer, sheet_name="Por ciudad", index=False)
    if not freq.empty:
        freq.to_excel(writer, sheet_name="Frecuencia", index=False)
    if not inact.empty:
        inact.to_excel(writer, sheet_name="Inactivos", index=False)
    if not geo_df.empty:
        geo_df.drop(columns=[c for c in ["_size"] if c in geo_df.columns])\
            .to_excel(writer, sheet_name="Mapa", index=False)

st.download_button(
    "⬇️ Descargar Excel — Ventas en Ruta",
    data=buf.getvalue(),
    file_name=f"ventas_ruta_{fecha_desde}_{fecha_hasta}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)
