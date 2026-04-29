# -*- coding: utf-8 -*-
"""
Cartera Inteligente – Casa de los Mineros
==========================================
App principal: dashboard ejecutivo con KPIs de cartera.

Cómo correr localmente:
    streamlit run app.py
"""
from __future__ import annotations

import streamlit as st

from src.auth import logout_button, require_auth
from src.data_loader import (
    compute_full_analysis,
    filter_analysis_by_vendedor,
    test_connection_summary,
)
from src.ui_components import (
    render_aging_chart,
    render_company_context,
    render_history_dso,
    render_history_facturado_cobrado,
    render_history_saldo,
    render_kpis,
    render_sidebar_filters,
    render_sidebar_vendedor_filter,
)

# ---------------------------------------------------------------------------
# Configuración de la página
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Cartera | Casa de los Mineros",
    page_icon="💰",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Estilos básicos
st.markdown(
    """
    <style>
        .main .block-container { padding-top: 2rem; padding-bottom: 2rem; }
        [data-testid="stMetricValue"] { font-size: 1.8rem; }
        .stDataFrame { font-size: 0.9rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
user = require_auth()
logout_button()

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
col_h1, col_h2 = st.columns([3, 1])
with col_h1:
    st.title("💰 Cartera Inteligente")
    st.caption("Casa de los Mineros · Dashboard Ejecutivo")
with col_h2:
    if st.button("🔄 Actualizar datos", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

# ---------------------------------------------------------------------------
# Filtros
# ---------------------------------------------------------------------------
filters = render_sidebar_filters()
months_back = filters["months_back"]
period_days = filters["period_days"]
company_ids = filters["company_ids"]
exclude_cash_sales = filters["exclude_cash_sales"]
analysis_window_days = filters.get("analysis_window_days")

# Si el usuario deseleccionó todas las empresas, no descargamos
if company_ids is not None and len(company_ids) == 0:
    st.warning("Selecciona al menos una empresa en el sidebar para ver datos.")
    st.stop()

# ---------------------------------------------------------------------------
# Carga de datos
# ---------------------------------------------------------------------------
try:
    data = compute_full_analysis(
        months_back=months_back,
        rotation_period_days=period_days,
        company_ids=company_ids,
        exclude_cash_sales=exclude_cash_sales,
        analysis_window_days=analysis_window_days,
    )
except Exception as exc:
    st.error("❌ No se pudieron cargar los datos desde Odoo.")
    st.exception(exc)
    with st.expander("🔍 Diagnóstico de conexión"):
        st.json(test_connection_summary())
    st.stop()

# ---------------------------------------------------------------------------
# Filtro por vendedor (después de cargar datos para tener la lista real)
# ---------------------------------------------------------------------------
vendedor_user_ids = render_sidebar_vendedor_filter(data.get("raw_partners"))
if vendedor_user_ids:
    data = filter_analysis_by_vendedor(
        data,
        vendedor_user_ids,
        period_days=period_days,
        exclude_cash_sales=exclude_cash_sales,
    )

# ---------------------------------------------------------------------------
# Banner de empresas activas
# ---------------------------------------------------------------------------
render_company_context(data.get("companies"), company_ids)

# Aviso visible si hay vendedor(es) seleccionados
if vendedor_user_ids:
    partners_df = data.get("raw_partners")
    if partners_df is not None and not partners_df.empty:
        names = (
            partners_df.loc[partners_df["user_id"].isin(list(vendedor_user_ids)), "user_name"]
            .dropna()
            .unique()
            .tolist()
        )
        if names:
            st.info(f"👤 Filtrando por vendedor(es): **{', '.join(names)}**")

# ---------------------------------------------------------------------------
# KPIs principales
# ---------------------------------------------------------------------------
render_kpis(data["kpis"], cutoff_date=data["cutoff_date"])

st.markdown("---")

# ---------------------------------------------------------------------------
# Histórico (últimos 12 meses)
# ---------------------------------------------------------------------------
st.subheader("📈 Evolución mensual (últimos 12 meses)")

history_df = data.get("history")

col_h1, col_h2 = st.columns([3, 2])
with col_h1:
    st.markdown("**Facturado a crédito vs. Cobrado**")
    render_history_facturado_cobrado(history_df)
    st.caption(
        "Compara mes a mes lo facturado a crédito (excluye contado si está activo el filtro) "
        "contra los pagos efectivamente recibidos."
    )
with col_h2:
    st.markdown("**DSO rolling 90d (salud reciente)**")
    render_history_dso(history_df)
    st.caption(
        "Días de cobro calculados sobre saldo de fin de mes vs. ventas de los últimos 90 días. "
        "El último valor de esta serie es el KPI **DSO últimos 90 días** del tablero."
    )

st.markdown("**Saldo de cartera estimado al cierre de cada mes**")
render_history_saldo(history_df)
st.caption(
    "Aproximación: facturado neto acumulado − cobrado acumulado. "
    "Para reconstrucción exacta histórica conciliada se requiere un cálculo más pesado sobre `account.move.line`."
)

st.markdown("---")

# ---------------------------------------------------------------------------
# Aging chart + tabla
# ---------------------------------------------------------------------------
col_g1, col_g2 = st.columns([2, 1])
with col_g1:
    st.subheader("📊 Antigüedad de saldos (Aging)")
    render_aging_chart(data["aging"])
with col_g2:
    st.subheader("Detalle")
    aging_display = data["aging"][["rango", "num_facturas", "monto", "pct_total"]].copy()
    aging_display["monto"] = aging_display["monto"].apply(lambda x: f"${x:,.0f}")
    aging_display["pct_total"] = aging_display["pct_total"].apply(lambda x: f"{x:.1f}%")
    st.dataframe(aging_display, hide_index=True, use_container_width=True)

st.markdown("---")

# ---------------------------------------------------------------------------
# Resumen de alertas y plan de cobro
# ---------------------------------------------------------------------------
col_a, col_b = st.columns(2)

with col_a:
    st.subheader("🚨 Alertas activas")
    alerts = data["alerts"]
    if alerts.empty:
        st.success("Sin alertas activas en este momento. 🎉")
    else:
        n_crit = (alerts["nivel"] == "critical").sum()
        n_warn = (alerts["nivel"] == "warning").sum()
        st.metric("Críticas", n_crit, delta=None, delta_color="inverse")
        st.metric("Advertencias", n_warn)
        st.caption("👉 Detalle completo en la página **Alertas**")
        st.dataframe(
            alerts.head(5)[["nivel", "titulo", "cliente", "monto"]],
            hide_index=True,
            use_container_width=True,
        )

with col_b:
    st.subheader("📞 Top prioridades de cobro hoy")
    plan = data["plan_cobro"]
    if plan.empty:
        st.info("No hay clientes con saldo pendiente.")
    else:
        top = plan.head(8)
        cols_show = ["prioridad", "partner_name", "saldo_actual", "monto_vencido", "accion"]
        cols_present = [c for c in cols_show if c in top.columns]
        st.dataframe(
            top[cols_present].rename(
                columns={
                    "partner_name": "Cliente",
                    "saldo_actual": "Saldo",
                    "monto_vencido": "Vencido",
                    "accion": "Acción sugerida",
                    "prioridad": "Prioridad",
                }
            ),
            hide_index=True,
            use_container_width=True,
        )
        st.caption("👉 Plan completo en la página **Plan de Cobro**")

st.markdown("---")
st.caption(
    f"Datos consultados al {data['cutoff_date']} · "
    f"Período de rotación: {period_days} días · "
    f"Histórico cargado: {months_back} meses"
)
