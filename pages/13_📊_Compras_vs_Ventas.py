# -*- coding: utf-8 -*-
"""
Página: Compras vs Ventas y Rotación de Inventario.

Cruza facturas de proveedor, facturas de cliente y stock actual para
analizar:
  - Qué compré y qué vendí en el período.
  - Productos comprados que no se vendieron.
  - Stock muerto (con inventario y sin ventas en 90 días).
  - Productos para comprar más (alta rotación + cobertura baja).
  - Productos con tendencia creciente.
  - KPIs de rotación general y por categoría.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from src.auth import logout_button, require_auth
from src.data_loader import (
    load_companies,
    load_invoice_lines,
    load_purchase_invoice_lines,
    load_stock_quants,
)
from src.purchases_analyzer import (
    compute_category_crosstab,
    compute_monthly_evolution,
    compute_product_crosstab,
    compute_purchases_vs_sales_summary,
    find_dead_stock,
    find_purchased_not_sold,
    find_to_purchase,
    find_trending_up,
)
from src.ui_components import render_company_context, render_sidebar_filters


st.set_page_config(
    page_title="Compras vs Ventas | Cartera",
    page_icon="📊",
    layout="wide",
)

require_auth()
logout_button()

st.title("📊 Compras vs Ventas e Inventario")
st.caption(
    "Cruce de facturas de proveedor, facturas de cliente y stock actual. "
    "Analiza rotación, identifica stock muerto y productos por comprar."
)

# Sidebar
filters = render_sidebar_filters()
if filters["company_ids"] is not None and len(filters["company_ids"]) == 0:
    st.warning("Selecciona al menos una empresa.")
    st.stop()


# ── Período ──
st.markdown("### 🗓️ Período de análisis")
col_p1, col_p2, col_p3 = st.columns([1, 1, 2])
today = date.today()

with col_p1:
    fecha_desde = st.date_input(
        "Desde",
        value=today.replace(day=1) - timedelta(days=180),
        key="cv_desde",
    )
with col_p2:
    fecha_hasta = st.date_input("Hasta", value=today, key="cv_hasta")
with col_p3:
    quick = st.radio(
        "Atajos",
        options=[
            "Personalizado", "Mes actual", "Mes anterior", "Trimestre actual",
            "Últimos 90 días", "Año en curso", "Últimos 12 meses",
        ],
        index=5, horizontal=False, key="cv_atajo",
    )

if quick != "Personalizado":
    if quick == "Año en curso":
        fecha_desde, fecha_hasta = today.replace(month=1, day=1), today
    elif quick == "Últimos 12 meses":
        fecha_desde, fecha_hasta = today - timedelta(days=365), today
    elif quick == "Últimos 90 días":
        fecha_desde, fecha_hasta = today - timedelta(days=90), today
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

st.caption(f"📅 Período: **{fecha_desde}** → **{fecha_hasta}**")
periodo_dias = (fecha_hasta - fecha_desde).days + 1
fecha_desde_prev = fecha_desde - timedelta(days=periodo_dias)
fecha_hasta_prev = fecha_desde - timedelta(days=1)


# ── Carga de datos ──
companies_df = load_companies()
render_company_context(companies_df, filters["company_ids"])

with st.spinner("Cargando compras, ventas y stock..."):
    # Ventas: cubren período actual + previo (para tendencias)
    sales_lines = load_invoice_lines(
        months_back=max(int(periodo_dias / 30) * 2 + 4, 12),
        company_ids=filters["company_ids"],
    )
    # Compras del período + previo
    purchases_lines = load_purchase_invoice_lines(
        date_from=fecha_desde_prev.isoformat(),
        date_to=fecha_hasta.isoformat(),
        company_ids=filters["company_ids"],
    )
    # Stock actual
    stock_df = load_stock_quants(company_ids=filters["company_ids"])

if (
    (sales_lines is None or sales_lines.empty)
    and (purchases_lines is None or purchases_lines.empty)
):
    st.error(
        "No se pudieron cargar líneas de factura. Verifica la conexión "
        "con Odoo o el período seleccionado."
    )
    st.stop()

# ── KPIs cabecera ──
summary = compute_purchases_vs_sales_summary(
    purchases_lines, sales_lines, stock_df, fecha_desde, fecha_hasta,
)
summary_prev = compute_purchases_vs_sales_summary(
    purchases_lines, sales_lines, stock_df, fecha_desde_prev, fecha_hasta_prev,
)


def _money(v: float) -> str:
    if v is None or pd.isna(v):
        return "—"
    return f"${v:,.0f}"


def _delta(act: float, prev: float) -> str:
    delta = act - prev
    pct = (delta / abs(prev) * 100) if prev else (100.0 if act else 0.0)
    return f"{delta:+,.0f} ({pct:+.1f}%)"


k1, k2, k3, k4 = st.columns(4)
with k1:
    st.metric(
        "🛒 Compras",
        _money(summary["total_compras"]),
        delta=_delta(summary["total_compras"], summary_prev["total_compras"]),
        delta_color="off",
    )
with k2:
    st.metric(
        "💰 Ventas",
        _money(summary["total_ventas"]),
        delta=_delta(summary["total_ventas"], summary_prev["total_ventas"]),
    )
with k3:
    st.metric(
        "📈 Margen bruto",
        _money(summary["margen_bruto"]),
        delta=f"{summary['margen_pct']:.1f}%",
        delta_color="off",
    )
with k4:
    st.metric(
        "🔄 Rotación anual",
        f"{summary['rotacion_general']:.2f}x",
        delta=(
            f"{summary['dias_inventario_general']:.0f} días inv."
            if summary["rotacion_general"] else "sin stock valorado"
        ),
        delta_color="off",
    )

k5, k6, k7, k8 = st.columns(4)
with k5:
    st.metric("# productos comprados", f"{summary['n_productos_comprados']:,}")
with k6:
    st.metric("# productos vendidos", f"{summary['n_productos_vendidos']:,}")
with k7:
    st.metric("# productos en stock", f"{summary['n_productos_stock']:,}")
with k8:
    st.metric("💎 Stock valorado", _money(summary["stock_value"]))

st.markdown("---")


# ── Calcular crosstabs (período actual) ──
with st.spinner("Cruzando compras, ventas y stock por producto..."):
    crosstab = compute_product_crosstab(
        purchases_lines, sales_lines, stock_df, fecha_desde, fecha_hasta,
    )
    cat_tab = compute_category_crosstab(crosstab)
    monthly = compute_monthly_evolution(
        purchases_lines, sales_lines, fecha_desde, fecha_hasta,
    )


# ── Sub-pestañas ──
t_res, t_cat, t_prod, t_sin, t_muerto, t_comprar, t_trend, t_rot = st.tabs([
    "📊 Resumen",
    "🏷️ Por categoría",
    "📦 Por producto",
    "❓ Sin ventas",
    "💀 Stock muerto",
    "🛒 Comprar más",
    "🚀 Tendencia ↑",
    "🔄 Rotación",
])


# ─── Tab Resumen ───
with t_res:
    st.markdown("### Evolución mensual: Compras vs Ventas")
    if monthly.empty:
        st.info("Sin datos suficientes para evolución mensual.")
    else:
        mm = monthly.copy()
        mm["mes_label"] = pd.to_datetime(mm["mes"]).dt.strftime("%Y-%m")
        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=mm["mes_label"], y=mm["compras"],
            name="Compras", marker_color="#a855f7",
        ))
        fig.add_trace(go.Bar(
            x=mm["mes_label"], y=mm["ventas"],
            name="Ventas", marker_color="#10b981",
        ))
        fig.add_trace(go.Scatter(
            x=mm["mes_label"], y=mm["gap"],
            name="Gap (compras − ventas)",
            line=dict(color="#ef4444", width=2, dash="dot"),
            mode="lines+markers",
        ))
        fig.update_layout(
            barmode="group", height=420,
            margin=dict(l=0, r=0, t=10, b=0),
            yaxis=dict(tickformat=",.0f"),
            legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0),
        )
        st.plotly_chart(fig, use_container_width=True)

        with st.expander("📋 Tabla mensual"):
            mm_show = mm[["mes_label", "compras", "ventas", "gap"]].rename(
                columns={"mes_label": "Mes"}
            )
            st.dataframe(
                mm_show,
                column_config={
                    "compras": st.column_config.NumberColumn(
                        "Compras", format="$%,.0f"
                    ),
                    "ventas": st.column_config.NumberColumn(
                        "Ventas", format="$%,.0f"
                    ),
                    "gap": st.column_config.NumberColumn(
                        "Gap", format="$%,.0f"
                    ),
                },
                use_container_width=True, hide_index=True,
            )

    st.markdown("### 🏷️ Top categorías por monto de ventas")
    if not cat_tab.empty:
        top_cats = cat_tab.head(10)
        figc = px.bar(
            top_cats.sort_values("monto_ventas"),
            x="monto_ventas", y="product_categ_name",
            orientation="h", color_discrete_sequence=["#10b981"],
            text="monto_ventas",
        )
        figc.update_traces(
            texttemplate="%{text:,.0f}", textposition="outside",
        )
        figc.update_layout(
            height=400, margin=dict(l=0, r=0, t=10, b=0),
            yaxis=dict(title=""), xaxis=dict(tickformat=",.0f"),
        )
        st.plotly_chart(figc, use_container_width=True)


# ─── Tab Por categoría ───
with t_cat:
    st.markdown("### Cruce compras vs ventas por categoría")
    if cat_tab.empty:
        st.info("Sin datos por categoría.")
    else:
        # Doble barra compras vs ventas
        top_n = min(15, len(cat_tab))
        cat_top = cat_tab.head(top_n).copy()
        fig_cv = go.Figure()
        fig_cv.add_trace(go.Bar(
            x=cat_top["product_categ_name"], y=cat_top["monto_compras"],
            name="Compras", marker_color="#a855f7",
        ))
        fig_cv.add_trace(go.Bar(
            x=cat_top["product_categ_name"], y=cat_top["monto_ventas"],
            name="Ventas", marker_color="#10b981",
        ))
        fig_cv.update_layout(
            barmode="group", height=440,
            margin=dict(l=0, r=0, t=10, b=0),
            yaxis=dict(tickformat=",.0f"),
            xaxis=dict(tickangle=-30),
            legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0),
        )
        st.plotly_chart(fig_cv, use_container_width=True)

        st.markdown("### Tabla detallada por categoría")
        st.dataframe(
            cat_tab,
            column_config={
                "product_categ_name": st.column_config.TextColumn(
                    "Categoría", width="large"
                ),
                "n_productos": st.column_config.NumberColumn(
                    "# Prods", format="%d"
                ),
                "qty_comprada": st.column_config.NumberColumn(
                    "Qty comprada", format="%,.1f"
                ),
                "monto_compras": st.column_config.NumberColumn(
                    "$ Compras", format="$%,.0f"
                ),
                "qty_vendida": st.column_config.NumberColumn(
                    "Qty vendida", format="%,.1f"
                ),
                "monto_ventas": st.column_config.NumberColumn(
                    "$ Ventas", format="$%,.0f"
                ),
                "costo_ventas": st.column_config.NumberColumn(
                    "$ Costo", format="$%,.0f"
                ),
                "margen": st.column_config.NumberColumn(
                    "Margen", format="$%,.0f"
                ),
                "margen_pct": st.column_config.NumberColumn(
                    "% Margen", format="%.1f%%"
                ),
                "stock_qty": st.column_config.NumberColumn(
                    "Stock qty", format="%,.1f"
                ),
                "stock_valor": st.column_config.NumberColumn(
                    "Stock $", format="$%,.0f"
                ),
                "gap_monto": st.column_config.NumberColumn(
                    "Gap ($)", format="$%,.0f"
                ),
                "rotacion_anual": st.column_config.NumberColumn(
                    "Rot. anual", format="%.2fx"
                ),
                "dias_inventario": st.column_config.NumberColumn(
                    "Días inv.", format="%.0f"
                ),
            },
            use_container_width=True, hide_index=True, height=500,
        )


# ─── Tab Por producto ───
with t_prod:
    st.markdown("### Cruce compras vs ventas por producto")
    if crosstab.empty:
        st.info("Sin datos.")
    else:
        cflt1, cflt2, cflt3 = st.columns([2, 2, 1])
        with cflt1:
            cats = ["(Todas)"] + sorted(
                crosstab["product_categ_name"].dropna().unique().tolist()
            )
            cat_sel = st.selectbox("Categoría", options=cats, key="cv_cat_prod")
        with cflt2:
            q = st.text_input("Buscar (código o nombre)", key="cv_buscar_prod")
        with cflt3:
            modo = st.radio(
                "Mostrar",
                options=["Todos", "Solo vendidos", "Solo comprados"],
                key="cv_modo_prod",
            )

        df = crosstab.copy()
        if cat_sel and cat_sel != "(Todas)":
            df = df[df["product_categ_name"] == cat_sel]
        if q:
            ql = q.lower()
            mask = (
                df["product_default_code"].astype(str).str.lower().str.contains(ql, na=False)
                | df["product_name"].astype(str).str.lower().str.contains(ql, na=False)
            )
            df = df[mask]
        if modo == "Solo vendidos":
            df = df[df["qty_vendida"] > 0]
        elif modo == "Solo comprados":
            df = df[df["qty_comprada"] > 0]

        st.caption(f"{len(df):,} productos")
        st.dataframe(
            df[[
                "product_default_code", "product_name", "product_categ_name",
                "qty_comprada", "monto_compras",
                "qty_vendida", "monto_ventas", "margen", "margen_pct",
                "stock_qty", "dias_cobertura", "rotacion_anual",
            ]],
            column_config={
                "product_default_code": "Código",
                "product_name": st.column_config.TextColumn(
                    "Producto", width="large"
                ),
                "product_categ_name": "Categoría",
                "qty_comprada": st.column_config.NumberColumn(
                    "Qty comprada", format="%,.1f"
                ),
                "monto_compras": st.column_config.NumberColumn(
                    "$ Compras", format="$%,.0f"
                ),
                "qty_vendida": st.column_config.NumberColumn(
                    "Qty vendida", format="%,.1f"
                ),
                "monto_ventas": st.column_config.NumberColumn(
                    "$ Ventas", format="$%,.0f"
                ),
                "margen": st.column_config.NumberColumn(
                    "Margen", format="$%,.0f"
                ),
                "margen_pct": st.column_config.NumberColumn(
                    "% Margen", format="%.1f%%"
                ),
                "stock_qty": st.column_config.NumberColumn(
                    "Stock", format="%,.1f"
                ),
                "dias_cobertura": st.column_config.NumberColumn(
                    "Días cobertura", format="%.0f"
                ),
                "rotacion_anual": st.column_config.NumberColumn(
                    "Rot. anual", format="%.2fx"
                ),
            },
            use_container_width=True, hide_index=True, height=550,
        )


# ─── Tab Sin ventas ───
with t_sin:
    st.markdown("### ❓ Productos comprados en el período sin ventas")
    st.caption(
        "Referencias que entraron al inventario en este período pero no se "
        "vendieron en él. Útil para revisar surtido y forecasting."
    )
    sin = find_purchased_not_sold(crosstab)
    if sin.empty:
        st.success("✅ Todos los productos comprados se vendieron en el período.")
    else:
        st.metric(
            "💸 Capital comprado sin vender",
            _money(float(sin["monto_compras"].sum())),
            delta=f"{len(sin):,} referencias",
            delta_color="off",
        )
        st.dataframe(
            sin[[
                "product_default_code", "product_name", "product_categ_name",
                "qty_comprada", "monto_compras", "stock_qty",
            ]],
            column_config={
                "product_default_code": "Código",
                "product_name": st.column_config.TextColumn(
                    "Producto", width="large"
                ),
                "product_categ_name": "Categoría",
                "qty_comprada": st.column_config.NumberColumn(
                    "Qty comprada", format="%,.1f"
                ),
                "monto_compras": st.column_config.NumberColumn(
                    "$ Comprado", format="$%,.0f"
                ),
                "stock_qty": st.column_config.NumberColumn(
                    "Stock actual", format="%,.1f"
                ),
            },
            use_container_width=True, hide_index=True, height=500,
        )


# ─── Tab Stock muerto ───
with t_muerto:
    st.markdown("### 💀 Stock muerto")
    days = st.slider(
        "Definir como stock muerto si no hay ventas en los últimos…",
        min_value=30, max_value=365, value=90, step=15, key="cv_dias_muerto",
    )
    muerto = find_dead_stock(crosstab, sales_lines, today, days_no_sale=days)
    st.caption(
        f"Productos con stock > 0 y sin ninguna venta en los últimos {days} días."
    )
    if muerto.empty:
        st.success("✅ Sin stock muerto bajo este criterio.")
    else:
        st.metric(
            "💎 Capital inmovilizado",
            _money(float(muerto["stock_valor"].sum())),
            delta=f"{len(muerto):,} referencias · {muerto['stock_qty'].sum():,.0f} unidades",
            delta_color="off",
        )
        st.dataframe(
            muerto[[
                "product_default_code", "product_name", "product_categ_name",
                "stock_qty", "stock_valor", "qty_vendida", "qty_comprada",
            ]],
            column_config={
                "product_default_code": "Código",
                "product_name": st.column_config.TextColumn(
                    "Producto", width="large"
                ),
                "product_categ_name": "Categoría",
                "stock_qty": st.column_config.NumberColumn(
                    "Stock", format="%,.1f"
                ),
                "stock_valor": st.column_config.NumberColumn(
                    "Valor stock", format="$%,.0f"
                ),
                "qty_vendida": st.column_config.NumberColumn(
                    f"Vendida ({periodo_dias}d)", format="%,.1f"
                ),
                "qty_comprada": st.column_config.NumberColumn(
                    f"Comprada ({periodo_dias}d)", format="%,.1f"
                ),
            },
            use_container_width=True, hide_index=True, height=500,
        )


# ─── Tab Comprar más ───
with t_comprar:
    st.markdown("### 🛒 Productos para comprar más")
    cob = st.slider(
        "Umbral: stock que dura menos de X días al ritmo actual",
        min_value=7, max_value=90, value=30, step=7, key="cv_cob_max",
    )
    suger = find_to_purchase(crosstab, cobertura_max_dias=cob)
    st.caption(
        f"Productos con alta rotación cuyo stock duraría menos de {cob} días "
        "al ritmo de ventas del período. Sugerencia: comprar para cubrir "
        f"{cob * 2} días."
    )
    if suger.empty:
        st.info("✅ Sin riesgo de quiebres bajo este umbral.")
    else:
        st.metric(
            "Referencias en riesgo de quiebre",
            f"{len(suger):,}",
            delta=f"{suger['unidades_sugeridas'].sum():,.0f} unidades sugeridas",
            delta_color="off",
        )
        st.dataframe(
            suger[[
                "product_default_code", "product_name", "product_categ_name",
                "stock_qty", "qty_vendida", "unidades_por_dia",
                "dias_cobertura", "unidades_sugeridas",
            ]],
            column_config={
                "product_default_code": "Código",
                "product_name": st.column_config.TextColumn(
                    "Producto", width="large"
                ),
                "product_categ_name": "Categoría",
                "stock_qty": st.column_config.NumberColumn(
                    "Stock", format="%,.1f"
                ),
                "qty_vendida": st.column_config.NumberColumn(
                    "Qty vendida", format="%,.1f"
                ),
                "unidades_por_dia": st.column_config.NumberColumn(
                    "Vel. (u/día)", format="%.2f"
                ),
                "dias_cobertura": st.column_config.NumberColumn(
                    "Días cobertura", format="%.0f"
                ),
                "unidades_sugeridas": st.column_config.NumberColumn(
                    "Unid. a comprar", format="%,.0f"
                ),
            },
            use_container_width=True, hide_index=True, height=500,
        )


# ─── Tab Tendencia ↑ ───
with t_trend:
    st.markdown("### 🚀 Productos con tendencia creciente")
    st.caption(
        f"Comparativo: período actual ({fecha_desde} → {fecha_hasta}) vs "
        f"anterior ({fecha_desde_prev} → {fecha_hasta_prev})."
    )
    min_g = st.slider(
        "Crecimiento mínimo (%)",
        min_value=10, max_value=200, value=30, step=10, key="cv_min_g",
    )
    # Para tendencia separamos ventas en actual y previo
    sl_full = sales_lines.copy() if sales_lines is not None else pd.DataFrame()
    if not sl_full.empty:
        sl_full["_d"] = pd.to_datetime(sl_full["invoice_date"], errors="coerce").dt.date
        sales_cur = sl_full[
            (sl_full["_d"] >= fecha_desde) & (sl_full["_d"] <= fecha_hasta)
        ].copy()
        sales_prv = sl_full[
            (sl_full["_d"] >= fecha_desde_prev) & (sl_full["_d"] <= fecha_hasta_prev)
        ].copy()
    else:
        sales_cur, sales_prv = pd.DataFrame(), pd.DataFrame()

    trend = find_trending_up(sales_cur, sales_prv, min_growth_pct=float(min_g))
    if trend.empty:
        st.info("Sin productos con crecimiento significativo en el período.")
    else:
        st.metric(
            f"Productos creciendo ≥ {min_g}%",
            f"{len(trend):,}",
            delta=_money(float(trend["delta_monto"].sum())),
            delta_color="off",
        )
        st.dataframe(
            trend[[
                "product_default_code", "product_name", "product_categ_name",
                "qty_prev", "qty_act",
                "monto_prev", "monto_act", "delta_monto", "pct_var_monto",
            ]],
            column_config={
                "product_default_code": "Código",
                "product_name": st.column_config.TextColumn(
                    "Producto", width="large"
                ),
                "product_categ_name": "Categoría",
                "qty_prev": st.column_config.NumberColumn(
                    "Qty prev", format="%,.1f"
                ),
                "qty_act": st.column_config.NumberColumn(
                    "Qty act", format="%,.1f"
                ),
                "monto_prev": st.column_config.NumberColumn(
                    "$ prev", format="$%,.0f"
                ),
                "monto_act": st.column_config.NumberColumn(
                    "$ act", format="$%,.0f"
                ),
                "delta_monto": st.column_config.NumberColumn(
                    "Δ $", format="$%,.0f"
                ),
                "pct_var_monto": st.column_config.NumberColumn(
                    "% Var", format="+%.1f%%"
                ),
            },
            use_container_width=True, hide_index=True, height=500,
        )


# ─── Tab Rotación ───
with t_rot:
    st.markdown("### 🔄 KPIs de rotación de inventario")
    st.caption(
        "Rotación = (ventas en cantidad) / (stock promedio) anualizada. "
        "Días de inventario = 365 / rotación. Cobertura = stock / velocidad."
    )

    kr1, kr2, kr3 = st.columns(3)
    with kr1:
        st.metric(
            "🔄 Rotación general (anual)",
            f"{summary['rotacion_general']:.2f}x",
        )
    with kr2:
        st.metric(
            "📅 Días de inventario",
            (
                f"{summary['dias_inventario_general']:.0f}"
                if summary["rotacion_general"] else "—"
            ),
        )
    with kr3:
        st.metric("💎 Stock valorado", _money(summary["stock_value"]))

    st.markdown("### Rotación por categoría")
    if cat_tab.empty:
        st.info("Sin datos por categoría.")
    else:
        cat_show = cat_tab[cat_tab["stock_valor"] > 0].copy()
        if cat_show.empty:
            st.info("No hay categorías con stock valorado.")
        else:
            # Gráfico de rotación
            fig_r = px.bar(
                cat_show.sort_values("rotacion_anual"),
                x="rotacion_anual", y="product_categ_name",
                orientation="h",
                color="rotacion_anual",
                color_continuous_scale="RdYlGn",
                text="rotacion_anual",
            )
            fig_r.update_traces(
                texttemplate="%{text:.2f}x", textposition="outside",
            )
            fig_r.update_layout(
                height=max(360, len(cat_show) * 30),
                margin=dict(l=0, r=0, t=10, b=0),
                yaxis=dict(title=""),
                xaxis=dict(title="Rotación anual (veces)"),
                coloraxis_showscale=False,
            )
            st.plotly_chart(fig_r, use_container_width=True)

            st.markdown("### Días de inventario por categoría")
            st.dataframe(
                cat_show[[
                    "product_categ_name", "n_productos", "stock_qty",
                    "stock_valor", "costo_ventas", "rotacion_anual",
                    "dias_inventario",
                ]],
                column_config={
                    "product_categ_name": st.column_config.TextColumn(
                        "Categoría", width="large"
                    ),
                    "n_productos": st.column_config.NumberColumn(
                        "# Prods", format="%d"
                    ),
                    "stock_qty": st.column_config.NumberColumn(
                        "Stock qty", format="%,.1f"
                    ),
                    "stock_valor": st.column_config.NumberColumn(
                        "Stock $", format="$%,.0f"
                    ),
                    "costo_ventas": st.column_config.NumberColumn(
                        "Costo ventas", format="$%,.0f"
                    ),
                    "rotacion_anual": st.column_config.NumberColumn(
                        "Rot. anual", format="%.2fx"
                    ),
                    "dias_inventario": st.column_config.NumberColumn(
                        "Días inv.", format="%.0f"
                    ),
                },
                use_container_width=True, hide_index=True, height=420,
            )
