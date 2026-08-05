# -*- coding: utf-8 -*-
"""
Página: Tabla Dinámica de Cartera.

Como una tabla dinámica de Excel, pero contra Odoo en vivo: eliges filas,
columnas, métrica y filtros, y se recalcula al instante.

Usa la MISMA fuente que Cuentas por Cobrar (pipeline de cartera), así que los
totales cuadran con el resto del tablero. Solo lectura.
"""
from __future__ import annotations

import importlib.util
import io
from datetime import date

import pandas as pd
import plotly.express as px
import streamlit as st

from src.auth import logout_button, require_auth
from src.data_loader import compute_full_analysis, load_companies, load_receivables
from src.receivables_analyzer import enrich_receivables
from src import pivot_cartera as pc
from src.ui_components import render_company_context, render_sidebar_filters

st.set_page_config(page_title="Tabla Dinámica Cartera | Cartera",
                   page_icon="🧮", layout="wide")

require_auth()
logout_button()

st.title("🧮 Tabla Dinámica de Cartera")
st.caption(
    "Cruza la cartera como en Excel, pero con datos en vivo. Usa la misma "
    "fuente que **Cuentas por Cobrar** (respeta los filtros del sidebar), "
    "así que los totales cuadran con el resto del tablero."
)


def fmt_cop(x) -> str:
    try:
        return f"${x:,.0f}".replace(",", ".")
    except (TypeError, ValueError):
        return "$0"


filters = render_sidebar_filters()
if filters["company_ids"] is not None and len(filters["company_ids"]) == 0:
    st.warning("Selecciona al menos una empresa.")
    st.stop()

today = date.today()
company_ids = tuple(filters["company_ids"]) if filters["company_ids"] else None

render_company_context(load_companies(), filters["company_ids"])

# ── Carga (misma lógica que Cuentas por Cobrar) ──
with st.spinner("Cargando cartera..."):
    receivables = load_receivables(company_ids=company_ids)
    analysis = compute_full_analysis(
        months_back=filters["months_back"],
        rotation_period_days=filters["period_days"],
        company_ids=filters["company_ids"],
        exclude_cash_sales=filters["exclude_cash_sales"],
        analysis_window_days=filters.get("analysis_window_days"),
    )

if receivables is None or receivables.empty:
    st.success("🎉 No hay facturas pendientes de cobro.")
    st.stop()

# Alinear con el pipeline de cartera (excluir contado / ventana de análisis)
open_inv = analysis.get("open_invoices")
if open_inv is not None and not open_inv.empty and "id" in open_inv.columns:
    ids_cartera = set(pd.to_numeric(open_inv["id"], errors="coerce").dropna().astype(int))
    receivables = receivables[
        pd.to_numeric(receivables["id"], errors="coerce").isin(ids_cartera)
    ].copy()

if receivables.empty:
    st.warning("No hay cartera con los filtros seleccionados.")
    st.stop()

enriched = enrich_receivables(receivables, today=today)
df = pc.preparar(enriched, analysis.get("raw_partners"))
if df.empty:
    st.warning("No hay datos para cruzar.")
    st.stop()

# ── Filtros de la dinámica ──
st.markdown("### 🔎 Filtros")
fc = st.columns(4)
filtros_activos: dict[str, list] = {}
for i, dim in enumerate(["Vendedor", "Ciudad", "Antigüedad (aging)", "Estado"]):
    col = pc.DIMENSIONES[dim]
    opts = sorted(df[col].dropna().unique().tolist())
    if dim == "Antigüedad (aging)":
        opts = [b for b in pc.ORDEN_AGING if b in opts] + \
               [o for o in opts if o not in pc.ORDEN_AGING]
    with fc[i]:
        sel = st.multiselect(dim, opts, default=[], placeholder="Todos")
    if sel:
        filtros_activos[col] = sel

dff = df.copy()
for col, vals in filtros_activos.items():
    dff = dff[dff[col].isin(vals)]
if dff.empty:
    st.warning("Ningún registro con esos filtros.")
    st.stop()

# ── KPIs del subconjunto ──
k = st.columns(4)
k[0].metric("Saldo por cobrar", fmt_cop(dff["saldo"].sum()))
k[1].metric("Saldo vencido", fmt_cop(dff["saldo_vencido"].sum()))
k[2].metric("Facturas", f"{len(dff):,}")
k[3].metric("Clientes", f"{dff['partner_id'].nunique():,}")

st.divider()

# ── Configuración de la dinámica ──
st.markdown("### 🧮 Arma tu cruce")
c1, c2, c3 = st.columns(3)
with c1:
    filas = st.multiselect(
        "Filas", list(pc.DIMENSIONES.keys()), default=["Vendedor"],
        help="Puedes anidar varias dimensiones.",
    )
with c2:
    columnas = st.multiselect(
        "Columnas", list(pc.DIMENSIONES.keys()),
        default=["Antigüedad (aging)"],
    )
with c3:
    metrica = st.selectbox("Métrica", list(pc.METRICAS.keys()), index=0)

if not filas and not columnas:
    st.info("Elige al menos una dimensión en filas o columnas.")
    st.stop()

piv = pc.construir(dff, filas, columnas, metrica, totales=True)
if piv.empty:
    st.warning("No se pudo construir la tabla con esa combinación.")
    st.stop()

es_dinero = metrica in ("Saldo por cobrar", "Saldo vencido", "Total facturado")
fmt = "{:,.0f}" if es_dinero or metrica.startswith("#") else "{:,.1f}"

styled = piv.style.format(fmt)
# background_gradient necesita matplotlib; si no está instalado (p. ej. en el
# deploy de Streamlit Cloud), mostramos la tabla sin el degradado de color.
if importlib.util.find_spec("matplotlib") is not None:
    styled = styled.background_gradient(
        cmap="Reds" if "vencid" in metrica.lower() else "Blues", axis=None,
    )

st.dataframe(styled, use_container_width=True)

# ── Gráfica del cruce ──
try:
    plot = piv.drop(index="TOTAL", errors="ignore")
    if "TOTAL" in plot.columns:
        plot = plot.drop(columns="TOTAL")
    if not plot.empty and len(plot.columns):
        long = plot.reset_index().melt(
            id_vars=plot.index.names or [plot.index.name or "index"],
            var_name="serie", value_name="valor",
        )
        xcol = long.columns[0]
        st.plotly_chart(
            px.bar(long, x=xcol, y="valor", color="serie", barmode="group",
                   labels={"valor": metrica, xcol: "", "serie": ""})
            .update_layout(height=380, margin=dict(l=0, r=0, t=10, b=0),
                           legend=dict(orientation="h", y=-0.2)),
            use_container_width=True,
        )
except Exception:  # noqa: BLE001 — la gráfica es un extra, no debe romper la tabla
    pass

st.divider()

# ── Exportación ──
st.markdown("### 📥 Descargar")
st.caption(
    "**Tabla dinámica**: el cruce tal como lo ves. **Datos planos**: una fila "
    "por factura, para que armes tus propias dinámicas en Excel "
    "(Insertar → Tabla dinámica)."
)
plana = pc.tabla_plana(dff)
buf = io.BytesIO()
with pd.ExcelWriter(buf, engine="xlsxwriter") as xw:
    piv.to_excel(xw, sheet_name="Tabla dinámica")
    plana.to_excel(xw, sheet_name="Datos planos", index=False)
    ws = xw.sheets["Datos planos"]
    ws.autofilter(0, 0, max(len(plana), 1), max(len(plana.columns) - 1, 0))
    ws.freeze_panes(1, 0)

st.download_button(
    "⬇️ Descargar Excel (dinámica + datos planos)", buf.getvalue(),
    "cartera_tabla_dinamica.xlsx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    type="primary",
)
