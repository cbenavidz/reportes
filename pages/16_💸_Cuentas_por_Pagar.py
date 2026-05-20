# -*- coding: utf-8 -*-
"""
Página: Cuentas por Pagar.

Informe completo de cuentas por pagar a proveedores:
  - KPIs (total por pagar, vencido, próximos vencimientos, descuentos).
  - 🔔 Alertas de PRONTO PAGO: descuentos por pago anticipado vigentes.
  - 📅 Calendario de vencimientos (próximos 30 días).
  - 💧 Proyección de flujo de caja de pagos (semanal).
  - 📊 Aging de saldos (antigüedad de la deuda).
  - 🔁 Rotación de CxP y DPO (días promedio de pago).
  - 🏆 Top proveedores por saldo.
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
    load_payables,
    load_payment_terms,
    load_purchase_invoice_lines,
)
from src.payables_analyzer import (
    build_tabla_detalle,
    compute_aging,
    compute_calendar,
    compute_calendar_semanal,
    compute_dpo,
    compute_payables_kpis,
    compute_pronto_pago_alerts,
    compute_top_proveedores,
    enrich_payables,
)
from src.ui_components import render_company_context, render_sidebar_filters


st.set_page_config(
    page_title="Cuentas por Pagar | Cartera",
    page_icon="💸",
    layout="wide",
)

require_auth()
logout_button()

st.title("💸 Cuentas por Pagar")
st.caption(
    "Qué facturas pagar y cuándo, alertas de descuento por pronto pago, "
    "calendario de vencimientos, proyección de flujo y rotación de CxP."
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
from src.data_loader import load_companies  # noqa: E402

companies_df = load_companies()
render_company_context(companies_df, filters["company_ids"])


# ── Carga de datos ──
with st.spinner("Cargando facturas de proveedor..."):
    company_ids = (
        tuple(filters["company_ids"]) if filters["company_ids"] else None
    )
    payables = load_payables(company_ids=company_ids)
    payment_terms = load_payment_terms()

if payables is None or payables.empty:
    st.success(
        "🎉 No hay facturas de proveedor pendientes de pago. "
        "Todas las cuentas por pagar están al día."
    )
    st.stop()

# Enriquecer
enriched = enrich_payables(payables, payment_terms, today=today)
kpis = compute_payables_kpis(enriched, today=today, horizonte_dias=horizonte)


# ===========================================================================
# KPIs CABECERA
# ===========================================================================
st.markdown("### 📊 Resumen")

k1, k2, k3, k4 = st.columns(4)
with k1:
    st.metric("Total por pagar", fmt_cop(kpis["total_por_pagar"]))
    st.caption(f"{kpis['n_facturas']} facturas · {kpis['n_proveedores']} proveedores")
with k2:
    st.metric(
        "⚠️ Vencido", fmt_cop(kpis["total_vencido"]),
        delta=f"{kpis['n_vencidas']} facturas",
        delta_color="inverse",
    )
with k3:
    st.metric(
        f"Vence en {horizonte} días",
        fmt_cop(kpis["total_por_vencer_h"]),
    )
with k4:
    st.metric(
        "💰 Descuentos disponibles",
        fmt_cop(kpis["total_descuentos_vigentes"]),
        delta=f"{kpis['n_facturas_con_descuento']} facturas con dto.",
    )

# Alerta global de descuento urgente
urgente_dias = kpis.get("descuento_mas_urgente_dias")
if urgente_dias is not None and kpis["n_facturas_con_descuento"] > 0:
    if urgente_dias <= 0:
        st.error(
            f"🔴 Tienes descuentos por pronto pago que **vencen HOY**. "
            f"Revisa el panel de alertas abajo para no perderlos."
        )
    elif urgente_dias <= 3:
        st.warning(
            f"🟠 El descuento por pronto pago más urgente vence en "
            f"**{urgente_dias} día(s)**. Revisa el panel de alertas."
        )

st.markdown("---")


# ===========================================================================
# 🔔 ALERTAS DE PRONTO PAGO
# ===========================================================================
st.markdown("### 🔔 Alertas de Pronto Pago")
st.caption(
    "Facturas con descuento por pago anticipado todavía vigente. "
    "Páguelas antes de la fecha límite para capturar el descuento."
)

alertas = compute_pronto_pago_alerts(enriched, incluir_perdidos=False)

if alertas is None or alertas.empty:
    # Verificar si hay términos de pronto pago configurados
    tiene_pp = enriched["pronto_pago"].any() if "pronto_pago" in enriched.columns else False
    if not tiene_pp:
        st.info(
            "No se detectaron términos de pago con descuento por pronto pago "
            "(early payment discount) en Odoo. Si negocia descuentos por "
            "pago anticipado, configúrelos en Contabilidad → Configuración → "
            "Términos de pago, activando 'Descuento por pago anticipado'."
        )
    else:
        st.success(
            "✅ No hay descuentos por pronto pago vigentes pendientes "
            "de aprovechar en este momento."
        )
else:
    total_dto = alertas["monto_descuento"].sum()
    st.success(
        f"💡 Hay **{len(alertas)} facturas** con descuento vigente. "
        f"Ahorro potencial total: **{fmt_cop(total_dto)}**"
    )

    # Resaltar las más urgentes
    def _urgencia_emoji(d):
        if d <= 0:
            return "🔴 HOY"
        if d <= 3:
            return f"🟠 {int(d)}d"
        if d <= 7:
            return f"🟡 {int(d)}d"
        return f"🟢 {int(d)}d"

    alertas_show = alertas.copy()
    alertas_show["Urgencia"] = alertas_show["dias_para_dto"].apply(_urgencia_emoji)

    st.dataframe(
        alertas_show,
        column_config={
            "Urgencia": st.column_config.TextColumn("Urgencia", width="small"),
            "name": "Factura",
            "ref": "Referencia",
            "partner_name": "Proveedor",
            "invoice_date": st.column_config.DateColumn(
                "Fecha factura", format="YYYY-MM-DD",
            ),
            "invoice_date_due": st.column_config.DateColumn(
                "Vencimiento", format="YYYY-MM-DD",
            ),
            "fecha_limite_dto": st.column_config.DateColumn(
                "Límite descuento", format="YYYY-MM-DD",
            ),
            "dias_para_dto": st.column_config.NumberColumn(
                "Días restantes", format="%d",
            ),
            "saldo": st.column_config.NumberColumn(
                "Saldo", format="$%.0f",
            ),
            "dto_porcentaje": st.column_config.NumberColumn(
                "% Dto.", format="%.1f%%",
            ),
            "monto_descuento": st.column_config.NumberColumn(
                "Ahorro", format="$%.0f",
            ),
            "estado_dto": "Estado",
        },
        use_container_width=True, hide_index=True, height=320,
        column_order=[
            "Urgencia", "name", "partner_name", "invoice_date",
            "fecha_limite_dto", "dias_para_dto", "saldo",
            "dto_porcentaje", "monto_descuento",
        ],
    )

    # Descuentos perdidos (oportunidades dejadas pasar)
    perdidos = compute_pronto_pago_alerts(enriched, incluir_perdidos=True)
    if perdidos is not None and not perdidos.empty:
        perdidos = perdidos[perdidos["estado_dto"] == "Perdido"]
        if not perdidos.empty:
            con_saldo = perdidos[perdidos["saldo"].abs() > 1]
            if not con_saldo.empty:
                ahorro_perdido = con_saldo["monto_descuento"].sum()
                with st.expander(
                    f"⏰ {len(con_saldo)} descuentos ya vencidos "
                    f"(ahorro perdido: {fmt_cop(ahorro_perdido)})"
                ):
                    st.dataframe(
                        con_saldo[[
                            "name", "partner_name", "fecha_limite_dto",
                            "saldo", "monto_descuento",
                        ]],
                        column_config={
                            "name": "Factura",
                            "partner_name": "Proveedor",
                            "fecha_limite_dto": st.column_config.DateColumn(
                                "Venció el", format="YYYY-MM-DD",
                            ),
                            "saldo": st.column_config.NumberColumn(
                                "Saldo", format="$%.0f",
                            ),
                            "monto_descuento": st.column_config.NumberColumn(
                                "Descuento perdido", format="$%.0f",
                            ),
                        },
                        use_container_width=True, hide_index=True,
                    )

st.markdown("---")


# ===========================================================================
# 📅 CALENDARIO DE VENCIMIENTOS
# ===========================================================================
st.markdown(f"### 📅 Calendario de Vencimientos — Próximos {horizonte} días")

cal = compute_calendar(enriched, today=today, horizonte_dias=horizonte)


def render_calendario_html(cal_df: pd.DataFrame, ref_today: date,
                            dias: int) -> str:
    """Construye un calendario mensual en HTML con los montos por día."""
    # dict {date: (monto, n_facturas)}
    montos: dict[date, tuple[float, int]] = {}
    if cal_df is not None and not cal_df.empty:
        for _, r in cal_df.iterrows():
            d = pd.Timestamp(r["fecha"]).date()
            montos[d] = (float(r["monto"]), int(r["n_facturas"]))

    fin = ref_today + timedelta(days=dias)
    # Meses a renderizar
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
        # Días del mes
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
        # Encabezado
        html.append('<tr>')
        for d in dias_sem:
            html.append(
                f'<th style="padding:4px;color:#888;font-weight:600;">{d}</th>'
            )
        html.append('</tr>')

        # Primer día de la semana (0=lun)
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

            # Color de fondo según monto/urgencia
            bg = "transparent"
            contenido_monto = ""
            if info and info[0] > 0:
                dias_falta = (fecha_celda - ref_today).days
                if dias_falta <= 2:
                    bg = "#E74C3C"  # rojo
                    txt_color = "#fff"
                elif dias_falta <= 7:
                    bg = "#E67E22"  # naranja
                    txt_color = "#fff"
                elif dias_falta <= 15:
                    bg = "#F1C40F"  # amarillo
                    txt_color = "#000"
                else:
                    bg = "#3498DB"  # azul
                    txt_color = "#fff"
                monto_k = info[0] / 1000
                if monto_k >= 1000:
                    monto_str = f"${monto_k/1000:.1f}M"
                else:
                    monto_str = f"${monto_k:.0f}K"
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
        # Completar última fila
        while col < 7:
            html.append('<td></td>')
            col += 1
        html.append('</tr></table></div>')
    html.append('</div>')

    # Leyenda
    html.append(
        '<div style="margin-top:8px;font-size:11px;color:#888;">'
        '<span style="background:#E74C3C;color:#fff;padding:2px 6px;'
        'border-radius:3px;">≤2 días</span> '
        '<span style="background:#E67E22;color:#fff;padding:2px 6px;'
        'border-radius:3px;">3-7 días</span> '
        '<span style="background:#F1C40F;color:#000;padding:2px 6px;'
        'border-radius:3px;">8-15 días</span> '
        '<span style="background:#3498DB;color:#fff;padding:2px 6px;'
        'border-radius:3px;">+15 días</span></div>'
    )
    return "".join(html)


if cal is None or cal.empty:
    st.info(
        f"No hay facturas que venzan en los próximos {horizonte} días."
    )
else:
    st.markdown(
        render_calendario_html(cal, today, horizonte),
        unsafe_allow_html=True,
    )

    # Gráfico de barras diario
    st.markdown("#### Vencimientos por día")
    fig_cal = px.bar(
        cal, x="fecha", y="monto",
        labels={"fecha": "Fecha", "monto": "Monto a pagar"},
        hover_data=["n_facturas", "n_proveedores"],
    )
    fig_cal.update_traces(marker_color="#3498DB")
    fig_cal.update_layout(
        height=280, margin=dict(l=0, r=0, t=10, b=0),
        yaxis_title="Monto ($)",
    )
    st.plotly_chart(fig_cal, use_container_width=True)

st.markdown("---")


# ===========================================================================
# 💧 PROYECCIÓN DE FLUJO DE CAJA
# ===========================================================================
st.markdown("### 💧 Proyección de Flujo de Pagos — Semanal")
st.caption(
    "Cuánto dinero necesitas cada semana para cubrir los pagos. "
    "La primera barra incluye todo lo que ya está vencido."
)

cal_sem = compute_calendar_semanal(enriched, today=today, semanas=8)
if cal_sem is not None and not cal_sem.empty:
    fig_sem = go.Figure()
    colores = [
        "#E74C3C" if i == 0 else "#1ABC9C"
        for i in range(len(cal_sem))
    ]
    fig_sem.add_trace(go.Bar(
        x=cal_sem["semana_label"],
        y=cal_sem["monto"],
        marker_color=colores,
        text=[fmt_cop(m) for m in cal_sem["monto"]],
        textposition="outside",
        hovertext=[f"{n} facturas" for n in cal_sem["n_facturas"]],
    ))
    fig_sem.update_layout(
        height=320, margin=dict(l=0, r=0, t=20, b=0),
        yaxis_title="Monto a pagar ($)",
        xaxis_title="Semana (inicio)",
    )
    st.plotly_chart(fig_sem, use_container_width=True)

    total_8sem = cal_sem["monto"].sum()
    st.caption(f"Total a pagar en las próximas 8 semanas: **{fmt_cop(total_8sem)}**")

st.markdown("---")


# ===========================================================================
# 📊 AGING + 🔁 ROTACIÓN
# ===========================================================================
col_ag, col_rot = st.columns([1, 1])

with col_ag:
    st.markdown("### 📊 Aging de Saldos")
    st.caption("Antigüedad de la deuda con proveedores.")
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
    st.markdown("### 🔁 Rotación de CxP y DPO")
    st.caption(
        "DPO = días promedio que tardas en pagar a proveedores."
    )
    # Cargar compras del último año para el cálculo
    with st.spinner("Calculando rotación..."):
        fecha_desde_compras = (today - timedelta(days=365)).isoformat()
        try:
            compras_lines = load_purchase_invoice_lines(
                date_from=fecha_desde_compras,
                date_to=today.isoformat(),
                company_ids=company_ids,
            )
        except Exception:  # noqa: BLE001
            compras_lines = pd.DataFrame()

    compras_periodo = 0.0
    if compras_lines is not None and not compras_lines.empty:
        if "price_subtotal_signed" in compras_lines.columns:
            compras_periodo = float(compras_lines["price_subtotal_signed"].sum())
        elif "price_subtotal" in compras_lines.columns:
            compras_periodo = float(compras_lines["price_subtotal"].sum())

    cxp_actual = kpis["total_por_pagar"]
    dpo_res = compute_dpo(cxp_actual, compras_periodo, dias_periodo=365)

    rk1, rk2 = st.columns(2)
    with rk1:
        st.metric("DPO (días de pago)", f"{dpo_res['dpo']:.0f} días")
    with rk2:
        st.metric("Rotación CxP", f"{dpo_res['rotacion']:.1f}x")

    st.caption(
        f"Compras último año: {fmt_cop(compras_periodo)} · "
        f"CxP actual: {fmt_cop(cxp_actual)}"
    )

    if dpo_res["dpo"] > 0:
        if dpo_res["dpo"] < 30:
            st.info(
                "🔵 DPO bajo: pagas rápido a tus proveedores. Verifica si "
                "puedes negociar plazos más largos para mejorar tu liquidez."
            )
        elif dpo_res["dpo"] > 90:
            st.warning(
                "🟠 DPO alto: tardas bastante en pagar. Cuida la relación "
                "con proveedores y posibles intereses de mora."
            )
        else:
            st.success(
                "🟢 DPO en rango saludable (30-90 días)."
            )

st.markdown("---")


# ===========================================================================
# 🏆 TOP PROVEEDORES
# ===========================================================================
st.markdown("### 🏆 Top Proveedores por Saldo")

top_prov = compute_top_proveedores(enriched, top_n=15)
if top_prov is not None and not top_prov.empty:
    fig_tp = go.Figure()
    fig_tp.add_trace(go.Bar(
        y=top_prov["partner_name"],
        x=top_prov["saldo_total"] - top_prov["saldo_vencido"],
        name="Por vencer", orientation="h", marker_color="#3498DB",
    ))
    fig_tp.add_trace(go.Bar(
        y=top_prov["partner_name"],
        x=top_prov["saldo_vencido"],
        name="Vencido", orientation="h", marker_color="#E74C3C",
    ))
    fig_tp.update_layout(
        barmode="stack", height=420,
        margin=dict(l=0, r=0, t=10, b=0),
        yaxis=dict(autorange="reversed"),
        xaxis_title="Saldo ($)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    st.plotly_chart(fig_tp, use_container_width=True)

    st.dataframe(
        top_prov[[
            "partner_name", "saldo_total", "saldo_vencido",
            "n_facturas", "pct_concentracion",
        ]],
        column_config={
            "partner_name": "Proveedor",
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
st.markdown("### 📋 Detalle de Facturas por Pagar")

detalle = build_tabla_detalle(enriched)

if detalle is not None and not detalle.empty:
    # Filtros
    fcol1, fcol2, fcol3 = st.columns(3)
    with fcol1:
        estados = ["(Todos)"] + sorted(detalle["estado"].dropna().unique().tolist())
        estado_sel = st.selectbox("Estado", estados, key="cxp_estado")
    with fcol2:
        provs = ["(Todos)"] + sorted(
            detalle["partner_name"].dropna().unique().tolist()
        )
        prov_sel = st.selectbox("Proveedor", provs, key="cxp_prov")
    with fcol3:
        solo_dto = st.checkbox("Solo con descuento pronto pago", key="cxp_dto")

    det_show = detalle.copy()
    if estado_sel != "(Todos)":
        det_show = det_show[det_show["estado"] == estado_sel]
    if prov_sel != "(Todos)":
        det_show = det_show[det_show["partner_name"] == prov_sel]
    if solo_dto and "pronto_pago" in det_show.columns:
        det_show = det_show[det_show["pronto_pago"]]

    st.caption(
        f"{len(det_show):,} facturas · "
        f"Saldo: {fmt_cop(det_show['saldo'].sum())}"
    )
    st.dataframe(
        det_show,
        column_config={
            "name": "Factura",
            "ref": "Referencia",
            "partner_name": "Proveedor",
            "invoice_date": st.column_config.DateColumn(
                "Fecha", format="YYYY-MM-DD",
            ),
            "invoice_date_due": st.column_config.DateColumn(
                "Vencimiento", format="YYYY-MM-DD",
            ),
            "dias_para_vencer": st.column_config.NumberColumn(
                "Días", format="%d",
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
            "pronto_pago": st.column_config.CheckboxColumn("Pronto pago"),
            "fecha_limite_dto": st.column_config.DateColumn(
                "Límite dto.", format="YYYY-MM-DD",
            ),
            "dias_para_dto": st.column_config.NumberColumn(
                "Días dto.", format="%d",
            ),
            "dto_porcentaje": st.column_config.NumberColumn(
                "% Dto.", format="%.1f%%",
            ),
            "monto_descuento": st.column_config.NumberColumn(
                "Ahorro dto.", format="$%.0f",
            ),
            "estado_dto": "Estado dto.",
            "company_id_name": "Empresa",
        },
        use_container_width=True, hide_index=True, height=420,
    )

    # ── Exportación a Excel ──
    def _generar_excel_cxp() -> bytes:
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            # Hoja resumen
            resumen = pd.DataFrame([
                {"Indicador": "Total por pagar", "Valor": kpis["total_por_pagar"]},
                {"Indicador": "Total vencido", "Valor": kpis["total_vencido"]},
                {"Indicador": f"Vence en {horizonte} días",
                 "Valor": kpis["total_por_vencer_h"]},
                {"Indicador": "# Facturas", "Valor": kpis["n_facturas"]},
                {"Indicador": "# Proveedores", "Valor": kpis["n_proveedores"]},
                {"Indicador": "Descuentos disponibles",
                 "Valor": kpis["total_descuentos_vigentes"]},
                {"Indicador": "DPO (días)", "Valor": dpo_res["dpo"]},
                {"Indicador": "Rotación CxP", "Valor": dpo_res["rotacion"]},
            ])
            resumen.to_excel(writer, sheet_name="Resumen", index=False)
            detalle.to_excel(writer, sheet_name="Detalle CxP", index=False)
            if alertas is not None and not alertas.empty:
                alertas.to_excel(
                    writer, sheet_name="Alertas Pronto Pago", index=False,
                )
            if aging is not None and not aging.empty:
                aging.to_excel(writer, sheet_name="Aging", index=False)
            if top_prov is not None and not top_prov.empty:
                top_prov.to_excel(
                    writer, sheet_name="Top Proveedores", index=False,
                )
            if cal_sem is not None and not cal_sem.empty:
                cal_sem.to_excel(
                    writer, sheet_name="Proyeccion Semanal", index=False,
                )
        buffer.seek(0)
        return buffer.getvalue()

    st.download_button(
        label="⬇️ Descargar informe de cuentas por pagar (Excel)",
        data=_generar_excel_cxp(),
        file_name=f"cuentas_por_pagar_{today.isoformat()}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
        use_container_width=True,
    )
