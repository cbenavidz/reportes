# -*- coding: utf-8 -*-
"""
Página: Informe de Caja diario.

Replica del Estado de Caja de Casa de los Mineros:
  - Saldo inicial / débitos / créditos / saldo final por cuenta 1105.*
  - Movimientos del día agrupados por tipo de comprobante (journal).
  - Resumen por cuenta y resumen por formas de pago.

Filtro de fecha (por defecto hoy) y de empresa. Botones para descargar
PDF y enviar por correo (ver `src.daily_pdf`).
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from src.auth import logout_button, require_auth
from src.cash_report import (
    build_saldo_inicial_dict,
    compute_estado_caja,
    get_cash_accounts,
)
from src.data_loader import (
    load_account_balances_aggregated,
    load_cash_movements_only,
    load_chart_of_accounts,
    load_companies,
)
from src.ui_components import render_company_context, render_sidebar_filters

st.set_page_config(
    page_title="Informe de Caja | Cartera",
    page_icon="💵", layout="wide",
)
require_auth()
logout_button()

st.title("💵 Informe de Caja")
st.caption(
    "Estado de Caja diario por cuenta 1105.* — saldo inicial y final, "
    "movimientos del día agrupados por tipo de comprobante."
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


# ── Helpers de formato ──
def fmt_money(x) -> str:
    try:
        return f"${x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return "$0,00"


# ── Carga de datos ──
with st.spinner("Cargando plan de cuentas..."):
    chart = load_chart_of_accounts(company_ids=company_ids)

cash_accs = get_cash_accounts(chart)
if cash_accs.empty:
    st.info(
        "No se encontraron cuentas que empiecen con 1105 en el plan "
        "de cuentas. Verifica el PUC."
    )
    st.stop()

cash_account_ids = tuple(int(x) for x in cash_accs["id"].tolist())
fecha_str = fecha.isoformat()
ant_str = (fecha - timedelta(days=1)).isoformat()

with st.spinner("Cargando movimientos del día..."):
    moves_dia = load_cash_movements_only(
        date_from=fecha_str, date_to=fecha_str,
        company_ids=company_ids, cash_account_ids=cash_account_ids,
    )

with st.spinner("Calculando saldos iniciales..."):
    balances = load_account_balances_aggregated(
        date_to=ant_str, company_ids=company_ids,
    )
    saldo_inicial = build_saldo_inicial_dict(balances, set(cash_account_ids))

estado = compute_estado_caja(chart, moves_dia, saldo_inicial, fecha)


# ── Render ──
st.markdown(f"### 📅 Estado de Caja — {fecha.strftime('%d/%m/%Y')}")

if not estado["cuentas"]:
    st.info("No hay movimientos ni saldos en cuentas de caja para esta fecha.")
    st.stop()

# KPIs cabecera
k1, k2, k3, k4 = st.columns(4)
total_si = sum(c["saldo_inicial"] for c in estado["cuentas"])
total_deb = sum(c["debitos"] for c in estado["cuentas"])
total_cre = sum(c["creditos"] for c in estado["cuentas"])
total_sf = sum(c["saldo_final"] for c in estado["cuentas"])
k1.metric("Saldo inicial", fmt_money(total_si))
k2.metric("(+) Débitos", fmt_money(total_deb))
k3.metric("(−) Créditos", fmt_money(total_cre))
k4.metric("Saldo final", fmt_money(total_sf))

st.markdown("---")

# Una sección por cuenta
for cta in estado["cuentas"]:
    st.markdown(f"#### 💼 {cta['code']} — {cta['name']}")
    sc1, sc2, sc3, sc4 = st.columns(4)
    sc1.metric("Saldo inicial", fmt_money(cta["saldo_inicial"]))
    sc2.metric("(+) Débitos", fmt_money(cta["debitos"]))
    sc3.metric("(−) Créditos", fmt_money(cta["creditos"]))
    sc4.metric("Saldo final", fmt_money(cta["saldo_final"]))

    if not cta["grupos"]:
        st.caption("Sin movimientos en esta cuenta en la fecha.")
        st.markdown("---")
        continue

    # Movimientos agrupados por tipo de comprobante
    for g in cta["grupos"]:
        st.markdown(f"**{g['journal_name']}**")
        df_g = pd.DataFrame(g["lineas"])
        if df_g.empty:
            continue
        st.dataframe(
            df_g, use_container_width=True, hide_index=True,
            column_config={
                "comprobante": st.column_config.TextColumn(
                    "Comprobante", width="small",
                ),
                "contacto": st.column_config.TextColumn(
                    "Contacto", width="medium",
                ),
                "referencia": st.column_config.TextColumn(
                    "Referencia / etiqueta", width="large",
                ),
                "valor": st.column_config.NumberColumn(
                    "Valor", format="localized",
                ),
            },
            column_order=["comprobante", "contacto", "referencia", "valor"],
        )
        st.caption(f"Subtotal {g['journal_name']}: **{fmt_money(g['subtotal'])}**")

    st.caption(
        f"Total flujo/movimiento de caja: **{fmt_money(cta['flujo_neto'])}**"
    )
    st.markdown("---")

# Resumen por cuenta
if not estado["resumen_cuentas"].empty:
    st.markdown("### 📋 Resumen por cuentas")
    rc = estado["resumen_cuentas"].copy()
    st.dataframe(
        rc, use_container_width=True, hide_index=True,
        column_config={
            "code": st.column_config.TextColumn("Cuenta"),
            "name": st.column_config.TextColumn("Nombre", width="large"),
            "saldo_inicial": st.column_config.NumberColumn(
                "Saldo inicial", format="localized",
            ),
            "debitos": st.column_config.NumberColumn(
                "Débitos", format="localized",
            ),
            "creditos": st.column_config.NumberColumn(
                "Créditos", format="localized",
            ),
            "saldo_final": st.column_config.NumberColumn(
                "Saldo final", format="localized",
            ),
        },
    )

# Resumen por formas de pago
if not estado["resumen_formas_pago"].empty:
    st.markdown("### 💳 Resumen por formas de pago")
    rfp = estado["resumen_formas_pago"].copy()
    st.dataframe(
        rfp, use_container_width=True, hide_index=True,
        column_config={
            "detalle": st.column_config.TextColumn(
                "Detalle", width="large",
            ),
            "valor": st.column_config.NumberColumn(
                "Valor", format="localized",
            ),
        },
    )

st.markdown(
    f"### 💰 Total flujo del día: {fmt_money(estado['total_flujo'])}"
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
