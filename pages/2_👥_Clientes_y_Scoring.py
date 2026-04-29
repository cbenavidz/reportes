# -*- coding: utf-8 -*-
"""Página: scoring de clientes A/B/C/D."""
import io

import pandas as pd
import streamlit as st

from src.auth import logout_button, require_auth
from src.data_loader import compute_full_analysis, filter_analysis_by_vendedor
from src.scoring import summary_by_calificacion
from src.ui_components import (
    render_company_context,
    render_score_distribution,
    render_sidebar_filters,
    render_sidebar_vendedor_filter,
)

st.set_page_config(page_title="Clientes | Cartera", page_icon="👥", layout="wide")

require_auth()
logout_button()

st.title("👥 Calificación de clientes")
st.caption(
    "Scoring A/B/C/D según hábito de pago histórico, mora y comportamiento reciente. "
    "**DSO** = promedio real de días al pago (settlement − factura), excluyendo contado, "
    "exactamente como lo calcula la página de Detalle Cliente. "
    "Mora y aging usan la fecha de vencimiento **efectiva** "
    "(payment_term explícito → settlement real → vencimiento nominal)."
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

# Filtro de vendedor (post-load)
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

scored = data["scored"]
if scored.empty:
    st.info("No hay clientes con datos suficientes para calificar.")
    st.stop()

# --- Configuración de la muestra para distribución A/B/C/D
st.markdown("##### 🧪 Muestra de análisis")
col_s1, col_s2, col_s3 = st.columns([1, 1, 2])
with col_s1:
    excluir_sin_historico = st.checkbox(
        "Excluir SIN_HISTORICO",
        value=True,
        help=(
            "Saca de la muestra a clientes sin facturas pagadas suficientes "
            "(menos del mínimo configurado). Recalcula la distribución A/B/C/D "
            "y los % sólo sobre los clientes con histórico real."
        ),
        key="excluir_sin_historico",
    )
with col_s2:
    excluir_solo_contado = st.checkbox(
        "Excluir solo contado",
        value=True,
        help=(
            "Oculta clientes que solo compran de contado (todas sus facturas "
            "son CONTADO). El scoring está diseñado para evaluar hábito de "
            "pago a crédito, así que estos clientes diluyen la lista. "
            "Desactívalo si quieres ver TODOS tus clientes."
        ),
        key="excluir_solo_contado",
    )
n_total = len(scored)
n_sin_hist = int((scored["calificacion"] == "SIN_HISTORICO").sum())
if "tipo_cliente" in scored.columns:
    n_solo_contado = int((scored["tipo_cliente"] == "CONTADO").sum())
else:
    n_solo_contado = 0

# Pool para distribución y resumen
scored_para_distrib = scored.copy()
if excluir_sin_historico:
    scored_para_distrib = scored_para_distrib[
        scored_para_distrib["calificacion"] != "SIN_HISTORICO"
    ]
if excluir_solo_contado and "tipo_cliente" in scored_para_distrib.columns:
    scored_para_distrib = scored_para_distrib[
        scored_para_distrib["tipo_cliente"] != "CONTADO"
    ]
n_muestra = len(scored_para_distrib)

with col_s3:
    partes = [f"📊 Muestra: **{n_muestra}** clientes"]
    excluidos = []
    if excluir_sin_historico and n_sin_hist:
        excluidos.append(f"{n_sin_hist} sin histórico")
    if excluir_solo_contado and n_solo_contado:
        excluidos.append(f"{n_solo_contado} solo contado")
    if excluidos:
        partes.append(f"({', '.join(excluidos)} excluidos)")
    st.caption(" · ".join(partes) + ".")

# --- Distribución
col1, col2 = st.columns([1, 2])
with col1:
    st.subheader("Distribución")
    render_score_distribution(scored_para_distrib)
with col2:
    st.subheader("Resumen por calificación")
    resumen = summary_by_calificacion(scored_para_distrib)
    st.dataframe(
        resumen,
        hide_index=True,
        use_container_width=True,
        column_config={
            "saldo_total": st.column_config.NumberColumn("Saldo total", format="$%.0f"),
            "monto_vencido": st.column_config.NumberColumn("Vencido", format="$%.0f"),
            "score_promedio": st.column_config.NumberColumn("Score prom.", format="%.1f"),
            "dias_mora_prom": st.column_config.NumberColumn("Mora prom. (días)", format="%.1f"),
            "pct_clientes": st.column_config.NumberColumn("% clientes", format="%.1f%%"),
            "pct_saldo": st.column_config.NumberColumn("% saldo", format="%.1f%%"),
        },
    )

st.markdown("---")

# --- Filtros
st.subheader("Detalle de clientes")
col_f1, col_f2, col_f3 = st.columns(3)
with col_f1:
    cal_filter = st.multiselect(
        "Calificación",
        options=["A", "B", "C", "D", "SIN_HISTORICO"],
        default=[],
    )
with col_f2:
    solo_con_saldo = st.checkbox("Solo con saldo > 0", value=True)
with col_f3:
    busqueda = st.text_input("Buscar cliente", placeholder="Nombre")

df = scored.copy()
# Aplica el toggle "excluir solo contado" también al detalle
if excluir_solo_contado and "tipo_cliente" in df.columns:
    df = df[df["tipo_cliente"] != "CONTADO"]
if cal_filter:
    df = df[df["calificacion"].isin(cal_filter)]
if solo_con_saldo:
    df = df[df["saldo_actual"] > 0]
if busqueda:
    df = df[df["partner_name"].astype(str).str.contains(busqueda, case=False, na=False)]

cols_show = [
    "calificacion",
    "score_total",
    "partner_name",
    "vat",
    "tipo_cliente",
    "saldo_actual",
    "monto_vencido",
    "plazo_promedio_dias",
    "dso_cliente",
    "dias_sobre_plazo",
    "dias_vencido_max",
    "pct_pagado_a_tiempo",
    "num_facturas_pagadas",
    "credit_limit",
    "ultimo_pago",
]
cols_present = [c for c in cols_show if c in df.columns]

display = df[cols_present].rename(
    columns={
        "calificacion": "Cal.",
        "score_total": "Score",
        "partner_name": "Cliente",
        "vat": "NIT",
        "tipo_cliente": "Tipo",
        "saldo_actual": "Saldo",
        "monto_vencido": "Vencido",
        "plazo_promedio_dias": "Plazo otorg. (d)",
        "dso_cliente": "DSO (d)",
        "dias_sobre_plazo": "Sobre plazo (d)",
        "dias_vencido_max": "Mora máx hoy (d)",
        "pct_pagado_a_tiempo": "% a tiempo",
        "num_facturas_pagadas": "# fact. pagadas",
        "credit_limit": "Límite crédito",
        "ultimo_pago": "Último pago",
    }
)

st.dataframe(
    display,
    hide_index=True,
    use_container_width=True,
    height=600,
    column_config={
        "Score": st.column_config.ProgressColumn(format="%.1f", min_value=0, max_value=100),
        "Saldo": st.column_config.NumberColumn(format="$%.0f"),
        "Vencido": st.column_config.NumberColumn(format="$%.0f"),
        "Límite crédito": st.column_config.NumberColumn(format="$%.0f"),
        "% a tiempo": st.column_config.NumberColumn(format="%.0f%%"),
        "Plazo otorg. (d)": st.column_config.NumberColumn(format="%.0f"),
        "DSO (d)": st.column_config.NumberColumn(format="%.0f"),
        "Sobre plazo (d)": st.column_config.NumberColumn(format="%+.0f"),
        "Mora máx hoy (d)": st.column_config.NumberColumn(format="%.0f"),
    },
)
st.caption(
    "💡 **DSO** = promedio real de días al pago (settlement_date − invoice_date) sobre "
    "las facturas pagadas, **excluyendo contado** — exactamente la fórmula de Detalle "
    "Cliente. **Plazo otorg.** = días de crédito promedio según el due efectivo. "
    "**Sobre plazo** = DSO − Plazo (positivo ⇒ paga después de su plazo). "
    "**Mora máx hoy** = mora actual de la factura más vencida del cliente. "
    "**% a tiempo** = porcentaje de facturas pagadas sin mora (settlement ≤ due efectivo)."
)

# Export
buf = io.BytesIO()
with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
    display.to_excel(writer, sheet_name="Clientes scoring", index=False)
    resumen.to_excel(writer, sheet_name="Resumen", index=False)

st.download_button(
    "⬇️ Descargar Excel",
    data=buf.getvalue(),
    file_name=f"clientes_scoring_{data['cutoff_date']}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)
