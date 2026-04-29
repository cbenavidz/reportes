# -*- coding: utf-8 -*-
"""Página: detalle de aging y facturas abiertas."""
import io

import pandas as pd
import streamlit as st

from src.auth import logout_button, require_auth
from src.data_loader import compute_full_analysis, filter_analysis_by_vendedor
from src.ui_components import (
    render_aging_chart,
    render_company_context,
    render_sidebar_filters,
    render_sidebar_vendedor_filter,
)

st.set_page_config(page_title="Aging | Cartera", page_icon="📊", layout="wide")

require_auth()
logout_button()

st.title("📊 Aging y facturas abiertas")
st.caption(
    "Antigüedad de saldos calculada contra **fecha de vencimiento efectiva** "
    "(la jerarquía es: payment_term explícito → settlement real → vencimiento "
    "nominal). Las facturas de contado mal etiquetadas en Odoo se vencen el "
    "día de la factura, no +30d."
)
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

# --- Aging chart
st.subheader("Distribución por antigüedad")
render_aging_chart(data["aging"])

# --- Tabla de facturas abiertas
st.subheader("Detalle de facturas abiertas")

open_inv = data["open_invoices"].copy()
if open_inv.empty:
    st.success("✅ No hay facturas abiertas.")
    st.stop()

# Filtros
col_f1, col_f2, col_f3 = st.columns(3)
with col_f1:
    solo_vencidas = st.checkbox("Solo vencidas", value=False)
with col_f2:
    rango = st.selectbox(
        "Rango de días vencidos",
        ["Todas", "1-30", "31-60", "61-90", "91-180", ">180"],
    )
with col_f3:
    cliente_filter = st.text_input("Buscar cliente", placeholder="Nombre o NIT")

if solo_vencidas:
    open_inv = open_inv[open_inv["esta_vencida"]]

rango_map = {
    "1-30": (1, 30),
    "31-60": (31, 60),
    "61-90": (61, 90),
    "91-180": (91, 180),
    ">180": (181, 100_000),
}
if rango in rango_map:
    lo, hi = rango_map[rango]
    open_inv = open_inv[(open_inv["dias_vencido"] >= lo) & (open_inv["dias_vencido"] <= hi)]

if cliente_filter:
    mask = open_inv["partner_name"].astype(str).str.contains(cliente_filter, case=False, na=False)
    open_inv = open_inv[mask]

# Enriquecemos con la clasificación CONTADO/CRÉDITO real para que el usuario
# pueda ver POR QUÉ una factura aparece vencida desde el día de facturación
# (porque payment_term="Contado" aunque Odoo le puso un due nominal +30d).
from src.analyzer import classify_invoices_credit_vs_cash  # noqa: E402
if not open_inv.empty:
    is_credito = classify_invoices_credit_vs_cash(
        open_inv, payments=data.get("raw_payments")
    )
    open_inv = open_inv.assign(
        tipo_real=pd.Series(is_credito).map({True: "CRÉDITO", False: "CONTADO"}).values
    )

# Columnas a mostrar
cols_show = [
    "name",
    "partner_name",
    "invoice_date",
    "fecha_vencimiento_efectiva",
    "dias_vencido",
    "amount_total_signed",
    "amount_residual_signed",
    "payment_state",
    "tipo_real",
    "ref",
]
cols_present = [c for c in cols_show if c in open_inv.columns]

display = open_inv[cols_present].copy()
if "amount_residual_signed" in display.columns:
    display["amount_residual_signed"] = display["amount_residual_signed"].abs()
if "amount_total_signed" in display.columns:
    display["amount_total_signed"] = display["amount_total_signed"].abs()

display = display.rename(
    columns={
        "name": "Factura",
        "partner_name": "Cliente",
        "invoice_date": "Fecha factura",
        "fecha_vencimiento_efectiva": "Vence (efectivo)",
        "dias_vencido": "Días vencido",
        "amount_total_signed": "Total",
        "amount_residual_signed": "Saldo",
        "payment_state": "Estado pago",
        "tipo_real": "Tipo",
        "ref": "Ref.",
    }
).sort_values("Días vencido", ascending=False)

st.caption(
    f"Total: {len(display)} facturas | Saldo: ${display['Saldo'].sum():,.0f}  ·  "
    "**Vence (efectivo)** corrige el due nominal de Odoo cuando hay payment_term=Contado "
    "o liquidación real anticipada. **Tipo** = CONTADO/CRÉDITO según la nueva jerarquía."
)

st.dataframe(
    display,
    hide_index=True,
    use_container_width=True,
    column_config={
        "Total": st.column_config.NumberColumn(format="$%.0f"),
        "Saldo": st.column_config.NumberColumn(format="$%.0f"),
        "Días vencido": st.column_config.NumberColumn(format="%.0f"),
    },
)

# --- Export a Excel
buf = io.BytesIO()
with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
    display.to_excel(writer, sheet_name="Facturas abiertas", index=False)
    data["aging"].to_excel(writer, sheet_name="Aging", index=False)
st.download_button(
    "⬇️ Descargar Excel",
    data=buf.getvalue(),
    file_name=f"cartera_aging_{data['cutoff_date']}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)
