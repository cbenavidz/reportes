# -*- coding: utf-8 -*-
"""
Página: Reportes Financieros — Dashboard ejecutivo.

Cuatro pilares:
  1. Resumen ejecutivo (top KPIs del período)
  2. Rentabilidad y márgenes (P&L, margen por categoría/producto)
  3. Crecimiento y estacionalidad (YoY, heatmap)
  4. Concentración y riesgo (Pareto, HHI, churn, slow movers)

Usa las líneas de factura ya enriquecidas con costo y margen
(extractor.py extrae standard_price y calcula line_cost / line_margin).
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from src.auth import logout_button, require_auth
from src.data_loader import compute_full_analysis, load_invoice_lines
from src.ui_components import render_company_context, render_sidebar_filters
from src.financial_analyzer import (
    compute_churn_clientes,
    compute_concentration_risk,
    compute_executive_summary,
    compute_margin_by_category,
    compute_negative_margin_products,
    compute_pareto_clientes,
    compute_pareto_productos,
    compute_pnl_monthly,
    compute_purchase_frequency,
    compute_seasonality_heatmap,
    compute_slow_movers,
    compute_top_products_by_margin,
    compute_yoy_growth,
)

st.set_page_config(
    page_title="Reportes Financieros | Cartera",
    page_icon="💼",
    layout="wide",
)

require_auth()
logout_button()

st.title("💼 Reportes Financieros")
st.caption(
    "Dashboard ejecutivo con KPIs de rentabilidad, crecimiento, concentración "
    "y eficiencia. Datos en vivo de Odoo (líneas de factura con costo)."
)

# Sidebar de filtros (empresas, etc.)
filters = render_sidebar_filters()
if filters["company_ids"] is not None and len(filters["company_ids"]) == 0:
    st.warning("Selecciona al menos una empresa en el sidebar.")
    st.stop()


# ---------------------------------------------------------------------------
# Filtro de período
# ---------------------------------------------------------------------------
st.markdown("### 🗓️ Período de análisis")
col_p1, col_p2, col_p3 = st.columns([1, 1, 2])

today = date.today()
default_from = today - timedelta(days=90)

with col_p1:
    fecha_desde = st.date_input("Desde", value=default_from, key="fin_desde")
with col_p2:
    fecha_hasta = st.date_input("Hasta", value=today, key="fin_hasta")
with col_p3:
    quick = st.radio(
        "Atajos",
        options=[
            "Personalizado", "Últimos 30 días", "Últimos 90 días",
            "Últimos 12 meses", "Año en curso", "Mes actual", "Mes anterior",
        ],
        index=2, horizontal=False, key="fin_atajo",
    )

if quick != "Personalizado":
    if quick == "Últimos 30 días":
        fecha_desde, fecha_hasta = today - timedelta(days=30), today
    elif quick == "Últimos 90 días":
        fecha_desde, fecha_hasta = today - timedelta(days=90), today
    elif quick == "Últimos 12 meses":
        fecha_desde, fecha_hasta = today - timedelta(days=365), today
    elif quick == "Año en curso":
        fecha_desde, fecha_hasta = today.replace(month=1, day=1), today
    elif quick == "Mes actual":
        fecha_desde, fecha_hasta = today.replace(day=1), today
    elif quick == "Mes anterior":
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        fecha_desde, fecha_hasta = last_prev.replace(day=1), last_prev

st.caption(f"📅 Período: **{fecha_desde}** → **{fecha_hasta}**")


# ---------------------------------------------------------------------------
# Cargar datos
# ---------------------------------------------------------------------------
# Usamos 24 meses de histórico para soportar YoY y churn de largo plazo
HIST_MONTHS = 24

with st.spinner("Cargando líneas de factura desde Odoo (24 meses)..."):
    lines = load_invoice_lines(
        months_back=HIST_MONTHS,
        company_ids=filters["company_ids"],
    )

# Banner de empresa(s) activa(s)
data_meta = compute_full_analysis(
    months_back=HIST_MONTHS,
    rotation_period_days=filters["period_days"],
    company_ids=filters["company_ids"],
    exclude_cash_sales=filters["exclude_cash_sales"],
    analysis_window_days=filters.get("analysis_window_days"),
)
render_company_context(data_meta.get("companies"), filters["company_ids"])

if lines is None or lines.empty:
    st.error("No se pudieron cargar líneas de factura. Verifica la conexión con Odoo.")
    st.stop()

# Verificar que tenemos datos de costo
total_cost = float(lines.get("line_cost", pd.Series([0])).sum())
total_sales = float(lines.get("price_subtotal_signed", pd.Series([0])).sum())
if total_cost == 0 and total_sales > 0:
    st.warning(
        "⚠️ No se detectaron costos en los productos. Para activar márgenes, "
        "verifica que `standard_price` esté configurado en Odoo y haz click "
        "en 'Recargar datos' para refrescar el caché."
    )

# Mostrar la fuente del costo/margen que se está usando (trazabilidad)
if "cost_source" in lines.columns and not lines.empty:
    fuentes_costo = lines["cost_source"].dropna().unique()
    fuentes_margen = lines["margin_source"].dropna().unique() if "margin_source" in lines.columns else []
    if "Enterprise" in str(fuentes_costo) or "Enterprise" in str(fuentes_margen):
        st.success(
            f"🎯 **Margen preciso (Enterprise)** — Origen: "
            f"costo desde **{', '.join(fuentes_costo)}**, "
            f"margen desde **{', '.join(fuentes_margen)}**. "
            "Los costos son los históricos al momento de la venta."
        )
    else:
        st.info(
            f"ℹ️ **Margen aproximado** — Origen: costo desde "
            f"**{', '.join(fuentes_costo)}**, margen "
            f"**{', '.join(fuentes_margen)}**. "
            "Usa el snapshot actual del costo (puede diferir del histórico). "
            "Para precisión total, instala el módulo `sale_margin` en Odoo."
        )


# ---------------------------------------------------------------------------
# Helpers de formato
# ---------------------------------------------------------------------------
def _fmt_money(v: float) -> str:
    return f"${v:,.0f}"


def _fmt_pct(v: float) -> str:
    return f"{v:.1f}%"


def _fmt_delta(actual: float, anterior: float | None) -> str | None:
    if anterior is None or anterior == 0 or pd.isna(anterior):
        return None
    pct = ((actual - anterior) / anterior) * 100
    return f"{pct:+.1f}%"


# ---------------------------------------------------------------------------
# 1. RESUMEN EJECUTIVO
# ---------------------------------------------------------------------------
st.markdown("---")
st.markdown("## 📊 Resumen Ejecutivo")

summary = compute_executive_summary(lines, fecha_desde, fecha_hasta)
actual = summary["actual"]
anterior = summary["anterior"]

st.caption(
    f"Comparativa: **{summary['periodo_actual'][0]}** → **{summary['periodo_actual'][1]}** "
    f"vs **{summary['periodo_anterior'][0]}** → **{summary['periodo_anterior'][1]}**"
)

c1, c2, c3, c4 = st.columns(4)
c1.metric(
    "💰 Ventas (sin IVA)",
    _fmt_money(actual["ventas"]),
    delta=_fmt_delta(actual["ventas"], anterior["ventas"]),
)
c2.metric(
    "📊 Margen bruto",
    _fmt_money(actual["margen"]),
    delta=_fmt_delta(actual["margen"], anterior["margen"]),
)
c3.metric(
    "📈 Margen %",
    _fmt_pct(actual["margen_pct"]),
    delta=_fmt_delta(actual["margen_pct"], anterior["margen_pct"]),
)
c4.metric(
    "💵 Costo",
    _fmt_money(actual["costo"]),
    delta=_fmt_delta(actual["costo"], anterior["costo"]),
)

c5, c6, c7, c8 = st.columns(4)
c5.metric(
    "🧾 # Facturas",
    f"{actual['n_facturas']:,}",
    delta=_fmt_delta(actual["n_facturas"], anterior["n_facturas"]),
)
c6.metric(
    "👥 Clientes activos",
    f"{actual['n_clientes']:,}",
    delta=_fmt_delta(actual["n_clientes"], anterior["n_clientes"]),
)
c7.metric(
    "📦 Productos vendidos",
    f"{actual['n_productos']:,}",
    delta=_fmt_delta(actual["n_productos"], anterior["n_productos"]),
)
c8.metric(
    "🎫 Ticket promedio",
    _fmt_money(actual["ticket_promedio"]),
    delta=_fmt_delta(actual["ticket_promedio"], anterior["ticket_promedio"]),
)


# ---------------------------------------------------------------------------
# 2. RENTABILIDAD Y MÁRGENES
# ---------------------------------------------------------------------------
st.markdown("---")
st.markdown("## 💎 Rentabilidad y Márgenes")

# 2.1 P&L mensual
pnl = compute_pnl_monthly(lines, fecha_desde, fecha_hasta)
if not pnl.empty:
    st.markdown("### 📅 P&L mensual")
    pnl_show = pnl.copy()
    pnl_show["mes_label"] = pnl_show["mes"].dt.strftime("%Y-%m")

    # Barras apiladas: costo + margen = ventas
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=pnl_show["mes_label"], y=pnl_show["costo"],
        name="Costo", marker_color="#94a3b8",
    ))
    fig.add_trace(go.Bar(
        x=pnl_show["mes_label"], y=pnl_show["margen"],
        name="Margen bruto", marker_color="#10b981",
    ))
    # Línea de margen %
    fig.add_trace(go.Scatter(
        x=pnl_show["mes_label"], y=pnl_show["margen_pct"],
        name="Margen %", yaxis="y2",
        line=dict(color="#f59e0b", width=3),
        mode="lines+markers",
    ))
    fig.update_layout(
        barmode="stack",
        yaxis=dict(title="Pesos", tickformat=",.0f"),
        yaxis2=dict(
            title="Margen %", overlaying="y", side="right",
            range=[0, max(pnl["margen_pct"].max() * 1.2, 50)],
            ticksuffix="%",
        ),
        height=400, margin=dict(l=0, r=0, t=20, b=0),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    st.plotly_chart(fig, use_container_width=True)

    # Tabla
    with st.expander("📋 Detalle P&L mensual", expanded=False):
        st.dataframe(
            pnl,
            column_config={
                "mes": st.column_config.DateColumn("Mes", format="YYYY-MM"),
                "ventas": st.column_config.NumberColumn("Ventas", format="$%,.0f"),
                "costo": st.column_config.NumberColumn("Costo", format="$%,.0f"),
                "margen": st.column_config.NumberColumn("Margen", format="$%,.0f"),
                "margen_pct": st.column_config.NumberColumn("Margen %", format="%.1f%%"),
                "n_facturas": st.column_config.NumberColumn("# Facturas", format="%,d"),
            },
            use_container_width=True, hide_index=True,
        )

# 2.2 Margen por categoría
margen_cat = compute_margin_by_category(lines, fecha_desde, fecha_hasta)
if not margen_cat.empty:
    st.markdown("### 🗂️ Margen por categoría de producto")
    col_a, col_b = st.columns([1, 2])
    with col_a:
        # Treemap por margen
        fig = px.treemap(
            margen_cat, path=["categoria"], values="margen",
            color="margen_pct",
            color_continuous_scale="RdYlGn",
            color_continuous_midpoint=margen_cat["margen_pct"].median(),
            title="Margen ($) por categoría",
        )
        fig.update_layout(height=400, margin=dict(l=0, r=0, t=40, b=0))
        st.plotly_chart(fig, use_container_width=True)
    with col_b:
        st.dataframe(
            margen_cat,
            column_config={
                "categoria": st.column_config.TextColumn("Categoría", width="medium"),
                "ventas": st.column_config.NumberColumn("Ventas", format="$%,.0f"),
                "costo": st.column_config.NumberColumn("Costo", format="$%,.0f"),
                "margen": st.column_config.NumberColumn("Margen", format="$%,.0f"),
                "margen_pct": st.column_config.NumberColumn("Margen %", format="%.1f%%"),
                "unidades": st.column_config.NumberColumn("Unid.", format="%,.0f"),
                "n_facturas": st.column_config.NumberColumn("Fact.", format="%,d"),
            },
            use_container_width=True, hide_index=True, height=400,
        )

# 2.3 Top 20 productos por margen
top_margin = compute_top_products_by_margin(lines, fecha_desde, fecha_hasta, n=20)
if not top_margin.empty:
    st.markdown("### 🏆 Top 20 productos por margen bruto")
    st.dataframe(
        top_margin,
        column_config={
            "product_id": None,  # ocultar id
            "product_name": st.column_config.TextColumn("Producto", width="large"),
            "ventas": st.column_config.NumberColumn("Ventas", format="$%,.0f"),
            "costo": st.column_config.NumberColumn("Costo", format="$%,.0f"),
            "margen": st.column_config.NumberColumn("Margen", format="$%,.0f"),
            "margen_pct": st.column_config.NumberColumn("Margen %", format="%.1f%%"),
            "unidades": st.column_config.NumberColumn("Unid.", format="%,.0f"),
        },
        use_container_width=True, hide_index=True,
    )

# 2.4 Productos con margen negativo (alerta)
neg_margin = compute_negative_margin_products(lines, fecha_desde, fecha_hasta)
if not neg_margin.empty:
    st.markdown("### ⚠️ Productos vendidos con margen negativo")
    st.warning(
        f"Hay **{len(neg_margin)}** productos que se vendieron por DEBAJO del costo "
        f"en este período. Pérdida total: **{_fmt_money(abs(neg_margin['margen'].sum()))}**"
    )
    st.dataframe(
        neg_margin,
        column_config={
            "product_id": None,
            "product_name": st.column_config.TextColumn("Producto", width="large"),
            "ventas": st.column_config.NumberColumn("Ventas", format="$%,.0f"),
            "costo": st.column_config.NumberColumn("Costo", format="$%,.0f"),
            "margen": st.column_config.NumberColumn("Margen", format="$%,.0f"),
            "margen_pct": st.column_config.NumberColumn("Margen %", format="%.1f%%"),
            "unidades": st.column_config.NumberColumn("Unid.", format="%,.0f"),
        },
        use_container_width=True, hide_index=True,
    )


# ---------------------------------------------------------------------------
# 3. CRECIMIENTO Y ESTACIONALIDAD
# ---------------------------------------------------------------------------
st.markdown("---")
st.markdown("## 📈 Crecimiento y Estacionalidad")

# 3.1 YoY 24 meses
yoy = compute_yoy_growth(lines, n_meses=24)
if not yoy.empty:
    st.markdown("### 📊 Tendencia 24 meses con YoY")
    yoy_show = yoy.copy()
    yoy_show["mes_label"] = yoy_show["mes"].dt.strftime("%Y-%m")

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=yoy_show["mes_label"], y=yoy_show["ventas"],
        name="Ventas", marker_color="#3b82f6",
    ))
    fig.add_trace(go.Scatter(
        x=yoy_show["mes_label"], y=yoy_show["ventas_yoy"],
        name="Año anterior", line=dict(color="#94a3b8", dash="dash"),
        mode="lines+markers",
    ))
    fig.update_layout(
        height=380, margin=dict(l=0, r=0, t=20, b=0),
        yaxis=dict(tickformat=",.0f"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    st.plotly_chart(fig, use_container_width=True)

    # Tabla con crecimiento %
    with st.expander("📋 Detalle YoY", expanded=False):
        show_yoy = yoy.copy()
        show_yoy["mes"] = show_yoy["mes"].dt.strftime("%Y-%m")
        st.dataframe(
            show_yoy,
            column_config={
                "mes": "Mes",
                "ventas": st.column_config.NumberColumn("Ventas", format="$%,.0f"),
                "mes_yoy": None,
                "ventas_yoy": st.column_config.NumberColumn("Año anterior", format="$%,.0f"),
                "crecimiento_yoy_pct": st.column_config.NumberColumn(
                    "Crecimiento YoY", format="%+.1f%%"
                ),
            },
            use_container_width=True, hide_index=True,
        )

# 3.2 Heatmap estacional
heatmap = compute_seasonality_heatmap(lines)
if not heatmap.empty:
    st.markdown("### 🌡️ Heatmap estacional (mes vs año)")
    meses_es = ["Ene", "Feb", "Mar", "Abr", "May", "Jun",
                "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
    heatmap_show = heatmap.copy()
    heatmap_show.columns = [meses_es[c - 1] for c in heatmap_show.columns]
    fig = px.imshow(
        heatmap_show, color_continuous_scale="Blues",
        labels=dict(x="Mes", y="Año", color="Ventas"),
        text_auto=".2s",
        aspect="auto",
    )
    fig.update_layout(height=300, margin=dict(l=0, r=0, t=20, b=0))
    st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# 4. CONCENTRACIÓN Y RIESGO
# ---------------------------------------------------------------------------
st.markdown("---")
st.markdown("## ⚖️ Concentración y Riesgo")

# 4.1 Métricas resumen de concentración
risk = compute_concentration_risk(lines, fecha_desde, fecha_hasta)
c1, c2, c3, c4 = st.columns(4)
c1.metric(
    "👥 Clientes A (80%)",
    f"{risk['n_clientes_a']:,}",
    help="Clientes que generan el 80% de las ventas",
)
c2.metric(
    "🎯 Top 5 clientes",
    f"{risk['top5_pct']:.1f}%",
    help="% de ventas concentradas en los 5 mejores clientes",
)
c3.metric(
    "🎯 Top 10 clientes",
    f"{risk['top10_pct']:.1f}%",
)
c4.metric(
    "⚠️ Riesgo Top 3",
    f"{risk['riesgo_top3']:.1f}%",
    help="Si pierdes a los 3 mejores clientes, pierdes este % de ventas",
)

# Interpretar HHI
hhi = risk["hhi"]
if hhi < 1500:
    hhi_label = "Baja concentración"
    hhi_color = "🟢"
elif hhi < 2500:
    hhi_label = "Concentración moderada"
    hhi_color = "🟡"
else:
    hhi_label = "Alta concentración (riesgo)"
    hhi_color = "🔴"
st.caption(f"**HHI** (Herfindahl-Hirschman): {hhi:,.0f} → {hhi_color} {hhi_label}")

# 4.2 Pareto clientes
pareto_cli = compute_pareto_clientes(lines, fecha_desde, fecha_hasta)
if not pareto_cli.empty:
    st.markdown("### 📊 Pareto de Clientes (80/20)")

    # Gráfica: barras de ventas + línea acumulada
    show_p = pareto_cli.head(30).copy()  # solo top 30 visibles
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=show_p["partner_name"], y=show_p["ventas"],
        name="Ventas", marker_color="#3b82f6",
    ))
    fig.add_trace(go.Scatter(
        x=show_p["partner_name"], y=show_p["pct_acumulado"],
        name="% Acumulado", yaxis="y2",
        line=dict(color="#ef4444", width=2),
        mode="lines+markers",
    ))
    fig.add_hline(
        y=80, line_dash="dash", line_color="#f59e0b",
        annotation_text="80%", yref="y2",
    )
    fig.update_layout(
        height=400, margin=dict(l=0, r=0, t=20, b=80),
        yaxis=dict(title="Ventas", tickformat=",.0f"),
        yaxis2=dict(title="% Acumulado", overlaying="y", side="right",
                    range=[0, 105], ticksuffix="%"),
        xaxis=dict(tickangle=-45),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    st.plotly_chart(fig, use_container_width=True)

    with st.expander(f"📋 Lista completa ({len(pareto_cli):,} clientes)", expanded=False):
        st.dataframe(
            pareto_cli,
            column_config={
                "partner_id": None,
                "rank": st.column_config.NumberColumn("#", format="%d"),
                "partner_name": st.column_config.TextColumn("Cliente", width="large"),
                "ventas": st.column_config.NumberColumn("Ventas", format="$%,.0f"),
                "margen": st.column_config.NumberColumn("Margen", format="$%,.0f"),
                "n_facturas": st.column_config.NumberColumn("# Fact.", format="%,d"),
                "pct": st.column_config.NumberColumn("% Total", format="%.2f%%"),
                "pct_acumulado": st.column_config.NumberColumn("% Acum.", format="%.1f%%"),
                "bucket": st.column_config.TextColumn("Bucket"),
            },
            use_container_width=True, hide_index=True, height=400,
        )

# 4.3 Pareto productos
pareto_prod = compute_pareto_productos(lines, fecha_desde, fecha_hasta)
if not pareto_prod.empty:
    with st.expander(
        f"📦 Pareto de Productos ({len(pareto_prod):,} productos)",
        expanded=False,
    ):
        st.dataframe(
            pareto_prod.head(50),
            column_config={
                "product_id": None,
                "rank": st.column_config.NumberColumn("#", format="%d"),
                "product_name": st.column_config.TextColumn("Producto", width="large"),
                "ventas": st.column_config.NumberColumn("Ventas", format="$%,.0f"),
                "margen": st.column_config.NumberColumn("Margen", format="$%,.0f"),
                "unidades": st.column_config.NumberColumn("Unid.", format="%,.0f"),
                "pct": st.column_config.NumberColumn("% Total", format="%.2f%%"),
                "pct_acumulado": st.column_config.NumberColumn("% Acum.", format="%.1f%%"),
                "bucket": st.column_config.TextColumn("Bucket"),
            },
            use_container_width=True, hide_index=True, height=400,
        )


# ---------------------------------------------------------------------------
# 5. EFICIENCIA OPERATIVA
# ---------------------------------------------------------------------------
st.markdown("---")
st.markdown("## ⚙️ Eficiencia Operativa")

freq = compute_purchase_frequency(lines, fecha_desde, fecha_hasta)
c1, c2, c3, c4 = st.columns(4)
c1.metric(
    "🔄 Frecuencia compra",
    f"{freq['frecuencia_promedio_dias']:.0f} días",
    help="Mediana de días entre compras de cada cliente recurrente",
)
c2.metric(
    "📦 Compras / cliente",
    f"{freq['compras_por_cliente']:.1f}",
    help="Promedio de compras (facturas) por cliente en el período",
)
c3.metric(
    "🎫 Ticket promedio",
    _fmt_money(freq["ticket_promedio"]),
)
c4.metric(
    "🔁 Clientes recurrentes",
    f"{freq['n_clientes_recurrentes']:,}",
    help="Clientes con 2+ compras en el período",
)

# 5.1 Slow movers
slow = compute_slow_movers(lines, today, days_threshold=90)
if not slow.empty:
    st.markdown(f"### 🐢 Productos sin movimiento (>90 días)")
    st.caption(
        f"Hay **{len(slow):,}** productos que vendieron alguna vez pero "
        "no han tenido ventas en los últimos 90 días. Considera "
        "promociones o liquidación."
    )
    with st.expander(f"📋 Ver lista ({len(slow):,} productos)", expanded=False):
        show_slow = slow.head(100).copy()
        show_slow["ultima_venta"] = pd.to_datetime(show_slow["ultima_venta"]).dt.strftime("%Y-%m-%d")
        st.dataframe(
            show_slow,
            column_config={
                "product_id": None,
                "product_name": st.column_config.TextColumn("Producto", width="large"),
                "ultima_venta": "Última venta",
                "dias_sin_vender": st.column_config.NumberColumn("Días", format="%d"),
                "ventas_total": st.column_config.NumberColumn("Ventas hist.", format="$%,.0f"),
                "unidades_total": st.column_config.NumberColumn("Unid.", format="%,.0f"),
            },
            use_container_width=True, hide_index=True,
        )

# 5.2 Churn de clientes
churn = compute_churn_clientes(lines, today, days_threshold=60)
if not churn.empty:
    st.markdown("### 🚨 Clientes en riesgo (sin comprar > 60 días)")
    valor_riesgo = float(churn["ventas_historicas"].sum())
    st.warning(
        f"Hay **{len(churn):,}** clientes que no compran hace más de 60 días. "
        f"Valor histórico de estos clientes: **{_fmt_money(valor_riesgo)}**. "
        "Considera contactarlos."
    )
    with st.expander(f"📋 Ver lista ({len(churn):,} clientes)", expanded=False):
        show_churn = churn.head(100).copy()
        show_churn["ultima_compra"] = pd.to_datetime(show_churn["ultima_compra"]).dt.strftime("%Y-%m-%d")
        show_churn["primera_compra"] = pd.to_datetime(show_churn["primera_compra"]).dt.strftime("%Y-%m-%d")
        st.dataframe(
            show_churn,
            column_config={
                "partner_id": None,
                "partner_name": st.column_config.TextColumn("Cliente", width="large"),
                "ultima_compra": "Última compra",
                "primera_compra": "Primera compra",
                "dias_inactivo": st.column_config.NumberColumn("Días inactivo", format="%d"),
                "ventas_historicas": st.column_config.NumberColumn(
                    "Ventas hist.", format="$%,.0f"
                ),
                "n_compras": st.column_config.NumberColumn("# Compras", format="%,d"),
            },
            use_container_width=True, hide_index=True,
        )
