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
    compute_full_analysis,
    load_account_movements,
    load_chart_of_accounts,
)
from src.ui_components import render_company_context, render_sidebar_filters
from src.financial_statements import (
    compute_balance_sheet,
    compute_cash_flow,
    compute_expenses_breakdown,
    compute_income_statement,
    compute_pnl_monthly_evolution,
    compute_working_capital,
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
        options=["Personalizado", "Año en curso", "Año anterior",
                 "Últimos 12 meses", "Mes actual", "Mes anterior", "Trimestre actual"],
        index=1, horizontal=False, key="ef_atajo",
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
# Cargar datos
# ---------------------------------------------------------------------------
# Determinar meses a cargar según el rango seleccionado (no cargar más de
# lo necesario para reducir tiempo de descarga)
dias_periodo = (fecha_hasta - fecha_desde).days
# Margen extra para tener saldo inicial del balance + período anterior comparativo
if dias_periodo <= 90:
    HIST_MONTHS = 12   # rango corto → 12 meses (suficiente para comparativo YoY)
elif dias_periodo <= 365:
    HIST_MONTHS = 18   # rango anual → 18 meses
else:
    HIST_MONTHS = 36   # rango muy largo → 3 años

with st.spinner(
    f"Cargando movimientos contables ({HIST_MONTHS} meses)..."
):
    chart = load_chart_of_accounts(
        company_ids=filters["company_ids"],
    )
    moves = load_account_movements(
        months_back=HIST_MONTHS,
        company_ids=filters["company_ids"],
    )

if moves is None or moves.empty:
    st.error("No se pudieron cargar movimientos contables.")
    st.stop()

# Banner de empresa
data_meta = compute_full_analysis(
    months_back=12,
    rotation_period_days=filters["period_days"],
    company_ids=filters["company_ids"],
    exclude_cash_sales=filters["exclude_cash_sales"],
    analysis_window_days=filters.get("analysis_window_days"),
)
render_company_context(data_meta.get("companies"), filters["company_ids"])

st.success(
    f"✅ {len(moves):,} movimientos · {len(chart):,} cuentas en el plan."
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
        st.markdown("**Movimientos contables (moves)**")
        st.write(f"Columnas: `{list(moves.columns)}`")
        st.write(f"Total movimientos: {len(moves):,}")
        if not moves.empty:
            st.write("Primeros 3 movimientos:")
            st.dataframe(moves.head(3), hide_index=True)
            if "date" in moves.columns:
                st.write(
                    f"Rango fechas: {moves['date'].min()} → {moves['date'].max()}"
                )
            if "account_id" in moves.columns:
                st.write(
                    f"Cuentas únicas usadas en moves: "
                    f"{moves['account_id'].nunique():,}"
                )
                # Verificar match con chart
                if "id" in chart.columns:
                    chart_ids = set(chart["id"].dropna().astype(int))
                    move_account_ids = set(
                        moves["account_id"].dropna().astype(int)
                    )
                    matched = chart_ids & move_account_ids
                    unmatched = move_account_ids - chart_ids
                    st.write(f"IDs que matchean entre chart y moves: {len(matched):,}")
                    st.write(f"IDs en moves SIN match en chart: {len(unmatched):,}")


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

    pnl = compute_income_statement(moves, chart, fecha_desde, fecha_hasta)

    # Período anterior (mismo número de días) para comparativa
    periodo_dias = (fecha_hasta - fecha_desde).days + 1
    fecha_desde_prev = fecha_desde - timedelta(days=periodo_dias)
    fecha_hasta_prev = fecha_desde - timedelta(days=1)
    pnl_prev = compute_income_statement(moves, chart, fecha_desde_prev, fecha_hasta_prev)

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

    bs = compute_balance_sheet(moves, chart, fecha_hasta)

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

    wc = compute_working_capital(moves, chart, fecha_hasta)

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
        f"Movimientos de cuentas de disponible (PUC 11xx) entre "
        f"{fecha_desde} y {fecha_hasta}."
    )

    cf = compute_cash_flow(moves, chart, fecha_desde, fecha_hasta)

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
    st.caption(f"Período: {fecha_desde} → {fecha_hasta}")

    exp = compute_expenses_breakdown(moves, chart, fecha_desde, fecha_hasta)

    if exp["total_gastos"] == 0:
        st.info("No hay gastos en el período seleccionado.")
    else:
        st.metric("💸 Total gastos + costos", _money(exp["total_gastos"]))

        # Treemap por subgrupo
        col_a, col_b = st.columns([1, 1])
        with col_a:
            st.markdown("### 🗂️ Por subgrupo")
            if not exp["por_subgrupo"].empty:
                fig = px.pie(
                    exp["por_subgrupo"], values="monto", names="subgrupo",
                    title=None,
                    color_discrete_sequence=px.colors.sequential.Reds_r,
                )
                fig.update_layout(height=400, margin=dict(l=0, r=0, t=10, b=0))
                st.plotly_chart(fig, use_container_width=True)

        with col_b:
            st.markdown("### 🏆 Top cuentas de gasto")
            top_cuentas = exp["por_cuenta"].head(15)
            fig2 = px.bar(
                top_cuentas.sort_values("monto"),
                x="monto", y="account_name", orientation="h",
                color_discrete_sequence=["#ef4444"],
                text="monto",
            )
            fig2.update_traces(texttemplate="%{text:,.0f}", textposition="outside")
            fig2.update_layout(
                height=400, margin=dict(l=0, r=0, t=10, b=0),
                yaxis=dict(title=""), xaxis=dict(tickformat=",.0f"),
            )
            st.plotly_chart(fig2, use_container_width=True)

        # Tabla detallada
        with st.expander("📋 Detalle completo por cuenta", expanded=False):
            st.dataframe(
                exp["por_cuenta"],
                column_config={
                    "account_code": "Código",
                    "account_name": st.column_config.TextColumn("Cuenta", width="large"),
                    "subgrupo": "Subgrupo",
                    "monto": st.column_config.NumberColumn("Monto", format="$%,.0f"),
                    "pct": st.column_config.NumberColumn("% del total", format="%.1f%%"),
                },
                use_container_width=True, hide_index=True, height=500,
            )

        # Evolución mensual de gastos por subgrupo
        if not exp["por_mes"].empty:
            st.markdown("### 📈 Evolución mensual de gastos por categoría")
            por_mes = exp["por_mes"].copy()
            por_mes["mes_label"] = pd.to_datetime(por_mes["mes"]).dt.strftime("%Y-%m")
            fig3 = px.bar(
                por_mes, x="mes_label", y="monto", color="subgrupo",
                title=None,
            )
            fig3.update_layout(height=400, margin=dict(l=0, r=0, t=10, b=0),
                               yaxis=dict(tickformat=",.0f"))
            st.plotly_chart(fig3, use_container_width=True)


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
