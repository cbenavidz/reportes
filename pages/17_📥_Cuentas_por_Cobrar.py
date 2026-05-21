# -*- coding: utf-8 -*-
"""
Página: Cuentas por Cobrar — vista operativa.

Informe operativo de cuentas por cobrar a clientes:
  - KPIs de cartera (total por cobrar, vencido, próximo a vencer).
  - 🔴 Panel de CLIENTES EN MORA: priorización de gestión de cobro.
  - 📅 Calendario de cobros (próximos vencimientos).
  - 💧 Proyección de ingresos esperados (semanal).
  - 📊 Aging de cartera (antigüedad de los saldos).
  - 🔁 Rotación de cartera y DSO (días promedio de cobro).
  - 🏆 Top clientes por saldo.
  - 📋 Tabla detallada + exportación a Excel.
"""
from __future__ import annotations

import io
from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from src.auth import logout_button, require_auth
from src.data_loader import (
    load_companies,
    load_invoice_lines,
    load_receivables,
)
from src.receivables_analyzer import (
    build_tabla_detalle,
    compute_aging,
    compute_calendar,
    compute_dso,
    compute_morosos,
    compute_proyeccion_cobros_semanal,
    compute_receivables_kpis,
    compute_top_clientes,
    enrich_receivables,
)
from src.ui_components import render_company_context, render_sidebar_filters


st.set_page_config(
    page_title="Cuentas por Cobrar | Cartera",
    page_icon="📥",
    layout="wide",
)

require_auth()
logout_button()

st.title("📥 Cuentas por Cobrar")
st.caption(
    "Vista operativa de la cartera: qué cobrar y cuándo, clientes en mora, "
    "calendario de cobros, proyección de ingresos y rotación de cartera."
)

# Sidebar
filters = render_sidebar_filters()
if filters["company_ids"] is not None and len(filters["company_ids"]) == 0:
    st.warning("Selecciona al menos una empresa.")
    st.stop()


# ── Helpers de formato ──
def fmt_cop(x) -> str:
    """Formatea un número como pesos colombianos."""
    try:
        return f"${x:,.0f}".replace(",", ".")
    except (TypeError, ValueError):
        return "$0"


# ── Configuración ──
cfg1, cfg2 = st.columns([1, 1])
with cfg1:
    horizonte = st.selectbox(
        "Horizonte del calendario",
        options=[30, 60, 90],
        index=0,
        format_func=lambda d: f"Próximos {d} días",
    )
with cfg2:
    if st.button("🔄 Recargar datos", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

today = date.today()

# Banner empresa
companies_df = load_companies()
render_company_context(companies_df, filters["company_ids"])


# ── Carga de datos ──
with st.spinner("Cargando facturas de cliente..."):
    company_ids = (
        tuple(filters["company_ids"]) if filters["company_ids"] else None
    )
    receivables = load_receivables(company_ids=company_ids)

if receivables is None or receivables.empty:
    st.success(
        "🎉 No hay facturas de cliente pendientes de cobro. "
        "Toda la cartera está al día."
    )
    st.stop()

# Enriquecer
enriched = enrich_receivables(receivables, today=today)
kpis = compute_receivables_kpis(enriched, today=today, horizonte_dias=horizonte)


# ===========================================================================
# KPIs CABECERA
# ===========================================================================
st.markdown("### 📊 Resumen de Cartera")

k1, k2, k3, k4 = st.columns(4)
with k1:
    st.metric("Total por cobrar", fmt_cop(kpis["total_por_cobrar"]))
    st.caption(f"{kpis['n_facturas']} facturas · {kpis['n_clientes']} clientes")
with k2:
    st.metric(
        "🔴 Cartera vencida", fmt_cop(kpis["total_vencido"]),
        delta=f"{kpis['pct_vencido']:.0f}% del total",
        delta_color="inverse",
    )
    st.caption(f"{kpis['n_vencidas']} facturas · {kpis['n_clientes_mora']} clientes")
with k3:
    st.metric(
        f"Vence en {horizonte} días",
        fmt_cop(kpis["total_por_cobrar_h"]),
    )
with k4:
    st.metric(
        "Mora promedio",
        f"{kpis['mora_promedio_dias']:.0f} días",
    )
    st.caption("Ponderada por saldo vencido")

# Alerta global de cartera vencida
if kpis["pct_vencido"] >= 40:
    st.error(
        f"🔴 El **{kpis['pct_vencido']:.0f}%** de tu cartera está vencida "
        f"({fmt_cop(kpis['total_vencido'])}). Es un nivel alto — revisa el "
        "panel de clientes en mora y prioriza la gestión de cobro."
    )
elif kpis["pct_vencido"] >= 20:
    st.warning(
        f"🟠 El {kpis['pct_vencido']:.0f}% de tu cartera está vencida "
        f"({fmt_cop(kpis['total_vencido'])}). Conviene gestionar los cobros."
    )

st.markdown("---")


# ===========================================================================
# 🔴 CLIENTES EN MORA
# ===========================================================================
st.markdown("### 🔴 Clientes en Mora")
st.caption(
    "Priorización de gestión de cobro: clientes con facturas vencidas, "
    "ordenados por saldo vencido."
)

morosos = compute_morosos(enriched, top_n=20)

if morosos is None or morosos.empty:
    st.success("✅ No hay clientes en mora. Toda la cartera está vigente.")
else:
    total_mora = morosos["saldo_vencido"].sum()
    st.warning(
        f"⚠️ **{len(morosos)} clientes** tienen cartera vencida por un total "
        f"de **{fmt_cop(total_mora)}**."
    )

    def _gravedad(dias):
        if dias > 90:
            return "🔴 Crítico"
        if dias > 60:
            return "🟠 Alto"
        if dias > 30:
            return "🟡 Medio"
        return "🟢 Reciente"

    morosos_show = morosos.copy()
    morosos_show["Gravedad"] = morosos_show["dias_mora_max"].apply(_gravedad)

    st.dataframe(
        morosos_show,
        column_config={
            "Gravedad": st.column_config.TextColumn("Gravedad", width="small"),
            "partner_name": "Cliente",
            "saldo_vencido": st.column_config.NumberColumn(
                "Saldo vencido", format="$%.0f",
            ),
            "saldo_total": st.column_config.NumberColumn(
                "Saldo total", format="$%.0f",
            ),
            "n_facturas_vencidas": st.column_config.NumberColumn(
                "# Facturas vencidas", format="%d",
            ),
            "dias_mora_max": st.column_config.NumberColumn(
                "Días mora (máx)", format="%d",
            ),
            "dias_mora_prom": st.column_config.NumberColumn(
                "Días mora (prom)", format="%d",
            ),
            "pct_cartera_vencida": st.column_config.NumberColumn(
                "% cartera vencida", format="%.1f%%",
            ),
        },
        use_container_width=True, hide_index=True, height=360,
        column_order=[
            "Gravedad", "partner_name", "saldo_vencido", "saldo_total",
            "n_facturas_vencidas", "dias_mora_max", "dias_mora_prom",
            "pct_cartera_vencida",
        ],
    )
    st.caption(
        "💡 Empieza la gestión por los clientes 🔴 Crítico (más de 90 días) "
        "y mayor saldo vencido — ahí está el mayor riesgo de incobrabilidad."
    )

st.markdown("---")


# ===========================================================================
# 📅 CALENDARIO DE COBROS
# ===========================================================================
st.markdown(f"### 📅 Calendario de Cobros — Próximos {horizonte} días")

cal = compute_calendar(enriched, today=today, horizonte_dias=horizonte)


def render_calendario_html(cal_df: pd.DataFrame, ref_today: date,
                            dias: int) -> str:
    """Construye un calendario mensual en HTML con los montos por día."""
    montos: dict[date, tuple[float, int]] = {}
    if cal_df is not None and not cal_df.empty:
        for _, r in cal_df.iterrows():
            d = pd.Timestamp(r["fecha"]).date()
            montos[d] = (float(r["monto"]), int(r["n_facturas"]))

    fin = ref_today + timedelta(days=dias)
    meses = []
    cur = date(ref_today.year, ref_today.month, 1)
    while cur <= fin:
        meses.append(cur)
        if cur.month == 12:
            cur = date(cur.year + 1, 1, 1)
        else:
            cur = date(cur.year, cur.month + 1, 1)

    nombres_mes = ["", "Enero", "Febrero", "Marzo", "Abril", "Mayo",
                   "Junio", "Julio", "Agosto", "Septiembre", "Octubre",
                   "Noviembre", "Diciembre"]
    dias_sem = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]

    html = ['<div style="display:flex;gap:24px;flex-wrap:wrap;">']
    for primer_dia in meses:
        if primer_dia.month == 12:
            siguiente = date(primer_dia.year + 1, 1, 1)
        else:
            siguiente = date(primer_dia.year, primer_dia.month + 1, 1)
        n_dias = (siguiente - primer_dia).days

        html.append(
            '<div style="flex:1;min-width:320px;">'
            f'<h4 style="margin:4px 0;text-align:center;">'
            f'{nombres_mes[primer_dia.month]} {primer_dia.year}</h4>'
        )
        html.append(
            '<table style="width:100%;border-collapse:collapse;'
            'table-layout:fixed;font-size:11px;">'
        )
        html.append('<tr>')
        for d in dias_sem:
            html.append(
                f'<th style="padding:4px;color:#888;font-weight:600;">{d}</th>'
            )
        html.append('</tr>')

        offset = primer_dia.weekday()
        dia = 1
        html.append('<tr>')
        for _ in range(offset):
            html.append('<td></td>')
        col = offset
        while dia <= n_dias:
            fecha_celda = date(primer_dia.year, primer_dia.month, dia)
            info = montos.get(fecha_celda)
            es_hoy = fecha_celda == ref_today
            es_pasado = fecha_celda < ref_today

            bg = "transparent"
            contenido_monto = ""
            if info and info[0] > 0:
                # Cobros: tonos verdes (entra dinero)
                monto_k = info[0] / 1000
                if monto_k >= 1000:
                    monto_str = f"${monto_k/1000:.1f}M"
                else:
                    monto_str = f"${monto_k:.0f}K"
                bg = "#27AE60"
                txt_color = "#fff"
                contenido_monto = (
                    f'<div style="font-size:10px;font-weight:700;'
                    f'color:{txt_color};">{monto_str}</div>'
                    f'<div style="font-size:8px;color:{txt_color};">'
                    f'{info[1]} fac.</div>'
                )

            borde = "2px solid #2C3E50" if es_hoy else "1px solid #ddd"
            num_color = "#bbb" if es_pasado else "#333"
            html.append(
                f'<td style="border:{borde};background:{bg};'
                f'height:48px;vertical-align:top;padding:2px;">'
                f'<div style="font-size:10px;color:{num_color};'
                f'font-weight:600;">{dia}</div>'
                f'{contenido_monto}</td>'
            )
            col += 1
            dia += 1
            if col == 7 and dia <= n_dias:
                html.append('</tr><tr>')
                col = 0
        while col < 7:
            html.append('<td></td>')
            col += 1
        html.append('</tr></table></div>')
    html.append('</div>')
    html.append(
        '<div style="margin-top:8px;font-size:11px;color:#888;">'
        '<span style="background:#27AE60;color:#fff;padding:2px 6px;'
        'border-radius:3px;">Cobro programado</span> '
        'Cada celda muestra el monto a cobrar y el # de facturas de ese día.'
        '</div>'
    )
    return "".join(html)


if cal is None or cal.empty:
    st.info(f"No hay facturas que venzan en los próximos {horizonte} días.")
else:
    st.markdown(
        render_calendario_html(cal, today, horizonte),
        unsafe_allow_html=True,
    )
    st.markdown("#### Cobros por día")
    fig_cal = px.bar(
        cal, x="fecha", y="monto",
        labels={"fecha": "Fecha", "monto": "Monto a cobrar"},
        hover_data=["n_facturas", "n_clientes"],
    )
    fig_cal.update_traces(marker_color="#27AE60")
    fig_cal.update_layout(
        height=280, margin=dict(l=0, r=0, t=10, b=0),
        yaxis_title="Monto ($)",
    )
    st.plotly_chart(fig_cal, use_container_width=True)

st.markdown("---")


# ===========================================================================
# 💧 PROYECCIÓN DE INGRESOS
# ===========================================================================
st.markdown("### 💧 Proyección de Ingresos — Semanal")
st.caption(
    "Cobros esperados cada semana, según la fecha de vencimiento de las "
    "facturas. La primera barra incluye todo lo ya vencido. "
    "La línea muestra el ingreso acumulado."
)

proy = compute_proyeccion_cobros_semanal(enriched, today=today, semanas=8)
if proy is not None and not proy.empty:
    fig_proy = go.Figure()
    colores = [
        "#E67E22" if i == 0 else "#27AE60"
        for i in range(len(proy))
    ]
    fig_proy.add_trace(go.Bar(
        x=proy["semana_label"],
        y=proy["monto"],
        name="Cobros esperados",
        marker_color=colores,
        text=[fmt_cop(m) for m in proy["monto"]],
        textposition="outside",
        hovertext=[f"{n} facturas" for n in proy["n_facturas"]],
    ))
    fig_proy.add_trace(go.Scatter(
        x=proy["semana_label"],
        y=proy["monto_acumulado"],
        name="Ingreso acumulado",
        mode="lines+markers",
        line=dict(color="#2C3E50", width=3),
        marker=dict(size=8),
    ))
    fig_proy.update_layout(
        height=380, margin=dict(l=0, r=0, t=20, b=0),
        yaxis_title="Monto a cobrar ($)",
        xaxis_title="Semana (inicio)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
        hovermode="x unified",
    )
    st.plotly_chart(fig_proy, use_container_width=True)

    total_8sem = proy["monto"].sum()
    primera_sem = proy.iloc[0]["monto"] if not proy.empty else 0
    pk1, pk2 = st.columns(2)
    with pk1:
        st.metric("Ingresos esperados (8 sem.)", fmt_cop(total_8sem))
    with pk2:
        st.metric(
            "A cobrar esta semana (incl. vencido)",
            fmt_cop(primera_sem),
        )
    st.caption(
        "ℹ️ La proyección usa la fecha de vencimiento. Si tus clientes "
        "suelen pagar tarde, el ingreso real llegará algo más diferido."
    )

st.markdown("---")


# ===========================================================================
# 📊 AGING + 🔁 ROTACIÓN
# ===========================================================================
col_ag, col_rot = st.columns([1, 1])

with col_ag:
    st.markdown("### 📊 Aging de Cartera")
    st.caption("Antigüedad de los saldos por cobrar.")
    aging = compute_aging(enriched)
    if aging is not None and not aging.empty:
        color_map = {
            "Por vencer": "#2ECC71",
            "1-30 días": "#F1C40F",
            "31-60 días": "#E67E22",
            "61-90 días": "#E74C3C",
            "+90 días": "#922B21",
            "Sin fecha": "#95A5A6",
        }
        fig_ag = px.bar(
            aging, x="bucket", y="monto",
            color="bucket", color_discrete_map=color_map,
            labels={"bucket": "", "monto": "Monto ($)"},
            text=aging["monto"].apply(fmt_cop),
        )
        fig_ag.update_layout(
            height=300, margin=dict(l=0, r=0, t=10, b=0),
            showlegend=False,
        )
        st.plotly_chart(fig_ag, use_container_width=True)
        st.dataframe(
            aging,
            column_config={
                "bucket": "Antigüedad",
                "monto": st.column_config.NumberColumn("Monto", format="$%.0f"),
                "n_facturas": st.column_config.NumberColumn("# Facturas", format="%d"),
                "pct": st.column_config.NumberColumn("% del total", format="%.1f%%"),
            },
            use_container_width=True, hide_index=True,
        )

with col_rot:
    st.markdown("### 🔁 Rotación de Cartera y DSO")
    st.caption("DSO = días promedio que tardas en cobrar a tus clientes.")
    with st.spinner("Calculando rotación..."):
        fecha_desde_ventas = (today - timedelta(days=365)).isoformat()
        try:
            ventas_lines = load_invoice_lines(
                date_from=fecha_desde_ventas,
                date_to=today.isoformat(),
                company_ids=company_ids,
            )
        except Exception:  # noqa: BLE001
            ventas_lines = pd.DataFrame()

    ventas_periodo = 0.0
    if ventas_lines is not None and not ventas_lines.empty:
        if "price_subtotal_signed" in ventas_lines.columns:
            ventas_periodo = float(ventas_lines["price_subtotal_signed"].sum())
        elif "price_subtotal" in ventas_lines.columns:
            ventas_periodo = float(ventas_lines["price_subtotal"].sum())

    cartera_actual = kpis["total_por_cobrar"]
    dso_res = compute_dso(cartera_actual, ventas_periodo, dias_periodo=365)

    rk1, rk2 = st.columns(2)
    with rk1:
        st.metric("DSO (días de cobro)", f"{dso_res['dso']:.0f} días")
    with rk2:
        st.metric("Rotación cartera", f"{dso_res['rotacion']:.1f}x")

    st.caption(
        f"Ventas último año: {fmt_cop(ventas_periodo)} · "
        f"Cartera actual: {fmt_cop(cartera_actual)}"
    )

    if dso_res["dso"] > 0:
        if dso_res["dso"] > 60:
            st.warning(
                "🟠 DSO alto: el dinero tarda mucho en volver. Refuerza la "
                "gestión de cobro y revisa los plazos que estás otorgando."
            )
        elif dso_res["dso"] < 20:
            st.info(
                "🔵 DSO bajo: cobras muy rápido. Buena liquidez; verifica "
                "que los plazos cortos no estén frenando ventas."
            )
        else:
            st.success("🟢 DSO en rango saludable (20-60 días).")

st.markdown("---")


# ===========================================================================
# 🏆 TOP CLIENTES
# ===========================================================================
st.markdown("### 🏆 Top Clientes por Saldo")

top_cli = compute_top_clientes(enriched, top_n=15)
if top_cli is not None and not top_cli.empty:
    fig_tc = go.Figure()
    fig_tc.add_trace(go.Bar(
        y=top_cli["partner_name"],
        x=top_cli["saldo_total"] - top_cli["saldo_vencido"],
        name="Por vencer", orientation="h", marker_color="#27AE60",
    ))
    fig_tc.add_trace(go.Bar(
        y=top_cli["partner_name"],
        x=top_cli["saldo_vencido"],
        name="Vencido", orientation="h", marker_color="#E74C3C",
    ))
    fig_tc.update_layout(
        barmode="stack", height=420,
        margin=dict(l=0, r=0, t=10, b=0),
        yaxis=dict(autorange="reversed"),
        xaxis_title="Saldo ($)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    st.plotly_chart(fig_tc, use_container_width=True)

    st.dataframe(
        top_cli[[
            "partner_name", "saldo_total", "saldo_vencido",
            "n_facturas", "pct_concentracion",
        ]],
        column_config={
            "partner_name": "Cliente",
            "saldo_total": st.column_config.NumberColumn(
                "Saldo total", format="$%.0f",
            ),
            "saldo_vencido": st.column_config.NumberColumn(
                "Saldo vencido", format="$%.0f",
            ),
            "n_facturas": st.column_config.NumberColumn(
                "# Facturas", format="%d",
            ),
            "pct_concentracion": st.column_config.NumberColumn(
                "% concentración", format="%.1f%%",
            ),
        },
        use_container_width=True, hide_index=True,
    )

st.markdown("---")


# ===========================================================================
# 📋 TABLA DETALLADA + EXPORTACIÓN
# ===========================================================================
st.markdown("### 📋 Detalle de Facturas por Cobrar")

detalle = build_tabla_detalle(enriched)

if detalle is not None and not detalle.empty:
    fcol1, fcol2 = st.columns(2)
    with fcol1:
        estados = ["(Todos)"] + sorted(detalle["estado"].dropna().unique().tolist())
        estado_sel = st.selectbox("Estado", estados, key="cxc_estado")
    with fcol2:
        clientes = ["(Todos)"] + sorted(
            detalle["partner_name"].dropna().unique().tolist()
        )
        cliente_sel = st.selectbox("Cliente", clientes, key="cxc_cliente")

    det_show = detalle.copy()
    if estado_sel != "(Todos)":
        det_show = det_show[det_show["estado"] == estado_sel]
    if cliente_sel != "(Todos)":
        det_show = det_show[det_show["partner_name"] == cliente_sel]

    st.caption(
        f"{len(det_show):,} facturas · "
        f"Saldo: {fmt_cop(det_show['saldo'].sum())}"
    )
    st.dataframe(
        det_show,
        column_config={
            "name": "Factura",
            "ref": "Referencia",
            "partner_name": "Cliente",
            "invoice_date": st.column_config.DateColumn(
                "Fecha", format="YYYY-MM-DD",
            ),
            "invoice_date_due": st.column_config.DateColumn(
                "Vencimiento", format="YYYY-MM-DD",
            ),
            "dias_para_vencer": st.column_config.NumberColumn(
                "Días", format="%d",
            ),
            "dias_mora": st.column_config.NumberColumn(
                "Días mora", format="%d",
            ),
            "estado": "Estado",
            "total_factura": st.column_config.NumberColumn(
                "Total", format="$%.0f",
            ),
            "saldo": st.column_config.NumberColumn(
                "Saldo", format="$%.0f",
            ),
            "bucket_aging": "Antigüedad",
            "payment_term_name": "Término pago",
            "company_id_name": "Empresa",
        },
        use_container_width=True, hide_index=True, height=420,
    )

    # ── Exportación a Excel ──
    def _generar_excel_cxc() -> bytes:
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            resumen = pd.DataFrame([
                {"Indicador": "Total por cobrar", "Valor": kpis["total_por_cobrar"]},
                {"Indicador": "Cartera vencida", "Valor": kpis["total_vencido"]},
                {"Indicador": "% vencido", "Valor": kpis["pct_vencido"]},
                {"Indicador": f"Vence en {horizonte} días",
                 "Valor": kpis["total_por_cobrar_h"]},
                {"Indicador": "# Facturas", "Valor": kpis["n_facturas"]},
                {"Indicador": "# Clientes", "Valor": kpis["n_clientes"]},
                {"Indicador": "# Clientes en mora", "Valor": kpis["n_clientes_mora"]},
                {"Indicador": "Mora promedio (días)",
                 "Valor": kpis["mora_promedio_dias"]},
                {"Indicador": "DSO (días)", "Valor": dso_res["dso"]},
                {"Indicador": "Rotación cartera", "Valor": dso_res["rotacion"]},
            ])
            resumen.to_excel(writer, sheet_name="Resumen", index=False)
            detalle.to_excel(writer, sheet_name="Detalle CxC", index=False)
            if morosos is not None and not morosos.empty:
                morosos.to_excel(
                    writer, sheet_name="Clientes en Mora", index=False,
                )
            if aging is not None and not aging.empty:
                aging.to_excel(writer, sheet_name="Aging", index=False)
            if top_cli is not None and not top_cli.empty:
                top_cli.to_excel(
                    writer, sheet_name="Top Clientes", index=False,
                )
            if proy is not None and not proy.empty:
                proy.to_excel(
                    writer, sheet_name="Proyeccion Ingresos", index=False,
                )
        buffer.seek(0)
        return buffer.getvalue()

    st.download_button(
        label="⬇️ Descargar informe de cuentas por cobrar (Excel)",
        data=_generar_excel_cxc(),
        file_name=f"cuentas_por_cobrar_{today.isoformat()}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
        use_container_width=True,
    )
