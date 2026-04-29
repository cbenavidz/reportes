# -*- coding: utf-8 -*-
"""
Página: Detalle por cliente.

Drill-down con KPIs, histórico mensual y gráficas de comportamiento de pago
para un único cliente. Permite afinar el análisis con un rango de fechas
independiente al de la app, para que el cálculo sea más exacto que el DSO
nativo de Odoo (que se calcula desde el inicio del cliente).
"""
from __future__ import annotations

import io
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from src.analyzer import (
    _compute_invoice_settlement_dates,
    classify_invoices_credit_vs_cash,
    compute_effective_due_date,
    compute_partner_payment_distribution,
    compute_partner_payment_timeline,
    compute_monthly_history,
    filter_partner_data,
)
from src.auth import logout_button, require_auth
from src.data_loader import compute_full_analysis, filter_analysis_by_vendedor
from src.ui_components import (
    render_company_context,
    render_history_dso,
    render_history_facturado_cobrado,
    render_history_saldo,
    render_sidebar_filters,
    render_sidebar_vendedor_filter,
)

st.set_page_config(
    page_title="Detalle cliente | Cartera",
    page_icon="🔎",
    layout="wide",
)

require_auth()
logout_button()

st.title("🔎 Detalle por cliente")
st.caption(
    "Análisis profundo de un cliente: KPIs, histórico, hábito de pago y "
    "facturas. Incluye filtro de fechas para comparar contra el DSO nativo "
    "de Odoo (que mira desde el inicio del cliente)."
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

scored = data["scored"]
if scored.empty:
    st.info("No hay clientes con datos suficientes para mostrar.")
    st.stop()

# ---------------------------------------------------------------------------
# Selector de cliente y rango de fechas
# ---------------------------------------------------------------------------
col_sel1, col_sel2, col_sel3 = st.columns([3, 2, 2])

with col_sel1:
    # Ordenar clientes por saldo descendente para que arriba aparezcan los
    # más relevantes; luego se puede buscar por nombre con el typeahead.
    cli_options = (
        scored.assign(
            etiqueta=lambda d: (
                d["partner_name"].astype(str)
                + "  ·  $"
                + d["saldo_actual"].fillna(0).astype(int).astype(str)
                + " saldo"
            )
        )
        .sort_values(["saldo_actual", "score_total"], ascending=[False, True])
    )
    label_to_id = dict(zip(cli_options["etiqueta"], cli_options["partner_id"]))
    seleccion = st.selectbox(
        "Cliente",
        options=cli_options["etiqueta"].tolist(),
        index=0,
    )
    partner_id = int(label_to_id[seleccion])

cutoff_date = pd.to_datetime(data["cutoff_date"]).date()
default_from = cutoff_date - timedelta(days=filters["months_back"] * 30)

with col_sel2:
    fecha_desde = st.date_input(
        "Desde",
        value=default_from,
        max_value=cutoff_date,
        help="Fecha inicial del estudio para este cliente.",
    )
with col_sel3:
    fecha_hasta = st.date_input(
        "Hasta",
        value=cutoff_date,
        max_value=cutoff_date,
        help="Fecha final del estudio para este cliente.",
    )

if fecha_desde > fecha_hasta:
    st.error("La fecha 'Desde' debe ser anterior a 'Hasta'.")
    st.stop()

# ---------------------------------------------------------------------------
# Datos del cliente seleccionado
# ---------------------------------------------------------------------------
cli_row = scored[scored["partner_id"] == partner_id].iloc[0].to_dict()

partner_data = filter_partner_data(
    invoices=data["raw_invoices"],
    payments=data["raw_payments"],
    open_invoices=data["open_invoices"],
    partner_id=partner_id,
    date_from=fecha_desde,
    date_to=fecha_hasta,
)

inv_p = partner_data["invoices"]
pay_p = partner_data["payments"]
open_p = partner_data["open_invoices"]

# Encabezado del cliente
st.markdown("---")
st.markdown(f"### {cli_row.get('partner_name', '—')}")
hdr_cols = st.columns(4)
hdr_cols[0].markdown(f"**NIT:** {cli_row.get('vat') or '—'}")
hdr_cols[1].markdown(f"**Calificación:** `{cli_row.get('calificacion', '—')}`")
hdr_cols[2].markdown(f"**Score:** `{cli_row.get('score_total', 0):.1f}` / 100")
hdr_cols[3].markdown(
    f"**Hábito de pago:** {cli_row.get('habito_pago', '—')}"
)

# ---------------------------------------------------------------------------
# KPIs del cliente
# ---------------------------------------------------------------------------
st.markdown("##### Indicadores clave")

# Recalcular KPIs sobre la ventana de fechas elegida.
# IMPORTANTE: para los días al pago usamos el settlement_date REAL reconstruido
# desde reconciled_invoice_ids (vínculo nativo de Odoo). NO se debe usar
# `invoice.date` como fecha de pago — ese campo es la fecha contable de la
# factura, no del pago.
settlement_p = _compute_invoice_settlement_dates(
    invoices=inv_p,
    payments=pay_p,
    exclude_cash_sales=filters["exclude_cash_sales"],
)

if not settlement_p.empty:
    # Dos due dates separadas:
    # - `due_otorgado` (sin payments): plazo del contrato, no se reclasifica
    #   por comportamiento. Un cliente con 30d sigue mostrando 30d aunque
    #   pague rápido. Lo usamos para "Plazo otorgado prom.".
    # - `due_efectivo` (con payments): además aplica override por settlement
    #   ≤3d para que facturas mal etiquetadas no muestren mora negativa.
    #   Lo usamos para mora y % a tiempo.
    due_otorg = compute_effective_due_date(inv_p, payments=None)
    due_eff = compute_effective_due_date(inv_p, payments=pay_p)
    inv_due = inv_p[["id", "invoice_date_due"]].copy()
    inv_due["due_otorgado"] = due_otorg.values
    inv_due["due_efectivo"] = due_eff.values

    # Plazo y mora se calculan factura a factura con el settlement_date
    sp = settlement_p.merge(
        inv_due.rename(columns={"id": "invoice_id"}),
        on="invoice_id",
        how="left",
    )
    sp["invoice_date_due"] = pd.to_datetime(sp["invoice_date_due"], errors="coerce")
    sp["due_otorgado"] = pd.to_datetime(sp["due_otorgado"], errors="coerce")
    sp["due_efectivo"] = pd.to_datetime(sp["due_efectivo"], errors="coerce")
    sp["plazo"] = (sp["due_otorgado"] - sp["invoice_date"]).dt.days.clip(lower=0)
    sp["dias_de_mora"] = (sp["settlement_date"] - sp["due_efectivo"]).dt.days

    dso_ventana = float(sp["dias_pago"].mean() or 0)
    mora_ventana = float(sp["dias_de_mora"].mean() or 0)
    plazo_ventana = float(sp["plazo"].mean() or 0)
    pct_a_tiempo = float((sp["dias_de_mora"] <= 0).mean() * 100)
    n_fact_pagadas = int(len(sp))
else:
    dso_ventana = mora_ventana = plazo_ventana = pct_a_tiempo = 0.0
    n_fact_pagadas = 0

dso_odoo_val = cli_row.get("dso_odoo")

k1, k2, k3, k4 = st.columns(4)
k1.metric(
    "Saldo actual",
    f"${cli_row.get('saldo_actual', 0):,.0f}",
    help="Saldo total pendiente de cobro al corte (no afectado por la ventana).",
)
k2.metric(
    "Vencido",
    f"${cli_row.get('monto_vencido', 0):,.0f}",
    delta=f"{cli_row.get('pct_vencido_cliente', 0):.0f}% del saldo",
    delta_color="inverse",
)
k3.metric(
    "Facturas abiertas",
    int(cli_row.get("num_facturas_abiertas", 0) or 0),
    help="# de facturas con saldo pendiente.",
)
k4.metric(
    "Mora máx hoy",
    f"{int(cli_row.get('dias_vencido_max', 0) or 0)} d",
    help="Días vencidos de la peor factura abierta hoy.",
)

k5, k6, k7, k8 = st.columns(4)
k5.metric(
    "DSO ventana",
    f"{dso_ventana:.0f} d" if dso_ventana else "—",
    help="Días reales de cobro promedio en la ventana de fechas seleccionada.",
)
k6.metric(
    "DSO Odoo",
    f"{dso_odoo_val:.0f} d" if dso_odoo_val and not pd.isna(dso_odoo_val) else "—",
    delta=(
        f"{dso_ventana - float(dso_odoo_val):+.1f} d vs ventana"
        if (dso_odoo_val and not pd.isna(dso_odoo_val) and dso_ventana)
        else None
    ),
    help=(
        "Campo nativo `days_sales_outstanding` de Odoo. Calculado desde el "
        "inicio del cliente — útil como referencia, pero la ventana suele "
        "ser más exacta para el comportamiento actual."
    ),
)
k7.metric(
    "% pagado a tiempo (ventana)",
    f"{pct_a_tiempo:.0f}%",
    help="% de facturas pagadas en o antes del vencimiento dentro de la ventana.",
)
k8.metric(
    "Plazo otorgado prom.",
    f"{plazo_ventana:.0f} d" if plazo_ventana else "—",
    help="Promedio de días entre fecha de factura y fecha de vencimiento.",
)

k9, k10, k11, k12 = st.columns(4)
k9.metric("Facturas pagadas (ventana)", n_fact_pagadas)
k10.metric(
    "Mora promedio (ventana)",
    f"{mora_ventana:+.1f} d",
    help="Negativo = paga antes del plazo; positivo = se pasa del plazo.",
)
limite = cli_row.get("credit_limit") or 0
saldo_actual = cli_row.get("saldo_actual") or 0
uso_cupo = (saldo_actual / limite * 100) if limite else 0
k11.metric(
    "Límite de crédito",
    f"${limite:,.0f}" if limite else "—",
    delta=f"{uso_cupo:.0f}% en uso" if limite else None,
    delta_color="inverse" if uso_cupo > 90 else "normal",
)
k12.metric(
    "Antigüedad como cliente",
    f"{int(cli_row.get('antiguedad_dias', 0) or 0)} d",
    help="Días desde la primera factura registrada.",
)

if cli_row.get("use_partner_credit_limit") is False:
    st.caption(
        "ℹ️ Este cliente tiene `use_partner_credit_limit = False` en Odoo: "
        "el sistema **no** está controlando su límite de crédito."
    )

st.markdown("---")

# ---------------------------------------------------------------------------
# Histórico mensual del cliente
# ---------------------------------------------------------------------------
st.subheader("📈 Histórico mensual del cliente")

# # de meses dentro de la ventana
delta_days = (fecha_hasta - fecha_desde).days
months_window = max(1, int(round(delta_days / 30)))

history_p = compute_monthly_history(
    invoices=inv_p,
    payments=pay_p,
    months=months_window,
    cutoff_date=fecha_hasta,
    exclude_cash_sales=filters["exclude_cash_sales"],
    open_invoices=open_p,
    # Para clientes individuales el saldo a fin de mes suele ser 0 (pagan
    # antes), así que la fórmula clásica saldo/ventas se va a 0. Usamos el
    # promedio real de días entre factura y pago de las facturas pagadas en
    # los últimos 90 días — métrica más intuitiva para per-cliente.
    dso_method="payment_days",
)

col_h1, col_h2 = st.columns([3, 2])
with col_h1:
    st.markdown("**Facturado a crédito vs. cobrado**")
    render_history_facturado_cobrado(history_p)
with col_h2:
    st.markdown("**DSO rolling 90 días**")
    render_history_dso(history_p)

st.markdown("**Saldo de cartera del cliente al cierre de cada mes**")
render_history_saldo(history_p)

st.caption(
    "El saldo es una aproximación: facturado neto acumulado − cobrado "
    "acumulado dentro de la ventana. Para una conciliación exacta hay que "
    "reconstruir desde `account.move.line`."
)

st.markdown("---")

# ---------------------------------------------------------------------------
# Distribución de hábito de pago + línea de tiempo
# ---------------------------------------------------------------------------
st.subheader("⏱️ Hábito de pago — distribución y línea de tiempo")

distrib = compute_partner_payment_distribution(inv_p, payments=pay_p)
timeline = compute_partner_payment_timeline(inv_p, payments=pay_p)

col_d1, col_d2 = st.columns(2)

with col_d1:
    st.markdown("**Distribución de días de mora (facturas pagadas)**")
    if distrib.empty or distrib["num_facturas"].sum() == 0:
        st.info("Sin facturas pagadas en la ventana seleccionada.")
    else:
        color_by_bucket = {
            "Antes del plazo (≤ -1d)": "#0ea5e9",
            "A tiempo (0d)": "#10b981",
            "1–7 días": "#84cc16",
            "8–15 días": "#facc15",
            "16–30 días": "#f97316",
            "31–60 días": "#ef4444",
            "61–90 días": "#b91c1c",
            "Más de 90 días": "#7f1d1d",
        }
        fig = go.Figure(
            go.Bar(
                x=distrib["bucket"],
                y=distrib["num_facturas"],
                marker_color=[
                    color_by_bucket.get(b, "#6b7280") for b in distrib["bucket"]
                ],
                text=distrib["num_facturas"],
                textposition="outside",
                hovertemplate=(
                    "%{x}<br>Facturas: %{y}"
                    "<br>Monto: $%{customdata:,.0f}<extra></extra>"
                ),
                customdata=distrib["monto_total"],
            )
        )
        fig.update_layout(
            height=320,
            margin=dict(l=10, r=10, t=10, b=80),
            xaxis_tickangle=-30,
            yaxis_title="# facturas",
        )
        st.plotly_chart(fig, use_container_width=True)

with col_d2:
    st.markdown("**Línea de tiempo: días de mora por factura**")
    if timeline.empty:
        st.info("Sin facturas pagadas en la ventana seleccionada.")
    else:
        # Color por gravedad de la mora
        def _mora_color(d: float) -> str:
            if d <= 0:
                return "#10b981"
            if d <= 15:
                return "#facc15"
            if d <= 45:
                return "#f97316"
            return "#ef4444"

        fig = go.Figure(
            go.Scatter(
                x=timeline["fecha_pago"],
                y=timeline["dias_de_mora"],
                mode="markers",
                marker=dict(
                    size=(timeline["monto"] / max(timeline["monto"].max(), 1) * 25 + 6),
                    color=[_mora_color(d) for d in timeline["dias_de_mora"]],
                    line=dict(width=0.5, color="#1f2937"),
                ),
                text=timeline["factura"],
                hovertemplate=(
                    "<b>%{text}</b><br>Pagada: %{x|%Y-%m-%d}"
                    "<br>Mora: %{y:+d} d"
                    "<br>Monto: $%{customdata:,.0f}<extra></extra>"
                ),
                customdata=timeline["monto"],
            )
        )
        fig.add_hline(y=0, line_dash="dash", line_color="#94a3b8")
        fig.update_layout(
            height=320,
            margin=dict(l=10, r=10, t=10, b=10),
            xaxis_title=None,
            yaxis_title="Días de mora",
            showlegend=False,
        )
        st.plotly_chart(fig, use_container_width=True)
        st.caption(
            "Cada burbuja = una factura pagada. Tamaño = monto. "
            "Bajo la línea 0 = pagó antes del vencimiento. "
            "Verde/amarillo/naranja/rojo = severidad de la mora."
        )

st.markdown("---")

# ---------------------------------------------------------------------------
# Tabla de facturas
# ---------------------------------------------------------------------------
st.subheader("📋 Facturas en la ventana")

if inv_p.empty:
    st.info("No hay facturas para este cliente en la ventana seleccionada.")
else:
    df_fact = inv_p.copy()
    df_fact["monto"] = df_fact["amount_total_signed"].abs()
    df_fact["saldo"] = df_fact["amount_residual_signed"].abs()

    # Vincular settlement_date real (último pago) por factura.
    if not settlement_p.empty:
        merge_cols = settlement_p[["invoice_id", "settlement_date", "dias_pago"]].rename(
            columns={"invoice_id": "id"}
        )
        df_fact = df_fact.merge(merge_cols, on="id", how="left")
    else:
        df_fact["settlement_date"] = pd.NaT
        df_fact["dias_pago"] = pd.NA

    # Tipo real (CONTADO / CRÉDITO): combina payment_term + settlement.
    # Útil para detectar facturas mal etiquetadas que estaban inflando DSO.
    is_credit_real = classify_invoices_credit_vs_cash(df_fact, payments=pay_p)
    df_fact["tipo_real"] = pd.Series(
        ["CRÉDITO" if c else "CONTADO" for c in is_credit_real],
        index=df_fact.index,
    )

    # Plazo NOMINAL del documento = invoice_date_due − invoice_date.
    # Es el plazo que dice el `payment_term` asignado a la factura
    # (puede no coincidir con el comportamiento real, pero es el dato
    # acordado en el documento).
    df_fact["plazo_dias"] = (
        pd.to_datetime(df_fact["invoice_date_due"], errors="coerce")
        - pd.to_datetime(df_fact["invoice_date"], errors="coerce")
    ).dt.days.fillna(0).astype("Int64")

    # Due efectivo y días de mora REAL contra ese due:
    # - CONTADO: due_efectivo = invoice_date → mora = días al pago.
    # - CRÉDITO: due_efectivo = invoice_date_due → mora = settlement vs vencimiento.
    df_fact["due_efectivo"] = compute_effective_due_date(
        df_fact, payments=pay_p
    ).values
    df_fact["dias_de_mora"] = (
        pd.to_datetime(df_fact["settlement_date"], errors="coerce")
        - pd.to_datetime(df_fact["due_efectivo"], errors="coerce")
    ).dt.days

    cols_fact = [
        "name",
        "invoice_date",
        "invoice_date_due",
        "plazo_dias",
        "payment_term_name",
        "settlement_date",
        "monto",
        "saldo",
        "payment_state",
        "dias_pago",
        "dias_de_mora",
        "tipo_real",
        "move_type",
    ]
    cols_fact = [c for c in cols_fact if c in df_fact.columns]

    show = df_fact[cols_fact].rename(
        columns={
            "name": "Factura",
            "invoice_date": "Fecha factura",
            "invoice_date_due": "Vencimiento",
            "plazo_dias": "Plazo (d)",
            "payment_term_name": "Término pago",
            "settlement_date": "Fecha liquidación",
            "monto": "Monto",
            "saldo": "Saldo",
            "payment_state": "Estado pago",
            "dias_pago": "Días al pago",
            "dias_de_mora": "Días de mora",
            "tipo_real": "Tipo real",
            "move_type": "Tipo",
        }
    ).sort_values("Fecha factura", ascending=False)

    st.dataframe(
        show,
        hide_index=True,
        use_container_width=True,
        height=420,
        column_config={
            "Monto": st.column_config.NumberColumn(format="$%,.0f"),
            "Saldo": st.column_config.NumberColumn(format="$%,.0f"),
            "Plazo (d)": st.column_config.NumberColumn(
                format="%d",
                help="Plazo nominal otorgado en el documento "
                     "(invoice_date_due − invoice_date).",
            ),
            "Término pago": st.column_config.TextColumn(
                help="Nombre del `account.payment.term` asignado a la factura.",
            ),
            "Días al pago": st.column_config.NumberColumn(format="%d"),
            "Días de mora": st.column_config.NumberColumn(format="%+d"),
        },
    )
    st.caption(
        "**Plazo (d)** = días otorgados según el documento (vencimiento − "
        "fecha factura). **Término pago** = nombre del término asignado en "
        "Odoo. **Fecha liquidación** = fecha del último pago vinculado a la "
        "factura (`reconciled_invoice_ids`). **Días al pago** = fecha "
        "liquidación − fecha factura. **Tipo real** combina el `payment_term` "
        "con el comportamiento real: CONTADO si el término dice contado o si "
        "se liquidó en ≤3 días, CRÉDITO en otro caso. **Días de mora** se "
        "calcula contra el vencimiento *efectivo* (= fecha factura para "
        "CONTADO real, = vencimiento Odoo para CRÉDITO real), no contra el "
        "`due` nominal — así una factura de contado mal etiquetada con plazo "
        "+30d ya no aparece con −30 días de mora."
    )

# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
buf = io.BytesIO()
with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
    pd.DataFrame([cli_row]).to_excel(writer, sheet_name="Resumen", index=False)
    if not inv_p.empty:
        inv_p.to_excel(writer, sheet_name="Facturas", index=False)
    if not pay_p.empty:
        pay_p.to_excel(writer, sheet_name="Pagos", index=False)
    if not history_p.empty:
        history_p.to_excel(writer, sheet_name="Histórico mensual", index=False)
    if not distrib.empty:
        distrib.to_excel(writer, sheet_name="Distrib. mora", index=False)

st.download_button(
    "⬇️ Descargar reporte del cliente",
    data=buf.getvalue(),
    file_name=(
        f"detalle_{(cli_row.get('partner_name') or 'cliente').replace(' ', '_')}_"
        f"{fecha_desde}_a_{fecha_hasta}.xlsx"
    ),
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)
