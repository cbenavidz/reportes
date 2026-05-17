# -*- coding: utf-8 -*-
"""
Página: Estados Financieros — Balance, P&L, KTNO, Flujo, Gastos.

Reportes contables completos basados en TODAS las cuentas del libro mayor.
Usa clasificación PUC colombiano (1=Activo, 2=Pasivo, 3=Patrimonio,
4=Ingresos, 5/6/7=Costos/Gastos).
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from src.auth import logout_button, require_auth
from src.data_loader import (
    load_account_balances_aggregated,
    load_account_balances_monthly,
    load_account_movements,
    load_cash_movements_only,
    load_chart_of_accounts,
    load_companies,
    load_expense_movements,
)
from src.ui_components import render_company_context, render_sidebar_filters
from src.financial_statements import (
    compute_balance_sheet,
    compute_cash_flow,
    compute_expenses_breakdown,
    compute_expenses_comparative,
    compute_income_statement,
    compute_pnl_monthly_evolution,
    compute_working_capital,
    enrich_chart_with_puc,
)

st.set_page_config(
    page_title="Estados Financieros | Cartera",
    page_icon="📈",
    layout="wide",
)

require_auth()
logout_button()

st.title("📈 Estados Financieros")
st.caption(
    "Estado de Resultados, Balance General, KTNO, Capital de Trabajo, "
    "Flujo de Efectivo y Análisis de Gastos. Basado en el plan de cuentas "
    "PUC colombiano y movimientos de TODAS las cuentas contables."
)

# Sidebar filtros
filters = render_sidebar_filters()
if filters["company_ids"] is not None and len(filters["company_ids"]) == 0:
    st.warning("Selecciona al menos una empresa.")
    st.stop()


# ---------------------------------------------------------------------------
# Período
# ---------------------------------------------------------------------------
st.markdown("### 🗓️ Período de análisis")
col_p1, col_p2, col_p3 = st.columns([1, 1, 2])
today = date.today()

with col_p1:
    fecha_desde = st.date_input(
        "Desde", value=today.replace(month=1, day=1), key="ef_desde"
    )
with col_p2:
    fecha_hasta = st.date_input("Hasta (corte)", value=today, key="ef_hasta")
with col_p3:
    quick = st.radio(
        "Atajos",
        options=["Personalizado", "Mes actual", "Mes anterior", "Trimestre actual",
                 "Año en curso", "Año anterior", "Últimos 12 meses"],
        index=1, horizontal=False, key="ef_atajo",
        help="Períodos cortos cargan más rápido. 12 meses puede tardar 2-3 min."
    )

if quick != "Personalizado":
    if quick == "Año en curso":
        fecha_desde, fecha_hasta = today.replace(month=1, day=1), today
    elif quick == "Año anterior":
        fecha_desde = date(today.year - 1, 1, 1)
        fecha_hasta = date(today.year - 1, 12, 31)
    elif quick == "Últimos 12 meses":
        fecha_desde, fecha_hasta = today - timedelta(days=365), today
    elif quick == "Mes actual":
        fecha_desde, fecha_hasta = today.replace(day=1), today
    elif quick == "Mes anterior":
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        fecha_desde, fecha_hasta = last_prev.replace(day=1), last_prev
    elif quick == "Trimestre actual":
        q_start_month = ((today.month - 1) // 3) * 3 + 1
        fecha_desde = today.replace(month=q_start_month, day=1)
        fecha_hasta = today

st.caption(f"📅 Período: **{fecha_desde}** → **{fecha_hasta}** · Corte balance: **{fecha_hasta}**")


# ---------------------------------------------------------------------------
# Cargar datos — SOLO el período seleccionado + período anterior comparativo
# ---------------------------------------------------------------------------
# Esto reduce dramáticamente el tiempo de carga vs cargar 12-36 meses fijo
periodo_dias = (fecha_hasta - fecha_desde).days + 1
fecha_desde_prev = fecha_desde - timedelta(days=periodo_dias)
fecha_hasta_prev = fecha_desde - timedelta(days=1)

# Rango para cargar: desde inicio del período anterior hasta fin del actual
load_date_from = fecha_desde_prev.isoformat()
load_date_to = fecha_hasta.isoformat()

with st.spinner("Cargando plan de cuentas..."):
    try:
        chart = load_chart_of_accounts(
            company_ids=filters["company_ids"],
        )
    except Exception as exc:  # noqa: BLE001
        st.error(
            f"❌ Error cargando plan de cuentas:\n\n```\n{exc}\n```"
        )
        st.stop()

# OPTIMIZACIÓN: usar read_group server-side para TODO en lugar de traer
# líneas individuales. Esto reduce el tiempo de minutos a segundos.
with st.spinner("Calculando saldos del período..."):
    # Saldos del período actual (para P&L)
    balances_periodo = load_account_balances_aggregated(
        date_from=fecha_desde.isoformat(),
        date_to=fecha_hasta.isoformat(),
        company_ids=filters["company_ids"],
    )
    # Saldos del período anterior (para comparativa)
    balances_periodo_prev = load_account_balances_aggregated(
        date_from=fecha_desde_prev.isoformat(),
        date_to=fecha_hasta_prev.isoformat(),
        company_ids=filters["company_ids"],
    )
    # Saldos HISTÓRICOS acumulados (para Balance General)
    balances_hist = load_account_balances_aggregated(
        date_to=fecha_hasta.isoformat(),
        company_ids=filters["company_ids"],
    )

# Para flujo de efectivo necesitamos líneas individuales, pero SOLO de
# cuentas de caja/bancos. Las identificamos del chart (PUC 11xx o
# account_type=asset_cash) y filtramos. `moves` queda vacío aquí — la
# pestaña de Flujo lo carga bajo demanda.
moves = pd.DataFrame()

# Validación: si no hay NI plan de cuentas NI balances, no hay datos.
if (chart is None or chart.empty) and (
    balances_periodo is None or balances_periodo.empty
) and (balances_hist is None or balances_hist.empty):
    st.error(
        "No se pudieron cargar datos contables. Verifica la conexión con "
        "Odoo y que el período tenga movimientos."
    )
    st.stop()

# Banner de empresa (carga ligera, solo lista de empresas)
companies_df = load_companies()
render_company_context(companies_df, filters["company_ids"])

st.success(
    f"✅ {len(chart):,} cuentas en el plan · "
    f"{len(balances_periodo):,} cuentas con movimiento en el período · "
    f"{len(balances_hist):,} cuentas en el balance histórico"
)

# Diagnóstico (expandible)
with st.expander("🔍 Diagnóstico de datos cargados", expanded=False):
    cdiag1, cdiag2 = st.columns(2)

    with cdiag1:
        st.markdown("**Plan de cuentas (chart)**")
        st.write(f"Columnas: `{list(chart.columns)}`")
        st.write(f"Total cuentas: {len(chart):,}")
        if not chart.empty and "code" in chart.columns:
            st.write("Primeras 5 cuentas:")
            st.dataframe(chart.head(5), hide_index=True)
            # Distribución por primer dígito
            chart["_d1"] = chart["code"].astype(str).str[:1]
            dist = chart["_d1"].value_counts().sort_index()
            st.write(f"Distribución por primer dígito del código:")
            st.write(dist.to_dict())

    with cdiag2:
        st.markdown("**Balances agregados (read_group server-side)**")
        st.write(
            f"Cuentas con movimiento en el período: {len(balances_periodo):,}"
        )
        st.write(
            f"Cuentas en el balance histórico: {len(balances_hist):,}"
        )
        if not balances_periodo.empty:
            st.write("Muestra de balances del período:")
            st.dataframe(balances_periodo.head(5), hide_index=True)

    # Diagnóstico: cuentas con saldo en el período, con su código PUC
    st.markdown("---")
    st.markdown("**🎯 Cuentas con saldo en el período (con código y clasificación)**")
    if (
        "id" in chart.columns
        and balances_periodo is not None
        and not balances_periodo.empty
    ):
        from src.financial_statements import enrich_chart_with_puc
        chart_e = enrich_chart_with_puc(chart)
        chart_min = chart_e[[
            c for c in ["id", "code", "name", "account_type",
                        "puc_group", "puc_subgroup", "grupo"]
            if c in chart_e.columns
        ]].rename(columns={
            "id": "account_id", "code": "acc_code", "name": "acc_name",
        })
        bal_with_code = balances_periodo.merge(
            chart_min, on="account_id", how="left"
        )
        if not bal_with_code.empty:
            bal_with_code["saldo_neto"] = (
                bal_with_code.get("debit", 0).fillna(0)
                - bal_with_code.get("credit", 0).fillna(0)
            )
            bal_with_code["abs_saldo"] = bal_with_code["saldo_neto"].abs()
            top30 = bal_with_code.sort_values(
                "abs_saldo", ascending=False
            ).head(30)
            cols_show = [c for c in [
                "acc_code", "acc_name", "account_type", "puc_group",
                "grupo", "debit", "credit", "saldo_neto",
            ] if c in top30.columns]
            st.write("Top 30 cuentas por saldo absoluto en el período:")
            st.dataframe(
                top30[cols_show],
                use_container_width=True, hide_index=True, height=400,
            )
            # Distribución por puc_group
            if "puc_group" in bal_with_code.columns:
                dist = bal_with_code.groupby(
                    "puc_group", dropna=False
                ).agg(
                    n_cuentas=("account_id", "count"),
                    saldo_total=("saldo_neto", "sum"),
                ).reset_index()
                st.write("**Distribución por grupo PUC:**")
                st.dataframe(dist, hide_index=True)


def _money(v: float) -> str:
    return f"${v:,.0f}"


def _pct(v: float) -> str:
    return f"{v:.1f}%"


# ---------------------------------------------------------------------------
# TABS para organizar
# ---------------------------------------------------------------------------
tab_pnl, tab_bal, tab_ktno, tab_flujo, tab_gastos, tab_comp = st.tabs([
    "📊 Estado de Resultados",
    "🏦 Balance General",
    "💧 KTNO y Capital de Trabajo",
    "💰 Flujo de Efectivo",
    "💸 Gastos",
    "🔄 Comparativos",
])


# ---------------------------------------------------------------------------
# TAB 1: Estado de Resultados (P&L)
# ---------------------------------------------------------------------------
with tab_pnl:
    st.markdown("## 📊 Estado de Resultados")
    st.caption(f"Período: {fecha_desde} → {fecha_hasta}")

    pnl = compute_income_statement(
        moves, chart, fecha_desde, fecha_hasta,
        balances_aggregated=balances_periodo,
    )

    # Período anterior (mismo número de días) para comparativa
    pnl_prev = compute_income_statement(
        moves, chart, fecha_desde_prev, fecha_hasta_prev,
        balances_aggregated=balances_periodo_prev,
    )

    def _delta(actual: float, anterior: float) -> str | None:
        if not anterior:
            return None
        pct = (actual - anterior) / abs(anterior) * 100
        return f"{pct:+.1f}%"

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("💰 Ingresos operacionales", _money(pnl["ingresos_operacionales"]),
              delta=_delta(pnl["ingresos_operacionales"], pnl_prev["ingresos_operacionales"]))
    c2.metric("📦 Costo de ventas", _money(pnl["costo_ventas"]),
              delta=_delta(pnl["costo_ventas"], pnl_prev["costo_ventas"]))
    c3.metric("📊 Utilidad Bruta", _money(pnl["utilidad_bruta"]),
              delta=_delta(pnl["utilidad_bruta"], pnl_prev["utilidad_bruta"]))
    c4.metric("📈 Margen Bruto %", _pct(pnl["margen_bruto_pct"]))

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("🏢 Gastos admin.", _money(pnl["gastos_admin"]))
    c6.metric("🛒 Gastos ventas", _money(pnl["gastos_ventas"]))
    c7.metric("🎯 Utilidad Operacional", _money(pnl["utilidad_operacional"]))
    c8.metric("📈 Margen Operacional %", _pct(pnl["margen_operacional_pct"]))

    c9, c10, c11, c12 = st.columns(4)
    c9.metric("➕ Ingresos no op.", _money(pnl["ingresos_no_operacionales"]))
    c10.metric("➖ Gastos no op.", _money(pnl["gastos_no_operacionales"]))
    c11.metric("🧾 Impuesto renta", _money(pnl["impuesto_renta"]))
    c12.metric("💵 Utilidad Neta", _money(pnl["utilidad_neta"]),
               delta=_delta(pnl["utilidad_neta"], pnl_prev["utilidad_neta"]))

    # Estructura visual del P&L tipo cascada
    st.markdown("### 📉 Cascada del Estado de Resultados")
    fig = go.Figure(go.Waterfall(
        x=["Ingresos op.", "Costo ventas", "Utilidad Bruta",
           "Gastos admin.", "Gastos ventas", "Utilidad Operacional",
           "Otros ingresos", "Otros gastos", "Impuesto", "Utilidad Neta"],
        measure=["relative", "relative", "total",
                 "relative", "relative", "total",
                 "relative", "relative", "relative", "total"],
        y=[pnl["ingresos_operacionales"], -pnl["costo_ventas"], 0,
           -pnl["gastos_admin"], -pnl["gastos_ventas"], 0,
           pnl["ingresos_no_operacionales"], -pnl["gastos_no_operacionales"],
           -pnl["impuesto_renta"], 0],
        connector={"line": {"color": "#94a3b8"}},
        increasing={"marker": {"color": "#10b981"}},
        decreasing={"marker": {"color": "#ef4444"}},
        totals={"marker": {"color": "#3b82f6"}},
    ))
    fig.update_layout(height=420, margin=dict(l=0, r=0, t=20, b=0),
                      yaxis=dict(tickformat=",.0f"))
    st.plotly_chart(fig, use_container_width=True)

    # Detalle por cuenta
    if not pnl["tabla_detalle"].empty:
        with st.expander("📋 Detalle por cuenta contable", expanded=False):
            st.dataframe(
                pnl["tabla_detalle"],
                column_config={
                    "account_code": "Código",
                    "account_name": st.column_config.TextColumn("Cuenta", width="large"),
                    "subgrupo": "Subgrupo",
                    "monto": st.column_config.NumberColumn("Monto", format="$%,.0f"),
                },
                use_container_width=True, hide_index=True, height=500,
            )


# ---------------------------------------------------------------------------
# TAB 2: Balance General
# ---------------------------------------------------------------------------
with tab_bal:
    st.markdown("## 🏦 Balance General")
    st.caption(f"Corte a: {fecha_hasta}")

    bs = compute_balance_sheet(moves, chart, fecha_hasta, balances_hist=balances_hist)

    c1, c2, c3 = st.columns(3)
    c1.metric("💼 Activo Total", _money(bs["activo_total"]))
    c2.metric("🔴 Pasivo Total", _money(bs["pasivo_total"]))
    c3.metric("🟢 Patrimonio", _money(bs["patrimonio"]))

    c4, c5, c6 = st.columns(3)
    c4.metric("🟦 Activo Corriente", _money(bs["activo_corriente"]))
    c5.metric("🟥 Pasivo Corriente", _money(bs["pasivo_corriente"]))
    razon = bs["activo_corriente"] / bs["pasivo_corriente"] if bs["pasivo_corriente"] else 0
    c6.metric("📊 Razón Corriente", f"{razon:.2f}",
              help="Activo Corriente / Pasivo Corriente. >1 = liquidez positiva")

    # Validación ecuación contable
    diferencia = bs["activo_total"] - bs["pasivo_patrimonio_total"]
    if abs(diferencia) > 1:
        st.warning(
            f"⚠️ La ecuación contable no cuadra exactamente: "
            f"Activo ({_money(bs['activo_total'])}) ≠ "
            f"Pasivo + Patrimonio ({_money(bs['pasivo_patrimonio_total'])}). "
            f"Diferencia: {_money(diferencia)}. Esto puede deberse a "
            "saldos iniciales fuera del rango cargado o cuentas no clasificadas."
        )

    col_a, col_b = st.columns(2)

    with col_a:
        st.markdown("### 💼 Activos")
        if not bs["tabla_activo"].empty:
            st.dataframe(
                bs["tabla_activo"][["account_code", "account_name", "subgrupo",
                                     "es_corriente", "saldo"]],
                column_config={
                    "account_code": st.column_config.TextColumn("Código", width="small"),
                    "account_name": st.column_config.TextColumn("Cuenta", width="large"),
                    "subgrupo": st.column_config.TextColumn("Subgrupo", width="medium"),
                    "es_corriente": st.column_config.CheckboxColumn("Corr."),
                    "saldo": st.column_config.NumberColumn("Saldo", format="$%,.0f"),
                },
                use_container_width=True, hide_index=True, height=400,
            )

    with col_b:
        st.markdown("### 🔴 Pasivos + Patrimonio")
        if not bs["tabla_pasivo"].empty:
            st.dataframe(
                bs["tabla_pasivo"][["account_code", "account_name", "subgrupo",
                                     "es_corriente", "saldo"]],
                column_config={
                    "account_code": st.column_config.TextColumn("Código", width="small"),
                    "account_name": st.column_config.TextColumn("Cuenta", width="large"),
                    "subgrupo": st.column_config.TextColumn("Subgrupo", width="medium"),
                    "es_corriente": st.column_config.CheckboxColumn("Corr."),
                    "saldo": st.column_config.NumberColumn("Saldo", format="$%,.0f"),
                },
                use_container_width=True, hide_index=True, height=200,
            )
        if not bs["tabla_patrimonio"].empty:
            st.dataframe(
                bs["tabla_patrimonio"][["account_code", "account_name",
                                          "subgrupo", "saldo"]],
                column_config={
                    "account_code": st.column_config.TextColumn("Código", width="small"),
                    "account_name": st.column_config.TextColumn("Cuenta", width="large"),
                    "subgrupo": st.column_config.TextColumn("Subgrupo", width="medium"),
                    "saldo": st.column_config.NumberColumn("Saldo", format="$%,.0f"),
                },
                use_container_width=True, hide_index=True, height=200,
            )


# ---------------------------------------------------------------------------
# TAB 3: KTNO + Capital de Trabajo
# ---------------------------------------------------------------------------
with tab_ktno:
    st.markdown("## 💧 KTNO y Capital de Trabajo")
    st.caption(f"Corte a: {fecha_hasta}")

    wc = compute_working_capital(moves, chart, fecha_hasta, balances_hist=balances_hist)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("💧 Capital de Trabajo (KT)", _money(wc["kt"]),
              help="Activo Corriente − Pasivo Corriente")
    c2.metric("🎯 KTNO", _money(wc["ktno"]),
              help="CxC + Inventarios − Proveedores")
    c3.metric("📊 Razón Corriente", f"{wc['razon_corriente']:.2f}",
              help=">1 = liquidez positiva")
    c4.metric("🧪 Prueba Ácida", f"{wc['prueba_acida']:.2f}",
              help="(Activo Corriente − Inv.) / Pasivo Corriente. >1 ideal")

    st.markdown("### 🔧 Componentes del KTNO")
    c5, c6, c7, c8 = st.columns(4)
    c5.metric("👥 Cuentas por Cobrar", _money(wc["cxc"]))
    c6.metric("📦 Inventarios", _money(wc["inventario"]))
    c7.metric("🏪 Proveedores", _money(wc["proveedores"]))
    c8.metric("💵 Disponible", _money(wc["disponible"]))

    # Visual: composición del activo corriente vs pasivo corriente
    st.markdown("### 📊 Estructura financiera de corto plazo")
    fig = go.Figure()
    fig.add_trace(go.Bar(
        name="Activo corriente", x=["Estructura"], y=[wc["activo_corriente"]],
        marker_color="#10b981",
        text=[_money(wc["activo_corriente"])],
        textposition="outside",
    ))
    fig.add_trace(go.Bar(
        name="Pasivo corriente", x=["Estructura"], y=[wc["pasivo_corriente"]],
        marker_color="#ef4444",
        text=[_money(wc["pasivo_corriente"])],
        textposition="outside",
    ))
    fig.update_layout(
        barmode="group", height=350, margin=dict(l=0, r=0, t=20, b=0),
        yaxis=dict(tickformat=",.0f"),
    )
    st.plotly_chart(fig, use_container_width=True)

    # Visual: composición del KTNO
    st.markdown("### 🎯 Composición del KTNO")
    ktno_data = pd.DataFrame([
        {"componente": "+ CxC clientes", "monto": wc["cxc"]},
        {"componente": "+ Inventarios", "monto": wc["inventario"]},
        {"componente": "− Proveedores", "monto": -wc["proveedores"]},
        {"componente": "= KTNO", "monto": wc["ktno"]},
    ])
    fig2 = go.Figure(go.Waterfall(
        x=ktno_data["componente"],
        measure=["relative", "relative", "relative", "total"],
        y=ktno_data["monto"],
        text=[_money(v) for v in ktno_data["monto"]],
        textposition="outside",
        increasing={"marker": {"color": "#10b981"}},
        decreasing={"marker": {"color": "#ef4444"}},
        totals={"marker": {"color": "#3b82f6"}},
    ))
    fig2.update_layout(height=350, margin=dict(l=0, r=0, t=20, b=0),
                       yaxis=dict(tickformat=",.0f"))
    st.plotly_chart(fig2, use_container_width=True)

    # Interpretación
    if wc["razon_corriente"] >= 1.5:
        st.success(
            f"✅ **Liquidez sana** — Razón corriente {wc['razon_corriente']:.2f}. "
            "Tienes activos corrientes suficientes para cubrir pasivos a corto plazo."
        )
    elif wc["razon_corriente"] >= 1.0:
        st.info(
            f"ℹ️ **Liquidez ajustada** — Razón corriente {wc['razon_corriente']:.2f}. "
            "Aceptable pero hay poco margen. Vigila el flujo de caja."
        )
    else:
        st.warning(
            f"⚠️ **Riesgo de liquidez** — Razón corriente {wc['razon_corriente']:.2f}. "
            "Tus pasivos corrientes superan los activos corrientes. "
            "Considera renegociar plazos o capitalizar la empresa."
        )


# ---------------------------------------------------------------------------
# TAB 4: Flujo de Efectivo
# ---------------------------------------------------------------------------
with tab_flujo:
    st.markdown("## 💰 Flujo de Efectivo")
    st.caption(
        f"Movimientos de cuentas de disponible (caja/bancos) entre "
        f"{fecha_desde} y {fecha_hasta}."
    )

    # OPTIMIZACIÓN: solo cargar líneas de cuentas de caja/bancos
    # Identificamos esas cuentas del chart (account_type=asset_cash o PUC 11)
    from src.financial_statements import enrich_chart_with_puc
    chart_e = enrich_chart_with_puc(chart)
    cash_account_ids: list[int] = []
    if "puc_subgroup" in chart_e.columns:
        mask = chart_e["puc_subgroup"].astype(str) == "11"
        cash_account_ids = chart_e.loc[mask, "id"].dropna().astype(int).tolist()
    if "account_type" in chart_e.columns:
        mask = chart_e["account_type"] == "asset_cash"
        cash_account_ids += chart_e.loc[mask, "id"].dropna().astype(int).tolist()
    cash_account_ids = list(set(cash_account_ids))

    with st.spinner(f"Cargando movimientos de {len(cash_account_ids)} cuentas de caja..."):
        cash_moves = load_cash_movements_only(
            date_from=fecha_desde.isoformat(),
            date_to=fecha_hasta.isoformat(),
            company_ids=filters["company_ids"],
            cash_account_ids=tuple(cash_account_ids) if cash_account_ids else None,
        )

    cf = compute_cash_flow(cash_moves, chart, fecha_desde, fecha_hasta)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("📥 Entradas", _money(cf["entradas"]))
    c2.metric("📤 Salidas", _money(cf["salidas"]))
    c3.metric(
        "📊 Flujo Neto", _money(cf["neto"]),
        delta=("positivo" if cf["neto"] >= 0 else "negativo"),
        delta_color=("normal" if cf["neto"] >= 0 else "inverse"),
    )
    c4.metric("💵 Saldo final caja/banco", _money(cf["saldo_final"]))

    c5, c6 = st.columns(2)
    c5.metric("Saldo inicial", _money(cf["saldo_inicial"]))
    c6.metric(
        "Variación",
        _money(cf["saldo_final"] - cf["saldo_inicial"]),
    )

    # Gráfica diaria
    if not cf["tabla_diaria"].empty:
        st.markdown("### 📈 Movimiento diario de caja")
        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=cf["tabla_diaria"]["fecha_dia"],
            y=cf["tabla_diaria"]["entradas"],
            name="Entradas", marker_color="#10b981",
        ))
        fig.add_trace(go.Bar(
            x=cf["tabla_diaria"]["fecha_dia"],
            y=-cf["tabla_diaria"]["salidas"],
            name="Salidas", marker_color="#ef4444",
        ))
        # Línea de saldo acumulado
        cf_daily = cf["tabla_diaria"].copy()
        cf_daily["saldo_acumulado"] = (
            cf["saldo_inicial"] + cf_daily["neto"].cumsum()
        )
        fig.add_trace(go.Scatter(
            x=cf_daily["fecha_dia"], y=cf_daily["saldo_acumulado"],
            name="Saldo acumulado", yaxis="y2",
            line=dict(color="#3b82f6", width=2),
            mode="lines+markers",
        ))
        fig.update_layout(
            barmode="relative", height=380, margin=dict(l=0, r=0, t=20, b=0),
            yaxis=dict(title="Movimiento", tickformat=",.0f"),
            yaxis2=dict(title="Saldo", overlaying="y", side="right",
                        tickformat=",.0f"),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        )
        st.plotly_chart(fig, use_container_width=True)

    # Top contrapartes (de quién recibo / a quién pago)
    if not cf["tabla_por_contraparte"].empty:
        with st.expander("📋 Top contrapartes en flujo de caja", expanded=False):
            st.dataframe(
                cf["tabla_por_contraparte"],
                column_config={
                    "partner_name_str": st.column_config.TextColumn(
                        "Contraparte", width="large"
                    ),
                    "entradas": st.column_config.NumberColumn(
                        "Entradas", format="$%,.0f"
                    ),
                    "salidas": st.column_config.NumberColumn(
                        "Salidas", format="$%,.0f"
                    ),
                    "neto": st.column_config.NumberColumn(
                        "Neto", format="$%,.0f"
                    ),
                },
                use_container_width=True, hide_index=True, height=400,
            )


# ---------------------------------------------------------------------------
# TAB 5: Análisis de Gastos
# ---------------------------------------------------------------------------
with tab_gastos:
    st.markdown("## 💸 Análisis de Gastos")
    st.caption(
        f"Período: {fecha_desde} → {fecha_hasta} · "
        f"Comparativo: {fecha_desde_prev} → {fecha_hasta_prev}"
    )

    # ── 1. Identificar cuentas de gasto del chart (grupo 5) para cargar
    #       movimientos individuales SOLO de esas cuentas (rápido).
    chart_e = enrich_chart_with_puc(chart) if not chart.empty else chart
    if not chart_e.empty and "puc_group" in chart_e.columns:
        expense_accounts = chart_e[chart_e["puc_group"].astype(str) == "5"]
    else:
        expense_accounts = pd.DataFrame()

    expense_account_ids: tuple[int, ...] = tuple(
        int(i) for i in expense_accounts["id"].dropna().unique()
    ) if not expense_accounts.empty and "id" in expense_accounts.columns else ()

    if not expense_account_ids:
        st.info(
            "No se encontraron cuentas clasificadas como gasto (grupo 5) "
            "en el plan de cuentas."
        )
    else:
        with st.spinner("Cargando movimientos de gastos..."):
            moves_gastos = load_expense_movements(
                date_from=fecha_desde.isoformat(),
                date_to=fecha_hasta.isoformat(),
                expense_account_ids=expense_account_ids,
                company_ids=filters["company_ids"],
            )
            moves_gastos_prev = load_expense_movements(
                date_from=fecha_desde_prev.isoformat(),
                date_to=fecha_hasta_prev.isoformat(),
                expense_account_ids=expense_account_ids,
                company_ids=filters["company_ids"],
            )

        exp = compute_expenses_breakdown(
            moves_gastos, chart, fecha_desde, fecha_hasta,
        )
        exp_prev = compute_expenses_breakdown(
            moves_gastos_prev, chart, fecha_desde_prev, fecha_hasta_prev,
        )
        comp = compute_expenses_comparative(exp, exp_prev, threshold_pct=20.0)

        if exp["total_gastos"] == 0:
            st.info("No hay gastos en el período seleccionado.")
        else:
            # ── KPIs cabecera ──────────────────────────────────────────
            ps = exp["por_subgrupo"]
            top_sg = ps.iloc[0] if not ps.empty else None

            k1, k2, k3, k4 = st.columns(4)
            with k1:
                st.metric("💸 Total gastos", _money(exp["total_gastos"]))
            with k2:
                delta_str = (
                    f"{comp['delta_total']:+,.0f} "
                    f"({comp['pct_var_total']:+.1f}%)"
                )
                st.metric(
                    "vs período anterior",
                    _money(exp_prev["total_gastos"]),
                    delta=delta_str,
                    delta_color=(
                        "inverse" if comp["delta_total"] > 0 else "normal"
                    ),
                )
            with k3:
                if top_sg is not None:
                    st.metric(
                        "🥇 Subgrupo mayor",
                        str(top_sg["subgrupo"]),
                        delta=f"{top_sg['pct']:.0f}% del total",
                        delta_color="off",
                    )
            with k4:
                n_alertas = len(comp["alertas"])
                st.metric(
                    "🔔 Alertas",
                    f"{n_alertas}",
                    delta=(
                        "ver panel abajo" if n_alertas else "sin variaciones"
                    ),
                    delta_color="off",
                )

            st.markdown("---")

            # ── Sub-pestañas para organizar el detalle ─────────────────
            tg_resumen, tg_drill, tg_evol, tg_comp, tg_tercero, tg_alert = (
                st.tabs([
                    "📊 Resumen",
                    "🔍 Drill-down",
                    "📈 Evolución mensual",
                    "🔄 Comparativo",
                    "🤝 Por tercero",
                    "🔔 Alertas",
                ])
            )

            # ─── Sub-tab: Resumen ──────────────────────────────────────
            with tg_resumen:
                col_a, col_b = st.columns([1, 1])
                with col_a:
                    st.markdown("### 🗂️ Composición por subgrupo")
                    if not ps.empty:
                        fig = px.pie(
                            ps, values="monto", names="subgrupo", title=None,
                            color_discrete_sequence=px.colors.sequential.Reds_r,
                        )
                        fig.update_layout(
                            height=400, margin=dict(l=0, r=0, t=10, b=0),
                        )
                        st.plotly_chart(fig, use_container_width=True)
                        st.dataframe(
                            ps,
                            column_config={
                                "subgrupo": "Subgrupo",
                                "monto": st.column_config.NumberColumn(
                                    "Monto", format="$%,.0f"
                                ),
                                "pct": st.column_config.NumberColumn(
                                    "%", format="%.1f%%"
                                ),
                            },
                            use_container_width=True, hide_index=True,
                        )

                with col_b:
                    st.markdown("### 🏆 Top 15 cuentas")
                    top_cuentas = exp["por_cuenta"].head(15)
                    if not top_cuentas.empty:
                        fig2 = px.bar(
                            top_cuentas.sort_values("monto"),
                            x="monto", y="account_name", orientation="h",
                            color="subgrupo", text="monto",
                            color_discrete_map={
                                "Gastos administrativos": "#ef4444",
                                "Gastos de ventas": "#f97316",
                                "Gastos no operacionales": "#a855f7",
                            },
                        )
                        fig2.update_traces(
                            texttemplate="%{text:,.0f}",
                            textposition="outside",
                        )
                        fig2.update_layout(
                            height=440, margin=dict(l=0, r=0, t=10, b=0),
                            yaxis=dict(title=""),
                            xaxis=dict(tickformat=",.0f"),
                            legend=dict(
                                orientation="h", yanchor="bottom",
                                y=-0.2, xanchor="left", x=0,
                            ),
                        )
                        st.plotly_chart(fig2, use_container_width=True)

                # Tabla detallada
                st.markdown("### 📋 Detalle por cuenta")
                pc_show = exp["por_cuenta"].copy()
                if not pc_show.empty:
                    # Búsqueda por código/nombre
                    q = st.text_input(
                        "Buscar cuenta",
                        placeholder="Código o nombre…",
                        key="exp_buscar_cuenta",
                    )
                    if q:
                        q_lower = q.lower()
                        mask = (
                            pc_show["account_code"].astype(str)
                            .str.lower().str.contains(q_lower, na=False)
                            | pc_show["account_name"].astype(str)
                            .str.lower().str.contains(q_lower, na=False)
                        )
                        pc_show = pc_show[mask]
                    st.dataframe(
                        pc_show,
                        column_config={
                            "account_code": "Código",
                            "account_name": st.column_config.TextColumn(
                                "Cuenta", width="large"
                            ),
                            "subgrupo": "Subgrupo",
                            "monto": st.column_config.NumberColumn(
                                "Monto", format="$%,.0f"
                            ),
                            "pct": st.column_config.NumberColumn(
                                "% del total", format="%.1f%%"
                            ),
                        },
                        use_container_width=True, hide_index=True,
                        height=400,
                    )

            # ─── Sub-tab: Drill-down ───────────────────────────────────
            with tg_drill:
                st.markdown("### 🔍 Subgrupo → Cuenta → Movimiento")
                st.caption(
                    "Selecciona un subgrupo para ver sus cuentas, y una cuenta "
                    "para ver los movimientos individuales."
                )
                col_sel1, col_sel2 = st.columns(2)
                with col_sel1:
                    subgrupos_disp = (
                        exp["por_subgrupo"]["subgrupo"].tolist()
                        if not exp["por_subgrupo"].empty else []
                    )
                    sel_sg = st.selectbox(
                        "Subgrupo", options=subgrupos_disp,
                        key="exp_sel_subgrupo",
                    )
                with col_sel2:
                    cuentas_del_sg = (
                        exp["por_cuenta"][exp["por_cuenta"]["subgrupo"] == sel_sg]
                        if sel_sg else pd.DataFrame()
                    )
                    opciones_cuenta = ["(Todas las cuentas)"] + [
                        f"{r['account_code']} — {r['account_name']}"
                        for _, r in cuentas_del_sg.iterrows()
                    ]
                    sel_cta = st.selectbox(
                        "Cuenta", options=opciones_cuenta,
                        key="exp_sel_cuenta",
                    )

                # Mostrar cuentas del subgrupo
                if not cuentas_del_sg.empty:
                    st.markdown(f"**Cuentas dentro de {sel_sg}:**")
                    st.dataframe(
                        cuentas_del_sg,
                        column_config={
                            "account_code": "Código",
                            "account_name": st.column_config.TextColumn(
                                "Cuenta", width="large"
                            ),
                            "subgrupo": None,
                            "monto": st.column_config.NumberColumn(
                                "Monto", format="$%,.0f"
                            ),
                            "pct": st.column_config.NumberColumn(
                                "% del total", format="%.1f%%"
                            ),
                        },
                        use_container_width=True, hide_index=True,
                        height=260,
                    )

                # Mostrar movimientos
                movs = exp.get("movimientos", pd.DataFrame())
                if not movs.empty:
                    if sel_sg:
                        movs = movs[movs["subgrupo"] == sel_sg]
                    if sel_cta and sel_cta != "(Todas las cuentas)":
                        cod = sel_cta.split(" — ")[0]
                        movs = movs[movs["account_code"] == cod]

                    st.markdown(
                        f"**Movimientos detallados ({len(movs):,} líneas):**"
                    )
                    if not movs.empty:
                        # Mostrar columnas amigables
                        show_cols = [
                            "date", "account_code", "account_name",
                            "partner_name", "move_id_name", "name",
                            "monto",
                        ]
                        show_cols = [c for c in show_cols if c in movs.columns]
                        st.dataframe(
                            movs[show_cols].head(500),
                            column_config={
                                "date": st.column_config.DateColumn(
                                    "Fecha", format="YYYY-MM-DD"
                                ),
                                "account_code": "Código",
                                "account_name": st.column_config.TextColumn(
                                    "Cuenta", width="medium"
                                ),
                                "partner_name": "Tercero",
                                "move_id_name": "Asiento",
                                "name": st.column_config.TextColumn(
                                    "Descripción", width="large"
                                ),
                                "monto": st.column_config.NumberColumn(
                                    "Monto", format="$%,.0f"
                                ),
                            },
                            use_container_width=True, hide_index=True,
                            height=420,
                        )
                        if len(movs) > 500:
                            st.caption(
                                f"Mostrando primeras 500 de {len(movs):,} "
                                "líneas."
                            )

            # ─── Sub-tab: Evolución mensual ────────────────────────────
            with tg_evol:
                st.markdown("### 📈 Evolución mensual por subgrupo")
                por_mes = exp.get("por_mes", pd.DataFrame()).copy()
                if not por_mes.empty:
                    por_mes["mes_label"] = (
                        pd.to_datetime(por_mes["mes"]).dt.strftime("%Y-%m")
                    )
                    # Barras apiladas
                    fig3 = px.bar(
                        por_mes, x="mes_label", y="monto", color="subgrupo",
                        title=None, text="monto",
                        color_discrete_map={
                            "Gastos administrativos": "#ef4444",
                            "Gastos de ventas": "#f97316",
                            "Gastos no operacionales": "#a855f7",
                        },
                    )
                    fig3.update_traces(
                        texttemplate="%{text:,.0f}", textposition="inside",
                    )
                    fig3.update_layout(
                        height=420, margin=dict(l=0, r=0, t=10, b=0),
                        yaxis=dict(tickformat=",.0f"),
                    )
                    st.plotly_chart(fig3, use_container_width=True)

                    # Línea de total mensual
                    total_mes = por_mes.groupby(
                        "mes_label", as_index=False
                    )["monto"].sum()
                    fig4 = px.line(
                        total_mes, x="mes_label", y="monto", markers=True,
                        text="monto", title=None,
                    )
                    fig4.update_traces(
                        texttemplate="%{text:,.0f}", textposition="top center",
                        line=dict(color="#dc2626", width=3),
                    )
                    fig4.update_layout(
                        height=300, margin=dict(l=0, r=0, t=10, b=0),
                        yaxis=dict(tickformat=",.0f"),
                        title="Gasto total por mes",
                    )
                    st.plotly_chart(fig4, use_container_width=True)

                    # Tabla pivote
                    with st.expander(
                        "📋 Tabla pivote (mes × subgrupo)", expanded=False
                    ):
                        pivot = por_mes.pivot_table(
                            index="mes_label", columns="subgrupo",
                            values="monto", aggfunc="sum", fill_value=0,
                        ).reset_index()
                        st.dataframe(
                            pivot, use_container_width=True, hide_index=True,
                        )
                else:
                    st.info("No hay datos mensuales para mostrar.")

            # ─── Sub-tab: Comparativo ──────────────────────────────────
            with tg_comp:
                st.markdown("### 🔄 Variación vs período anterior")
                st.caption(
                    f"Actual: {fecha_desde} → {fecha_hasta} · "
                    f"Anterior: {fecha_desde_prev} → {fecha_hasta_prev}"
                )

                vs = comp.get("variacion_subgrupo", pd.DataFrame())
                if not vs.empty:
                    st.markdown("**Variación por subgrupo**")
                    st.dataframe(
                        vs,
                        column_config={
                            "subgrupo": "Subgrupo",
                            "monto_act": st.column_config.NumberColumn(
                                "Actual", format="$%,.0f"
                            ),
                            "monto_prev": st.column_config.NumberColumn(
                                "Anterior", format="$%,.0f"
                            ),
                            "delta": st.column_config.NumberColumn(
                                "Δ", format="$%,.0f"
                            ),
                            "pct_var": st.column_config.NumberColumn(
                                "% Var", format="%.1f%%"
                            ),
                        },
                        use_container_width=True, hide_index=True,
                    )

                vc = comp.get("variacion_cuenta", pd.DataFrame())
                if not vc.empty:
                    st.markdown("**Variación por cuenta**")
                    # Filtro por subgrupo
                    sub_filter = st.multiselect(
                        "Filtrar por subgrupo",
                        options=sorted(vc["subgrupo"].dropna().unique()),
                        default=[],
                        key="exp_comp_sub_filter",
                    )
                    vc_show = vc.copy()
                    if sub_filter:
                        vc_show = vc_show[vc_show["subgrupo"].isin(sub_filter)]
                    only_alertas = st.checkbox(
                        "Solo cuentas con alerta",
                        value=False, key="exp_comp_only_alert",
                    )
                    if only_alertas:
                        vc_show = vc_show[vc_show["alerta"] != ""]

                    st.dataframe(
                        vc_show,
                        column_config={
                            "account_code": "Código",
                            "account_name": st.column_config.TextColumn(
                                "Cuenta", width="large"
                            ),
                            "subgrupo": "Subgrupo",
                            "monto_act": st.column_config.NumberColumn(
                                "Actual", format="$%,.0f"
                            ),
                            "monto_prev": st.column_config.NumberColumn(
                                "Anterior", format="$%,.0f"
                            ),
                            "delta": st.column_config.NumberColumn(
                                "Δ", format="$%,.0f"
                            ),
                            "pct_var": st.column_config.NumberColumn(
                                "% Var", format="%.1f%%"
                            ),
                            "alerta": "Alerta",
                        },
                        use_container_width=True, hide_index=True,
                        height=500,
                    )

                    # Top subidas y bajadas
                    cs1, cs2 = st.columns(2)
                    with cs1:
                        st.markdown("**🔺 Top 10 subidas (Δ$)**")
                        top_sub = vc[vc["delta"] > 0].nlargest(10, "delta")
                        if not top_sub.empty:
                            fig_s = px.bar(
                                top_sub.sort_values("delta"),
                                x="delta", y="account_name",
                                orientation="h",
                                color_discrete_sequence=["#dc2626"],
                                text="delta",
                            )
                            fig_s.update_traces(
                                texttemplate="+%{text:,.0f}",
                                textposition="outside",
                            )
                            fig_s.update_layout(
                                height=360, margin=dict(l=0, r=0, t=10, b=0),
                                yaxis=dict(title=""),
                                xaxis=dict(tickformat=",.0f"),
                            )
                            st.plotly_chart(fig_s, use_container_width=True)
                    with cs2:
                        st.markdown("**🔻 Top 10 bajadas (Δ$)**")
                        top_baj = vc[vc["delta"] < 0].nsmallest(10, "delta")
                        if not top_baj.empty:
                            fig_b = px.bar(
                                top_baj.sort_values("delta", ascending=False),
                                x="delta", y="account_name",
                                orientation="h",
                                color_discrete_sequence=["#10b981"],
                                text="delta",
                            )
                            fig_b.update_traces(
                                texttemplate="%{text:,.0f}",
                                textposition="outside",
                            )
                            fig_b.update_layout(
                                height=360, margin=dict(l=0, r=0, t=10, b=0),
                                yaxis=dict(title=""),
                                xaxis=dict(tickformat=",.0f"),
                            )
                            st.plotly_chart(fig_b, use_container_width=True)
                else:
                    st.info("No hay datos del período anterior para comparar.")

            # ─── Sub-tab: Por tercero ──────────────────────────────────
            with tg_tercero:
                st.markdown("### 🤝 Análisis por tercero/proveedor")
                st.caption(
                    "Qué proveedores concentran cada cuenta de gasto. "
                    "Útil para negociar, detectar concentración y auditar."
                )
                pt = exp.get("por_tercero", pd.DataFrame())
                if pt.empty:
                    st.info(
                        "No hay información de terceros en los movimientos "
                        "de este período."
                    )
                else:
                    # KPIs de concentración
                    pt_partner = pt.groupby(
                        ["partner_id", "partner_name"], as_index=False,
                        dropna=False,
                    )["monto"].sum().sort_values("monto", ascending=False)
                    total_t = float(pt_partner["monto"].sum())
                    n_terceros = pt_partner["partner_id"].nunique()
                    top_3 = pt_partner.head(3)["monto"].sum()
                    pct_top3 = (top_3 / total_t * 100) if total_t else 0

                    kt1, kt2, kt3 = st.columns(3)
                    with kt1:
                        st.metric("Terceros distintos", f"{n_terceros:,}")
                    with kt2:
                        st.metric("Top 3 concentración", f"{pct_top3:.1f}%")
                    with kt3:
                        if not pt_partner.empty:
                            top_p = pt_partner.iloc[0]
                            st.metric(
                                "🥇 Top 1",
                                str(top_p["partner_name"])[:30],
                                delta=_money(top_p["monto"]),
                                delta_color="off",
                            )

                    # Filtro por subgrupo
                    sg_t = st.selectbox(
                        "Ver por subgrupo",
                        options=["(Todos)"] + sorted(
                            pt["subgrupo"].dropna().unique().tolist()
                        ),
                        key="exp_tercero_sub",
                    )
                    pt_show = pt.copy()
                    if sg_t and sg_t != "(Todos)":
                        pt_show = pt_show[pt_show["subgrupo"] == sg_t]

                    # Top 20 terceros
                    st.markdown("**Top 20 terceros por gasto**")
                    top_t = pt_show.groupby(
                        ["partner_id", "partner_name"], as_index=False,
                        dropna=False,
                    )["monto"].sum().sort_values(
                        "monto", ascending=False
                    ).head(20)
                    if not top_t.empty:
                        fig_t = px.bar(
                            top_t.sort_values("monto"),
                            x="monto", y="partner_name", orientation="h",
                            color_discrete_sequence=["#7c3aed"],
                            text="monto",
                        )
                        fig_t.update_traces(
                            texttemplate="%{text:,.0f}",
                            textposition="outside",
                        )
                        fig_t.update_layout(
                            height=520, margin=dict(l=0, r=0, t=10, b=0),
                            yaxis=dict(title=""),
                            xaxis=dict(tickformat=",.0f"),
                        )
                        st.plotly_chart(fig_t, use_container_width=True)

                    # Tabla cruzada tercero × cuenta
                    st.markdown("**Detalle tercero × cuenta**")
                    st.dataframe(
                        pt_show[[
                            "partner_name", "account_code", "account_name",
                            "subgrupo", "monto", "pct",
                        ]],
                        column_config={
                            "partner_name": st.column_config.TextColumn(
                                "Tercero", width="medium"
                            ),
                            "account_code": "Código",
                            "account_name": st.column_config.TextColumn(
                                "Cuenta", width="large"
                            ),
                            "subgrupo": "Subgrupo",
                            "monto": st.column_config.NumberColumn(
                                "Monto", format="$%,.0f"
                            ),
                            "pct": st.column_config.NumberColumn(
                                "%", format="%.1f%%"
                            ),
                        },
                        use_container_width=True, hide_index=True,
                        height=420,
                    )

            # ─── Sub-tab: Alertas ──────────────────────────────────────
            with tg_alert:
                st.markdown("### 🔔 Alertas automáticas")
                st.caption(
                    "Variaciones significativas (>20%) vs período anterior, "
                    "gastos nuevos que no existían antes, y reducciones "
                    "importantes."
                )
                alertas = comp.get("alertas", [])
                if not alertas:
                    st.success(
                        "✅ Sin variaciones significativas vs el período "
                        "anterior."
                    )
                else:
                    # Agrupar por tipo
                    df_a = pd.DataFrame(alertas)
                    for tipo, grupo in df_a.groupby("tipo"):
                        st.markdown(f"#### {tipo}")
                        for _, r in grupo.iterrows():
                            with st.container(border=True):
                                cA, cB = st.columns([3, 1])
                                with cA:
                                    st.markdown(
                                        f"**{r['cuenta']}**  "
                                        f"\n_{r['subgrupo']}_  "
                                        f"\n{r['detalle']}"
                                    )
                                with cB:
                                    st.metric(
                                        "Monto", _money(r["monto"]),
                                        label_visibility="collapsed",
                                    )


# ---------------------------------------------------------------------------
# TAB 6: Comparativos
# ---------------------------------------------------------------------------
with tab_comp:
    st.markdown("## 🔄 Evolución mensual de utilidades y márgenes")
    st.caption(f"Período: {fecha_desde} → {fecha_hasta}")

    monthly = compute_pnl_monthly_evolution(moves, chart, fecha_desde, fecha_hasta)

    if monthly.empty:
        st.info("No hay datos suficientes para evolución mensual.")
    else:
        monthly_show = monthly.copy()
        monthly_show["mes_label"] = monthly_show["mes"].dt.strftime("%Y-%m")

        # Gráfica 1: Ingresos, costos, utilidad neta
        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=monthly_show["mes_label"], y=monthly_show["ingreso_op"],
            name="Ingresos op.", marker_color="#10b981",
        ))
        fig.add_trace(go.Bar(
            x=monthly_show["mes_label"], y=-monthly_show["costo"],
            name="Costo", marker_color="#94a3b8",
        ))
        fig.add_trace(go.Bar(
            x=monthly_show["mes_label"],
            y=-(monthly_show["gasto_admin"] + monthly_show["gasto_ventas"]),
            name="Gastos op.", marker_color="#f59e0b",
        ))
        fig.add_trace(go.Scatter(
            x=monthly_show["mes_label"], y=monthly_show["utilidad_neta"],
            name="Utilidad Neta", line=dict(color="#3b82f6", width=3),
            mode="lines+markers",
        ))
        fig.update_layout(
            barmode="relative", height=420, margin=dict(l=0, r=0, t=20, b=0),
            yaxis=dict(tickformat=",.0f"),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        )
        st.plotly_chart(fig, use_container_width=True)

        # Gráfica 2: Márgenes
        st.markdown("### 📊 Evolución de márgenes (%)")
        fig2 = go.Figure()
        for col, name, color in [
            ("margen_bruto_pct", "Margen Bruto %", "#10b981"),
            ("margen_op_pct", "Margen Operacional %", "#3b82f6"),
            ("margen_neto_pct", "Margen Neto %", "#f59e0b"),
        ]:
            fig2.add_trace(go.Scatter(
                x=monthly_show["mes_label"], y=monthly_show[col],
                name=name, line=dict(color=color, width=2),
                mode="lines+markers",
            ))
        fig2.update_layout(height=350, margin=dict(l=0, r=0, t=20, b=0),
                           yaxis=dict(ticksuffix="%"))
        st.plotly_chart(fig2, use_container_width=True)

        # Tabla
        with st.expander("📋 Tabla mensual completa", expanded=False):
            show_t = monthly.copy()
            show_t["mes"] = show_t["mes"].dt.strftime("%Y-%m")
            st.dataframe(
                show_t,
                column_config={
                    "mes": "Mes",
                    "ingreso_op": st.column_config.NumberColumn("Ingresos op.", format="$%,.0f"),
                    "ingreso_no_op": st.column_config.NumberColumn("Ingr. no op.", format="$%,.0f"),
                    "costo": st.column_config.NumberColumn("Costo", format="$%,.0f"),
                    "gasto_admin": st.column_config.NumberColumn("Gastos admin.", format="$%,.0f"),
                    "gasto_ventas": st.column_config.NumberColumn("Gastos ventas", format="$%,.0f"),
                    "gasto_no_op": st.column_config.NumberColumn("Gastos no op.", format="$%,.0f"),
                    "impto": st.column_config.NumberColumn("Impuesto", format="$%,.0f"),
                    "utilidad_bruta": st.column_config.NumberColumn("Util. Bruta", format="$%,.0f"),
                    "utilidad_op": st.column_config.NumberColumn("Util. Op.", format="$%,.0f"),
                    "utilidad_neta": st.column_config.NumberColumn("Util. Neta", format="$%,.0f"),
                    "margen_bruto_pct": st.column_config.NumberColumn("M. Bruto %", format="%.1f%%"),
                    "margen_op_pct": st.column_config.NumberColumn("M. Op. %", format="%.1f%%"),
                    "margen_neto_pct": st.column_config.NumberColumn("M. Neto %", format="%.1f%%"),
                },
                use_container_width=True, hide_index=True,
            )
