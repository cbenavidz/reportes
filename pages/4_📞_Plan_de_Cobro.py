# -*- coding: utf-8 -*-
"""Página: plan de cobro priorizado y próximos vencimientos."""
import io

import pandas as pd
import streamlit as st

from src.auth import logout_button, require_auth
from src.data_loader import compute_full_analysis, filter_analysis_by_vendedor
from src.ui_components import (
    render_company_context,
    render_sidebar_filters,
    render_sidebar_vendedor_filter,
)

st.set_page_config(page_title="Plan de cobro | Cartera", page_icon="📞", layout="wide")

require_auth()
logout_button()

st.title("📞 Plan de cobro")
st.caption("Recomendaciones priorizadas para gestión de cartera hoy.")

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

plan = data["plan_cobro"]
if plan.empty:
    st.success("No hay clientes con saldo pendiente. 🎉")
    st.stop()

# --- Filtros
col_f1, col_f2 = st.columns(2)
with col_f1:
    prioridades = st.multiselect(
        "Prioridad",
        options=["URGENTE", "ALTA", "MEDIA", "PROACTIVA", "BAJA"],
        default=["URGENTE", "ALTA"],
    )
with col_f2:
    busqueda = st.text_input("Buscar cliente", placeholder="Nombre")

df = plan.copy()
if prioridades:
    df = df[df["prioridad"].isin(prioridades)]
if busqueda:
    df = df[df["partner_name"].astype(str).str.contains(busqueda, case=False, na=False)]

# Resumen
c1, c2, c3 = st.columns(3)
c1.metric("Clientes a contactar", len(df))
c2.metric("Saldo total a gestionar", f"${df['saldo_actual'].sum():,.0f}")
c3.metric("Vencido a recuperar", f"${df['monto_vencido'].sum():,.0f}")

st.markdown("---")

# --- Tabla de plan de cobro
st.subheader("📋 Plan priorizado")
cols_show = [
    "prioridad",
    "accion",
    "partner_name",
    "phone",
    "mobile",
    "email",
    "saldo_actual",
    "monto_vencido",
    "dias_vencido_max",
    "calificacion",
    "score_total",
    "observaciones",
    "sugerencia_cupo",
    "sugerencia_plazo",
]
cols_present = [c for c in cols_show if c in df.columns]

display = df[cols_present].rename(
    columns={
        "prioridad": "Prioridad",
        "accion": "Acción",
        "partner_name": "Cliente",
        "phone": "Tel.",
        "mobile": "Cel.",
        "email": "Email",
        "saldo_actual": "Saldo",
        "monto_vencido": "Vencido",
        "dias_vencido_max": "Mora máx (días)",
        "calificacion": "Cal.",
        "score_total": "Score",
        "observaciones": "Observaciones",
        "sugerencia_cupo": "Sugerencia límite crédito",
        "sugerencia_plazo": "Sugerencia plazo",
    }
)

st.dataframe(
    display,
    hide_index=True,
    use_container_width=True,
    height=500,
    column_config={
        "Saldo": st.column_config.NumberColumn(format="$%,.0f"),
        "Vencido": st.column_config.NumberColumn(format="$%,.0f"),
        "Score": st.column_config.NumberColumn(format="%.1f"),
    },
)

# Export
buf = io.BytesIO()
with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
    display.to_excel(writer, sheet_name="Plan de cobro", index=False)
    if not data["proximos_vencer"].empty:
        data["proximos_vencer"].to_excel(writer, sheet_name="Próximos a vencer", index=False)
st.download_button(
    "⬇️ Descargar plan en Excel",
    data=buf.getvalue(),
    file_name=f"plan_cobro_{data['cutoff_date']}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)

st.markdown("---")

# --- Próximos vencimientos
st.subheader("📅 Cobro proactivo: vencen en los próximos 7 días")
proximos = data["proximos_vencer"]
if proximos.empty:
    st.info("No hay vencimientos en los próximos 7 días.")
else:
    proximos_disp = proximos.copy()
    if "saldo" in proximos_disp.columns:
        proximos_disp["saldo"] = proximos_disp["saldo"].abs()
    st.dataframe(
        proximos_disp.rename(
            columns={
                "partner_name": "Cliente",
                "factura": "Factura",
                "fecha_vencimiento": "Vence",
                "dias_para_vencer": "Días para vencer",
                "saldo": "Saldo",
            }
        ),
        hide_index=True,
        use_container_width=True,
        column_config={
            "Saldo": st.column_config.NumberColumn(format="$%,.0f"),
        },
    )
