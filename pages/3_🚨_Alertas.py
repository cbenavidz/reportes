# -*- coding: utf-8 -*-
"""Página: alertas de riesgo de cartera."""
import streamlit as st

from src.auth import logout_button, require_auth
from src.data_loader import compute_full_analysis, filter_analysis_by_vendedor
from src.ui_components import (
    render_company_context,
    render_sidebar_filters,
    render_sidebar_vendedor_filter,
)

st.set_page_config(page_title="Alertas | Cartera", page_icon="🚨", layout="wide")

require_auth()
logout_button()

st.title("🚨 Alertas de riesgo")
st.caption("Detección automática de situaciones que requieren atención.")

filters = render_sidebar_filters()
if filters["company_ids"] is not None and len(filters["company_ids"]) == 0:
    st.warning("Selecciona al menos una empresa en el sidebar para ver datos.")
    st.stop()

data = compute_full_analysis(
    months_back=filters["months_back"],
    rotation_period_days=filters["period_days"],
    company_ids=filters["company_ids"],
    exclude_cash_sales=filters["exclude_cash_sales"],
    analysis_window_days=filters.get("analysis_window_days"),
)

vendedor_user_ids = render_sidebar_vendedor_filter(data.get("raw_partners"))
if vendedor_user_ids:
    data = filter_analysis_by_vendedor(
        data,
        vendedor_user_ids,
        period_days=filters["period_days"],
        exclude_cash_sales=filters["exclude_cash_sales"],
    )

render_company_context(data.get("companies"), filters["company_ids"])

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

alerts = data["alerts"]
if alerts.empty:
    st.success("🎉 ¡Sin alertas activas! Tu cartera está sana.")
    st.stop()

# Resumen
n_crit = (alerts["nivel"] == "critical").sum()
n_warn = (alerts["nivel"] == "warning").sum()
n_info = (alerts["nivel"] == "info").sum()

c1, c2, c3 = st.columns(3)
c1.metric("🔴 Críticas", n_crit)
c2.metric("🟡 Advertencias", n_warn)
c3.metric("🔵 Informativas", n_info)

st.markdown("---")

# Filtros
col_f1, col_f2 = st.columns(2)
with col_f1:
    niveles = st.multiselect(
        "Nivel",
        options=["critical", "warning", "info"],
        default=["critical", "warning"],
    )
with col_f2:
    reglas = st.multiselect(
        "Tipo de alerta",
        options=sorted(alerts["regla"].unique().tolist()),
        default=[],
    )

df = alerts.copy()
if niveles:
    df = df[df["nivel"].isin(niveles)]
if reglas:
    df = df[df["regla"].isin(reglas)]

# Render con tarjetas por nivel
emoji_map = {"critical": "🔴", "warning": "🟡", "info": "🔵"}

for _, row in df.iterrows():
    emoji = emoji_map.get(row["nivel"], "•")
    if row["nivel"] == "critical":
        st.error(f"{emoji} **{row['titulo']}**")
    elif row["nivel"] == "warning":
        st.warning(f"{emoji} **{row['titulo']}**")
    else:
        st.info(f"{emoji} **{row['titulo']}**")
    st.caption(row["mensaje"])

st.markdown("---")
st.caption(
    f"Las alertas se recalculan cada vez que actualizas los datos. "
    f"Última actualización: {data['cutoff_date']}"
)
