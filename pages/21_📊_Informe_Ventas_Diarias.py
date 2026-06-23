# -*- coding: utf-8 -*-
"""
Página: Informe de Ventas Diarias.

Vista de un día: KPIs (ventas brutas, NC, netas, costo, margen, margen %),
detalle por categoría y por producto con utilidad.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from src.auth import logout_button, require_auth
from src.data_loader import load_companies, load_invoice_lines
from src.sales_analyzer import (
    compute_sales_by_product,
    compute_sales_kpis_from_lines,
)
from src.ui_components import render_company_context, render_sidebar_filters

st.set_page_config(
    page_title="Informe de Ventas Diarias | Cartera",
    page_icon="📊", layout="wide",
)
require_auth()
logout_button()

st.title("📊 Informe de Ventas Diarias")
st.caption(
    "Vista de un día: KPIs, detalle por categoría y por producto con "
    "costo, margen y utilidad."
)

# ── Sidebar ──
filters = render_sidebar_filters()
if filters["company_ids"] is not None and len(filters["company_ids"]) == 0:
    st.warning("Selecciona al menos una empresa.")
    st.stop()

# ── Configuración ──
cfg1, cfg2 = st.columns([2, 1])
with cfg1:
    fecha = st.date_input(
        "Fecha del informe",
        value=date.today(),
        max_value=date.today(),
        format="DD/MM/YYYY",
    )
with cfg2:
    if st.button("🔄 Recargar datos", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

companies_df = load_companies()
render_company_context(companies_df, filters["company_ids"])
company_ids = (
    tuple(filters["company_ids"]) if filters["company_ids"] else None
)
fecha_str = fecha.isoformat()


# ── Helpers de formato ──
def fmt_money(x) -> str:
    try:
        return f"${x:,.0f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return "$0"


# ── Carga ──
with st.spinner("Cargando facturas del día..."):
    lineas = load_invoice_lines(
        company_ids=company_ids, date_from=fecha_str, date_to=fecha_str,
    )

if lineas is None or lineas.empty:
    st.info("No hay facturas registradas en esta fecha.")
    st.stop()

# ── KPIs ──
kpis = compute_sales_kpis_from_lines(
    lineas, date_from=fecha, date_to=fecha, company_ids=company_ids,
)

st.markdown(f"### 📅 Resumen del día — {fecha.strftime('%d/%m/%Y')}")
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Ventas brutas", fmt_money(kpis.ventas_brutas))
c2.metric("Ventas netas", fmt_money(kpis.ventas_netas))
c3.metric("Costo", fmt_money(kpis.costo_ventas))
c4.metric("Margen bruto", fmt_money(kpis.margen))
c5.metric("Margen %", f"{kpis.margen_pct:.1f}%")

c6, c7, c8 = st.columns(3)
c6.metric("# Facturas", f"{kpis.n_facturas:,}")
c7.metric("# Notas crédito", f"{kpis.n_notas_credito:,}")
ticket = (
    kpis.ventas_netas / kpis.n_facturas if kpis.n_facturas else 0.0
)
c8.metric("Ticket promedio", fmt_money(ticket))

# ── Por categoría ──
st.markdown("### 🗂️ Por categoría")
por_cat = compute_sales_by_product(
    lineas, group_by="category",
    date_from=fecha, date_to=fecha, company_ids=company_ids,
)
if por_cat.empty:
    st.caption("Sin categorías para mostrar.")
else:
    por_cat_disp = por_cat.copy()
    cols_cat = [
        "categoria_nombre", "cantidad", "ventas_netas",
        "costo", "margen", "margen_pct", "n_facturas", "participacion_pct",
    ]
    cols_cat = [c for c in cols_cat if c in por_cat_disp.columns]
    st.dataframe(
        por_cat_disp[cols_cat], use_container_width=True, hide_index=True,
        column_config={
            "categoria_nombre": st.column_config.TextColumn(
                "Categoría", width="large",
            ),
            "cantidad": st.column_config.NumberColumn("Cant.", format="localized"),
            "ventas_netas": st.column_config.NumberColumn(
                "Ventas netas", format="localized",
            ),
            "costo": st.column_config.NumberColumn("Costo", format="localized"),
            "margen": st.column_config.NumberColumn("Margen", format="localized"),
            "margen_pct": st.column_config.NumberColumn(
                "Margen %", format="%.2f %%",
            ),
            "n_facturas": st.column_config.NumberColumn("# Fact.", format="%d"),
            "participacion_pct": st.column_config.NumberColumn(
                "% Particip.", format="%.2f %%",
            ),
        },
    )

# ── Por producto ──
st.markdown("### 📦 Por producto")
por_prod = compute_sales_by_product(
    lineas, group_by="product",
    date_from=fecha, date_to=fecha, company_ids=company_ids,
)
if por_prod.empty:
    st.caption("Sin productos para mostrar.")
else:
    por_prod_disp = por_prod.copy()
    cols_prod = [
        "product_nombre", "cantidad", "ventas_netas",
        "costo", "margen", "margen_pct", "n_facturas", "participacion_pct",
    ]
    cols_prod = [c for c in cols_prod if c in por_prod_disp.columns]
    st.dataframe(
        por_prod_disp[cols_prod], use_container_width=True, hide_index=True,
        height=520,
        column_config={
            "product_nombre": st.column_config.TextColumn(
                "Producto", width="large",
            ),
            "cantidad": st.column_config.NumberColumn("Cant.", format="localized"),
            "ventas_netas": st.column_config.NumberColumn(
                "Ventas netas", format="localized",
            ),
            "costo": st.column_config.NumberColumn("Costo", format="localized"),
            "margen": st.column_config.NumberColumn("Margen", format="localized"),
            "margen_pct": st.column_config.NumberColumn(
                "Margen %", format="%.2f %%",
            ),
            "n_facturas": st.column_config.NumberColumn("# Fact.", format="%d"),
            "participacion_pct": st.column_config.NumberColumn(
                "% Particip.", format="%.2f %%",
            ),
        },
    )

# ── Acciones (PDF + correo) ──
st.markdown("---")
st.markdown("### 📤 Enviar / Descargar")
st.caption(
    "El PDF combinado (Caja + Ventas Diarias) y el envío por correo se "
    "habilitan en una siguiente fase."
)
a1, a2 = st.columns(2)
with a1:
    st.button("⬇️ Descargar PDF (próximamente)", disabled=True,
              use_container_width=True)
with a2:
    st.button("✉️ Enviar por correo (próximamente)", disabled=True,
              use_container_width=True)
