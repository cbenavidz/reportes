# -*- coding: utf-8 -*-
"""
Página: Rutero de vendedores externos.

Arma un plan de visitas semanal (Lun-Vie) para cada vendedor externo a partir
de la georreferenciación de sus clientes y su ritmo histórico de compra
(últimos 12 meses por defecto). Un rutero por vendedor. Los clientes sin
coordenadas GPS quedan en una lista aparte "por geolocalizar".
"""
from __future__ import annotations

import io
from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

from src.auth import logout_button, require_auth
from src.data_loader import compute_full_analysis, load_invoice_lines
from src.route_sales import (
    build_geo_dataframe,
    compute_visit_frequency,
    get_partners_for_sellers,
)
from src import rutero_planner as rp
from src.ui_components import render_company_context, render_sidebar_filters

st.set_page_config(page_title="Rutero | Cartera", page_icon="🧭", layout="wide")

require_auth()
logout_button()

st.title("🧭 Rutero de vendedores externos")
st.caption(
    "Plan de visitas semanal (Lun-Vie) por vendedor, según la georreferencia "
    "de los clientes y su ritmo histórico de compra. El **día** lo define la "
    "zona geográfica; la **frecuencia** (semanal/quincenal/mensual) sale de la "
    "cadencia de compra; el **orden** de cada día se optimiza por cercanía. "
    "Los clientes sin GPS quedan en una lista aparte para geolocalizar."
)
st.info(
    "🗓️ **Festivos en lunes:** como en Colombia muchos festivos caen en lunes "
    "(Ley Emiliani), el rutero coloca la **zona de menor venta el lunes**, para "
    "arriesgar lo mínimo cuando sea festivo. Si un lunes es festivo, reprograma "
    "esa ruta al día disponible más cercano de la semana."
)

DEFAULT_SELLERS = ["luis felipe", "felipe", "yarley", "vanessa", "vannesa"]


def fmt_money(x) -> str:
    try:
        return f"${x:,.0f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return "$0"


# ── Filtros ──
filters = render_sidebar_filters()
if filters["company_ids"] is not None and len(filters["company_ids"]) == 0:
    st.warning("Selecciona al menos una empresa en el sidebar.")
    st.stop()
company_ids = filters["company_ids"]

hoy = date.today()
desde = hoy - timedelta(days=365)

with st.spinner("Cargando clientes y ventas (últimos 12 meses)..."):
    data = compute_full_analysis(
        months_back=max(filters["months_back"], 12),
        rotation_period_days=filters["period_days"],
        company_ids=company_ids,
        exclude_cash_sales=filters["exclude_cash_sales"],
        analysis_window_days=filters.get("analysis_window_days"),
    )
    partners_all = data.get("raw_partners")
    invoices_all = data.get("raw_invoices")
    lines_all = load_invoice_lines(
        company_ids=tuple(company_ids) if company_ids else None,
        date_from=desde.isoformat(), date_to=hoy.isoformat(),
    )

render_company_context(data.get("companies"), company_ids)

if partners_all is None or partners_all.empty:
    st.error("No se pudieron cargar los clientes.")
    st.stop()

# ── Detectar vendedores ──
seller_df = pd.DataFrame()
if (
    invoices_all is not None and not invoices_all.empty
    and "invoice_user_id" in invoices_all.columns
    and "invoice_user_id_name" in invoices_all.columns
):
    seller_df = (
        invoices_all[["invoice_user_id", "invoice_user_id_name"]]
        .dropna(subset=["invoice_user_id"]).drop_duplicates("invoice_user_id")
        .rename(columns={"invoice_user_id": "user_id",
                         "invoice_user_id_name": "user_name"})
    )
if seller_df.empty and "user_id" in partners_all.columns:
    seller_df = (
        partners_all[["user_id", "user_name"]]
        .dropna(subset=["user_id"]).drop_duplicates("user_id")
    )
if seller_df.empty:
    st.error("No se detectaron vendedores en facturas ni en clientes.")
    st.stop()
seller_df["user_id"] = seller_df["user_id"].astype(int)
seller_df = seller_df.sort_values("user_name").reset_index(drop=True)

opciones = seller_df["user_name"].astype(str).tolist()
default_sel = [n for n in opciones if any(d in n.lower() for d in DEFAULT_SELLERS)]
seleccion = st.multiselect(
    "Vendedores para el rutero",
    options=opciones,
    default=default_sel if default_sel else opciones[:2],
    help="Por defecto: Luis Felipe y Yarley Vanessa. Se arma un rutero por cada uno.",
)
if not seleccion:
    st.warning("Selecciona al menos un vendedor.")
    st.stop()


# ── Cálculo del rutero por vendedor ──
def rutero_de_vendedor(user_id: int):
    assigned = get_partners_for_sellers(partners_all, invoices_all, [user_id])
    if assigned is None or assigned.empty:
        return None, None, None

    # Líneas emitidas por el vendedor (invoice_user_id) o, si no se detectan,
    # de sus clientes asignados.
    moves_v: set[int] = set()
    if (
        invoices_all is not None and not invoices_all.empty
        and "invoice_user_id" in invoices_all.columns
    ):
        idv = pd.to_numeric(invoices_all["invoice_user_id"], errors="coerce")
        moves_v = set(pd.to_numeric(
            invoices_all.loc[idv == user_id, "id"], errors="coerce"
        ).dropna().astype(int))
    if lines_all is not None and not lines_all.empty and moves_v:
        lines_v = lines_all[
            pd.to_numeric(lines_all["move_id"], errors="coerce").isin(moves_v)
        ].copy()
    elif lines_all is not None and not lines_all.empty:
        asig_ids = set(assigned["id"].astype(int))
        lines_v = lines_all[lines_all["partner_id"].isin(asig_ids)].copy()
    else:
        lines_v = pd.DataFrame()

    geo = build_geo_dataframe(assigned, lines_v, desde, hoy, company_ids)
    freq = compute_visit_frequency(lines_v, assigned, desde, hoy, company_ids)
    rutero = rp.build_rutero(geo, freq, dias=5)

    # Clientes activos sin GPS (con ventas > 0 pero sin coordenadas)
    sin_gps = pd.DataFrame()
    if lines_v is not None and not lines_v.empty:
        ventas_pid = (
            lines_v.groupby("partner_id")["price_subtotal_signed"].sum()
        )
        activos = ventas_pid[ventas_pid > 0]
        con_gps = set(rutero["partner_id"].astype(int)) if not rutero.empty else set()
        faltan = [int(p) for p in activos.index if int(p) not in con_gps]
        if faltan:
            nm = (
                lines_v.dropna(subset=["partner_name"])
                .drop_duplicates("partner_id").set_index("partner_id")["partner_name"]
                .to_dict()
            )
            city_map = {}
            if "city" in assigned.columns:
                city_map = (
                    assigned[["id", "city"]].set_index("id")["city"].to_dict()
                )
            sin_gps = pd.DataFrame([{
                "partner_name": nm.get(p, "—"),
                "city": city_map.get(p, "—") or "—",
                "ventas_periodo": float(activos.get(p, 0.0)),
            } for p in faltan]).sort_values("ventas_periodo", ascending=False)
    return rutero, rp.resumen_por_dia(rutero), sin_gps


# Mapa user_name -> user_id de la selección
sel_ids = [
    (n, int(seller_df.loc[seller_df["user_name"] == n, "user_id"].iloc[0]))
    for n in seleccion
]

resultados = {}
tabs = st.tabs([n for n, _ in sel_ids])
for (nombre, uid), tab in zip(sel_ids, tabs):
    with tab:
        rutero, resumen, sin_gps = rutero_de_vendedor(uid)
        resultados[nombre] = (rutero, sin_gps)
        if rutero is None or rutero.empty:
            st.info(
                f"**{nombre}** no tiene clientes con GPS y ventas en el período. "
                "Revisa que sus clientes tengan coordenadas en Odoo."
            )
            if sin_gps is not None and not sin_gps.empty:
                st.markdown("#### 📍 Clientes sin GPS (por geolocalizar)")
                st.dataframe(sin_gps, use_container_width=True, hide_index=True)
            continue

        m = st.columns(4)
        m[0].metric("Clientes en ruta", f"{len(rutero):,}")
        m[1].metric("Sin GPS", f"{0 if sin_gps is None else len(sin_gps):,}")
        m[2].metric("Ventas 12m (con GPS)", fmt_money(rutero["ventas_periodo"].sum()))
        m[3].metric("Km totales/semana", f"{resumen['km_ruta'].sum():.0f}")

        # Mapa por día
        figm = px.scatter_mapbox(
            rutero, lat="lat", lon="lon", color="dia",
            category_orders={"dia": rp.DIAS},
            hover_name="partner_name",
            hover_data={"orden": True, "frecuencia": True, "semanas": True,
                        "ventas_periodo": ":,.0f", "city": True,
                        "lat": False, "lon": False, "dia": False},
            zoom=8, height=520,
        )
        figm.update_layout(mapbox_style="open-street-map",
                           margin=dict(l=0, r=0, t=0, b=0),
                           legend_title="Día")
        st.plotly_chart(figm, use_container_width=True)

        # Resumen por día
        st.markdown("#### 📅 Resumen por día")
        st.dataframe(
            resumen, use_container_width=True, hide_index=True,
            column_config={
                "dia": "Día",
                "n_clientes": "# Clientes",
                "ventas_periodo": st.column_config.NumberColumn("Ventas 12m", format="localized"),
                "km_ruta": st.column_config.NumberColumn("Km ruta", format="%.1f"),
            },
        )

        # Calendario detallado por día
        st.markdown("#### 🗺️ Rutero detallado")
        for dia in rp.DIAS:
            sub = rutero[rutero["dia"] == dia]
            if sub.empty:
                continue
            with st.expander(f"{dia} — {len(sub)} clientes", expanded=(dia == "Lunes")):
                st.dataframe(
                    sub[["orden", "partner_name", "city", "frecuencia",
                         "semanas", "ventas_periodo", "cadencia_dias"]],
                    use_container_width=True, hide_index=True,
                    column_config={
                        "orden": "Orden",
                        "partner_name": "Cliente",
                        "city": "Ciudad",
                        "frecuencia": "Frecuencia",
                        "semanas": "Semanas del mes",
                        "ventas_periodo": st.column_config.NumberColumn("Ventas 12m", format="localized"),
                        "cadencia_dias": st.column_config.NumberColumn("Cadencia (días)", format="%.0f"),
                    },
                )

        # Lista sin GPS
        if sin_gps is not None and not sin_gps.empty:
            st.markdown("#### 📍 Clientes sin GPS (por geolocalizar)")
            st.caption(
                "Tienen ventas pero no coordenadas en Odoo, así que no entran a la "
                "ruta. Geolocalízalos en Odoo (Contactos → Acción → Geolocalizar) "
                "y volverán a aparecer, priorizando los de mayor venta."
            )
            st.dataframe(
                sin_gps, use_container_width=True, hide_index=True,
                column_config={
                    "partner_name": "Cliente", "city": "Ciudad",
                    "ventas_periodo": st.column_config.NumberColumn("Ventas 12m", format="localized"),
                },
            )


# ── Export a Excel ──
def build_excel(resultados: dict) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="xlsxwriter") as xw:
        for nombre, (rutero, sin_gps) in resultados.items():
            base = "".join(c for c in nombre if c.isalnum() or c == " ")[:22].strip()
            if rutero is not None and not rutero.empty:
                out = rutero[[
                    "dia", "orden", "partner_name", "city", "frecuencia",
                    "semanas", "ventas_periodo", "cadencia_dias", "lat", "lon",
                ]].rename(columns={
                    "dia": "Día", "orden": "Orden", "partner_name": "Cliente",
                    "city": "Ciudad", "frecuencia": "Frecuencia",
                    "semanas": "Semanas", "ventas_periodo": "Ventas 12m",
                    "cadencia_dias": "Cadencia (días)", "lat": "Lat", "lon": "Lon",
                })
                out.to_excel(xw, sheet_name=f"Rutero {base}"[:31], index=False)
            if sin_gps is not None and not sin_gps.empty:
                sin_gps.rename(columns={
                    "partner_name": "Cliente", "city": "Ciudad",
                    "ventas_periodo": "Ventas 12m",
                }).to_excel(xw, sheet_name=f"SinGPS {base}"[:31], index=False)
    return buf.getvalue()


if resultados:
    st.divider()
    xls = build_excel(resultados)
    st.download_button(
        "⬇️ Descargar rutero en Excel (imprimible)",
        xls, "rutero_vendedores.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
