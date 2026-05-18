# -*- coding: utf-8 -*-
"""
Página: Rotación de Inventario.

KPIs y análisis completo de rotación basado en:
  - Numerador: ventas del período (account.move.line de facturas).
  - Denominador: saldo cuenta 14 del balance contable (PUC colombiano).

Incluye:
  - KPIs cabecera (rotación, días, saldo, ventas).
  - Evolución mensual: ventas, saldo inventario, rotación anualizada.
  - Por categoría: rotación, días, margen.
  - Por producto: top rápidos, top lentos.
  - Recomendaciones automáticas accionables.
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
    load_chart_of_accounts,
    load_companies,
    load_inventory_balance_monthly_series,
    load_invoice_lines,
    load_purchase_invoice_lines,
    load_stock_quants,
)
from src.purchases_analyzer import (
    compute_category_crosstab,
    compute_inventory_recommendations,
    compute_product_crosstab,
    compute_purchases_vs_sales_summary,
    compute_rotacion_cuenta_14,
)
from src.financial_statements import enrich_chart_with_puc
from src.ui_components import render_company_context, render_sidebar_filters


st.set_page_config(
    page_title="Rotación de Inventario | Cartera",
    page_icon="🔄",
    layout="wide",
)

require_auth()
logout_button()

st.title("🔄 Rotación de Inventario")
st.caption(
    "Análisis completo de rotación: evolución mensual, por categoría, "
    "por producto y recomendaciones accionables. "
    "**Fórmula:** Ventas del período / Saldo cuenta 14 (Inventarios)."
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
        key="ri_desde",
    )
with col_p2:
    fecha_hasta = st.date_input("Hasta", value=today, key="ri_hasta")
with col_p3:
    quick = st.radio(
        "Atajos",
        options=[
            "Personalizado", "Mes actual", "Mes anterior", "Trimestre actual",
            "Últimos 90 días", "Año en curso", "Últimos 12 meses",
        ],
        index=6, horizontal=False, key="ri_atajo",
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


# ── Banner empresa ──
companies_df = load_companies()
render_company_context(companies_df, filters["company_ids"])


# ── Helpers ──
def _money(v) -> str:
    if v is None or pd.isna(v):
        return "—"
    return f"${v:,.0f}"


# ── Carga base ──
with st.spinner("Cargando plan de cuentas, ventas y compras..."):
    chart_df = load_chart_of_accounts(company_ids=filters["company_ids"])
    # Ventas: rango exacto (período previo → actual) — no más
    sales_lines = load_invoice_lines(
        company_ids=filters["company_ids"],
        date_from=fecha_desde_prev.isoformat(),
        date_to=fecha_hasta.isoformat(),
    )
    purchases_lines = load_purchase_invoice_lines(
        date_from=fecha_desde_prev.isoformat(),
        date_to=fecha_hasta.isoformat(),
        company_ids=filters["company_ids"],
    )
    stock_df = load_stock_quants(company_ids=filters["company_ids"])
    # Balance al corte del período actual y anterior (para KPIs)
    balances_corte = load_account_balances_aggregated(
        date_to=fecha_hasta.isoformat(),
        company_ids=filters["company_ids"],
    )
    balances_corte_prev = load_account_balances_aggregated(
        date_to=fecha_hasta_prev.isoformat(),
        company_ids=filters["company_ids"],
    )

if chart_df is None or chart_df.empty:
    st.error(
        "No se pudo cargar el plan de cuentas. La rotación requiere "
        "el saldo de la cuenta 14 desde el balance contable."
    )
    st.stop()


# ── Cálculo de summary y rotación ──
summary_act = compute_purchases_vs_sales_summary(
    purchases_lines, sales_lines, stock_df, fecha_desde, fecha_hasta,
)
summary_prev = compute_purchases_vs_sales_summary(
    purchases_lines, sales_lines, stock_df, fecha_desde_prev, fecha_hasta_prev,
)
rot14_act = compute_rotacion_cuenta_14(
    balances_corte, chart_df, summary_act["total_ventas"],
    fecha_desde, fecha_hasta,
)
rot14_prev = compute_rotacion_cuenta_14(
    balances_corte_prev, chart_df, summary_prev["total_ventas"],
    fecha_desde_prev, fecha_hasta_prev,
)


# ── KPIs cabecera ──
def _delta_pct(act: float, prev: float) -> str:
    if prev == 0:
        return "+100%" if act else "—"
    return f"{(act - prev) / abs(prev) * 100:+.1f}%"


k1, k2, k3, k4 = st.columns(4)
with k1:
    st.metric(
        "🔄 Rotación anual",
        f"{rot14_act['rotacion_anual']:.2f}x"
        if rot14_act["rotacion_anual"] else "—",
        delta=_delta_pct(
            rot14_act["rotacion_anual"], rot14_prev["rotacion_anual"]
        ),
    )
with k2:
    st.metric(
        "📅 Días de inventario",
        f"{rot14_act['dias_inventario']:.0f}"
        if rot14_act["rotacion_anual"] > 0 else "—",
        delta=_delta_pct(
            rot14_act["dias_inventario"], rot14_prev["dias_inventario"]
        ),
        delta_color="inverse",
    )
with k3:
    st.metric(
        "📒 Saldo cuenta 14",
        _money(rot14_act["saldo_inventario"]),
        delta=_delta_pct(
            rot14_act["saldo_inventario"], rot14_prev["saldo_inventario"]
        ),
    )
with k4:
    st.metric(
        "💰 Ventas período",
        _money(summary_act["total_ventas"]),
        delta=_delta_pct(
            summary_act["total_ventas"], summary_prev["total_ventas"]
        ),
    )

if st.button("🔄 Recargar datos (limpia caché)", key="ri_reload"):
    st.cache_data.clear()
    st.rerun()

st.markdown("---")


# ── Serie mensual: saldo cuenta 14 + ventas del mes ──
with st.spinner("Construyendo serie mensual..."):
    chart_e = enrich_chart_with_puc(chart_df)

    # Identificar IDs de cuentas 14 (Inventarios) UNA sola vez
    chart_e_copy = chart_e.copy()
    chart_e_copy["code_str"] = chart_e_copy.get("code", "").astype(str)
    inv_mask = chart_e_copy["code_str"].str.startswith("14")
    if not inv_mask.any() and "puc_subgroup" in chart_e_copy.columns:
        inv_mask = chart_e_copy["puc_subgroup"].astype(str) == "14"
    if not inv_mask.any() and "account_type" in chart_e_copy.columns:
        inv_mask = chart_e_copy["account_type"].astype(str).str.contains(
            "stock|inventory", case=False, na=False,
        )
    inv_account_ids: tuple[int, ...] = tuple(
        int(i) for i in chart_e_copy.loc[inv_mask, "id"].dropna().unique()
    ) if "id" in chart_e_copy.columns else ()

    # Saldos mensuales en 1 sola consulta (read_group + cumsum)
    saldo_mes_df = load_inventory_balance_monthly_series(
        date_from=fecha_desde.isoformat(),
        date_to=fecha_hasta.isoformat(),
        inventory_account_ids=inv_account_ids,
        company_ids=filters["company_ids"],
    )
    saldo_mes = saldo_mes_df.rename(columns={"saldo_cierre": "saldo_inv_cierre"})

    # Ventas del período mensualizadas (cálculo en memoria, ya tenemos las líneas)
    ventas_mes = pd.DataFrame()
    if sales_lines is not None and not sales_lines.empty:
        from src.purchases_analyzer import _apply_default_exclusions, _normalize_sales_signed
        sl = _apply_default_exclusions(_normalize_sales_signed(sales_lines))
        if not sl.empty and "invoice_date" in sl.columns:
            sl["_d"] = pd.to_datetime(sl["invoice_date"], errors="coerce").dt.date
            sl_per = sl[(sl["_d"] >= fecha_desde) & (sl["_d"] <= fecha_hasta)].copy()
            sl_per["mes"] = pd.to_datetime(sl_per["invoice_date"]).dt.to_period("M").dt.to_timestamp()
            ventas_mes = sl_per.groupby("mes", as_index=False)["price_subtotal_signed"].sum()
            ventas_mes = ventas_mes.rename(columns={"price_subtotal_signed": "ventas_mes"})

    # Combinar ventas + saldo (outer join para incluir meses con uno u otro)
    monthly = saldo_mes.merge(ventas_mes, on="mes", how="outer").fillna(
        {"ventas_mes": 0, "saldo_inv_cierre": 0}
    )
    # Rotación mensual anualizada: ventas_mes × 12 / saldo_cierre
    monthly["rotacion_anual_mes"] = monthly.apply(
        lambda r: (r["ventas_mes"] * 12 / r["saldo_inv_cierre"])
        if r["saldo_inv_cierre"] > 0 else None,
        axis=1,
    )
    monthly["dias_inv_mes"] = monthly["rotacion_anual_mes"].apply(
        lambda x: 365 / x if x and x > 0 else None
    )
    monthly = monthly.sort_values("mes").reset_index(drop=True)
    monthly["mes_label"] = pd.to_datetime(monthly["mes"]).dt.strftime("%Y-%m")


# ── Crosstabs por producto y categoría ──
crosstab = compute_product_crosstab(
    purchases_lines, sales_lines, stock_df, fecha_desde, fecha_hasta,
)
cat_tab = compute_category_crosstab(crosstab)


# ── Sub-pestañas ──
t_evol, t_cat, t_prod, t_rec, t_diag = st.tabs([
    "📈 Evolución mensual",
    "🏷️ Por categoría",
    "📦 Por producto",
    "💡 Recomendaciones",
    "🔍 Diagnóstico",
])


# ─── Tab Evolución ───
with t_evol:
    st.markdown("### Evolución mensual de la rotación")
    st.caption(
        "Cada barra representa un mes. La rotación se anualiza multiplicando "
        "las ventas del mes por 12 sobre el saldo de cuenta 14 al cierre del mes."
    )

    if monthly.empty:
        st.info("Sin datos para construir la serie mensual.")
    else:
        # Gráfico 1: ventas vs saldo de inventario
        fig1 = go.Figure()
        fig1.add_trace(go.Bar(
            x=monthly["mes_label"], y=monthly["ventas_mes"],
            name="Ventas del mes", marker_color="#10b981",
            yaxis="y",
        ))
        fig1.add_trace(go.Scatter(
            x=monthly["mes_label"], y=monthly["saldo_inv_cierre"],
            name="Saldo cuenta 14 (cierre)",
            line=dict(color="#a855f7", width=3),
            mode="lines+markers", yaxis="y2",
        ))
        fig1.update_layout(
            height=420, margin=dict(l=0, r=0, t=30, b=0),
            title="Ventas vs Saldo de inventario",
            yaxis=dict(title="Ventas $", tickformat=",.0f"),
            yaxis2=dict(
                title="Saldo inv. $", overlaying="y", side="right",
                tickformat=",.0f",
            ),
            legend=dict(orientation="h", yanchor="bottom", y=1.05, x=0),
        )
        st.plotly_chart(fig1, use_container_width=True)

        # Gráfico 2: rotación anualizada por mes
        rot_chart = monthly.dropna(subset=["rotacion_anual_mes"]).copy()
        if not rot_chart.empty:
            fig2 = go.Figure()
            fig2.add_trace(go.Bar(
                x=rot_chart["mes_label"], y=rot_chart["rotacion_anual_mes"],
                name="Rotación anualizada (mes)",
                marker_color="#0ea5e9",
                text=rot_chart["rotacion_anual_mes"],
                texttemplate="%{text:.1f}x",
                textposition="outside",
            ))
            fig2.add_trace(go.Scatter(
                x=rot_chart["mes_label"], y=rot_chart["dias_inv_mes"],
                name="Días de inventario",
                line=dict(color="#ef4444", width=2, dash="dot"),
                mode="lines+markers", yaxis="y2",
            ))
            fig2.update_layout(
                height=400, margin=dict(l=0, r=0, t=30, b=0),
                title="Rotación anualizada y días de inventario por mes",
                yaxis=dict(title="Rotación (veces/año)"),
                yaxis2=dict(
                    title="Días", overlaying="y", side="right",
                ),
                legend=dict(orientation="h", yanchor="bottom", y=1.05, x=0),
            )
            st.plotly_chart(fig2, use_container_width=True)

        # Tabla mensual
        with st.expander("📋 Tabla detallada por mes", expanded=False):
            tabla = monthly[[
                "mes_label", "ventas_mes", "saldo_inv_cierre",
                "rotacion_anual_mes", "dias_inv_mes",
            ]].rename(columns={"mes_label": "Mes"})
            st.dataframe(
                tabla,
                column_config={
                    "ventas_mes": st.column_config.NumberColumn(
                        "Ventas del mes", format="$%,.0f"
                    ),
                    "saldo_inv_cierre": st.column_config.NumberColumn(
                        "Saldo inv. cierre", format="$%,.0f"
                    ),
                    "rotacion_anual_mes": st.column_config.NumberColumn(
                        "Rotación anual", format="%.2fx"
                    ),
                    "dias_inv_mes": st.column_config.NumberColumn(
                        "Días inv.", format="%.0f"
                    ),
                },
                use_container_width=True, hide_index=True,
            )


# ─── Tab Por categoría ───
with t_cat:
    st.markdown("### Rotación por categoría")
    st.caption(
        "Rotación = costo de ventas / valor de stock por categoría. "
        "Calculada con el snapshot actual de stock.quant."
    )
    if cat_tab is None or cat_tab.empty:
        st.info("Sin datos por categoría.")
    else:
        # Solo categorías con stock valorado y rotación calculable
        cat_show = cat_tab[
            (cat_tab["stock_valor"] > 0)
            & (cat_tab["dias_inventario"].notna())
        ].copy()
        if cat_show.empty:
            st.info(
                "No hay categorías con stock valorado e información completa. "
                "Verifica que tus productos tengan costo y stock asignado."
            )
        else:
            # KPIs cabecera por categoría
            kc1, kc2, kc3 = st.columns(3)
            with kc1:
                top_r = cat_show.nsmallest(1, "dias_inventario").iloc[0]
                st.metric(
                    "🚀 Categoría más rápida",
                    str(top_r["product_categ_name"])[:30],
                    delta=f"{top_r['dias_inventario']:.0f} días",
                    delta_color="off",
                )
            with kc2:
                top_l = cat_show.nlargest(1, "dias_inventario").iloc[0]
                st.metric(
                    "🐢 Categoría más lenta",
                    str(top_l["product_categ_name"])[:30],
                    delta=f"{top_l['dias_inventario']:.0f} días",
                    delta_color="off",
                )
            with kc3:
                stock_total = float(cat_show["stock_valor"].sum())
                st.metric(
                    "💎 Stock valorado (todas)",
                    _money(stock_total),
                    delta=f"{len(cat_show)} categorías",
                    delta_color="off",
                )

            # Gráfico horizontal
            fig_cat = px.bar(
                cat_show.sort_values("rotacion_anual"),
                x="rotacion_anual", y="product_categ_name",
                orientation="h",
                color="rotacion_anual",
                color_continuous_scale="RdYlGn",
                text="rotacion_anual",
            )
            fig_cat.update_traces(
                texttemplate="%{text:.2f}x", textposition="outside",
            )
            fig_cat.update_layout(
                height=max(400, len(cat_show) * 30),
                margin=dict(l=0, r=0, t=10, b=0),
                yaxis=dict(title=""),
                xaxis=dict(title="Rotación anual (veces)"),
                coloraxis_showscale=False,
            )
            st.plotly_chart(fig_cat, use_container_width=True)

            # Tabla
            st.markdown("**Detalle por categoría**")
            st.dataframe(
                cat_show[[
                    "product_categ_name", "n_productos",
                    "stock_qty", "stock_valor",
                    "monto_ventas", "costo_ventas", "margen_pct",
                    "rotacion_anual", "dias_inventario",
                ]].sort_values("rotacion_anual", ascending=False),
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
                    "monto_ventas": st.column_config.NumberColumn(
                        "Ventas", format="$%,.0f"
                    ),
                    "costo_ventas": st.column_config.NumberColumn(
                        "Costo ventas", format="$%,.0f"
                    ),
                    "margen_pct": st.column_config.NumberColumn(
                        "% Margen", format="%.1f%%"
                    ),
                    "rotacion_anual": st.column_config.NumberColumn(
                        "Rot. anual", format="%.2fx"
                    ),
                    "dias_inventario": st.column_config.NumberColumn(
                        "Días inv.", format="%.0f"
                    ),
                },
                use_container_width=True, hide_index=True, height=480,
            )


# ─── Tab Por producto ───
with t_prod:
    st.markdown("### Rotación por producto")
    st.caption(
        "Análisis individual de productos con stock valorado. "
        "Identifica los más rápidos (riesgo de quiebre) y los más lentos "
        "(capital atrapado)."
    )
    if crosstab is None or crosstab.empty:
        st.info("Sin datos.")
    else:
        # Productos con datos válidos
        prod = crosstab[
            (crosstab["stock_qty"] > 0)
            & (crosstab["qty_vendida"] > 0)
        ].copy()
        if prod.empty:
            st.info(
                "No hay productos con stock y ventas simultáneamente. "
                "Revisa por separado en Compras vs Ventas."
            )
        else:
            cf1, cf2 = st.columns(2)
            with cf1:
                cats = ["(Todas)"] + sorted(
                    prod["product_categ_name"].fillna("(Sin categoría)").unique().tolist()
                )
                cat_filter = st.selectbox(
                    "Categoría", options=cats, key="ri_cat_prod",
                )
            with cf2:
                top_n = st.slider(
                    "Top N a mostrar",
                    min_value=10, max_value=100, value=20, step=5,
                    key="ri_top_n",
                )

            prod_show = prod.copy()
            prod_show["product_categ_name"] = prod_show[
                "product_categ_name"
            ].fillna("(Sin categoría)")
            if cat_filter and cat_filter != "(Todas)":
                prod_show = prod_show[
                    prod_show["product_categ_name"] == cat_filter
                ]

            # Top más rápidos
            col_r, col_l = st.columns(2)
            with col_r:
                st.markdown(f"**🚀 Top {top_n} más rápidos (más rotación)**")
                rapidos = prod_show.nlargest(top_n, "rotacion_anual")
                if not rapidos.empty:
                    st.dataframe(
                        rapidos[[
                            "product_default_code", "product_name",
                            "stock_qty", "qty_vendida",
                            "rotacion_anual", "dias_cobertura",
                        ]],
                        column_config={
                            "product_default_code": "Código",
                            "product_name": st.column_config.TextColumn(
                                "Producto", width="medium"
                            ),
                            "stock_qty": st.column_config.NumberColumn(
                                "Stock", format="%,.1f"
                            ),
                            "qty_vendida": st.column_config.NumberColumn(
                                "Vendida", format="%,.1f"
                            ),
                            "rotacion_anual": st.column_config.NumberColumn(
                                "Rot. anual", format="%.2fx"
                            ),
                            "dias_cobertura": st.column_config.NumberColumn(
                                "Días cob.", format="%.0f"
                            ),
                        },
                        use_container_width=True, hide_index=True,
                        height=500,
                    )

            with col_l:
                st.markdown(f"**🐢 Top {top_n} más lentos (menos rotación)**")
                lentos = prod_show[
                    prod_show["rotacion_anual"] > 0
                ].nsmallest(top_n, "rotacion_anual")
                if not lentos.empty:
                    st.dataframe(
                        lentos[[
                            "product_default_code", "product_name",
                            "stock_qty", "qty_vendida",
                            "rotacion_anual", "dias_cobertura",
                        ]],
                        column_config={
                            "product_default_code": "Código",
                            "product_name": st.column_config.TextColumn(
                                "Producto", width="medium"
                            ),
                            "stock_qty": st.column_config.NumberColumn(
                                "Stock", format="%,.1f"
                            ),
                            "qty_vendida": st.column_config.NumberColumn(
                                "Vendida", format="%,.1f"
                            ),
                            "rotacion_anual": st.column_config.NumberColumn(
                                "Rot. anual", format="%.2fx"
                            ),
                            "dias_cobertura": st.column_config.NumberColumn(
                                "Días cob.", format="%.0f"
                            ),
                        },
                        use_container_width=True, hide_index=True,
                        height=500,
                    )


# ─── Tab Recomendaciones ───
with t_rec:
    st.markdown("### 💡 Recomendaciones automáticas")
    st.caption(
        "Insights generados a partir de la evolución mensual, comparativos "
        "vs período anterior y análisis por categoría/producto."
    )

    recs = compute_inventory_recommendations(
        rot14_act, rot14_prev, cat_tab, crosstab,
        monthly, summary_act, summary_prev,
    )

    if not recs:
        st.success(
            "✅ Sin recomendaciones urgentes. El inventario está en buen estado."
        )
    else:
        # Resumen
        alta = sum(1 for r in recs if r.get("prioridad") == "alta")
        media = sum(1 for r in recs if r.get("prioridad") == "media")
        baja = sum(1 for r in recs if r.get("prioridad") == "baja")

        cr1, cr2, cr3 = st.columns(3)
        with cr1:
            st.metric("🔴 Alta prioridad", f"{alta}")
        with cr2:
            st.metric("🟡 Media prioridad", f"{media}")
        with cr3:
            st.metric("🟢 Informativa", f"{baja}")

        st.markdown("---")

        prio_colors = {
            "alta": "#fee2e2",
            "media": "#fef3c7",
            "baja": "#dcfce7",
        }
        prio_emoji = {
            "alta": "🔴",
            "media": "🟡",
            "baja": "🟢",
        }
        for r in recs:
            prio = r.get("prioridad", "baja")
            with st.container(border=True):
                col_h1, col_h2 = st.columns([4, 1])
                with col_h1:
                    st.markdown(
                        f"### {r.get('tipo', '')} — {r.get('titulo', '')}"
                    )
                with col_h2:
                    st.markdown(
                        f"**{prio_emoji.get(prio, '')} {prio.upper()}**"
                    )
                st.markdown(f"**Diagnóstico:** {r.get('detalle', '')}")
                st.markdown(f"**Acción sugerida:** {r.get('accion', '')}")


# ─── Tab Diagnóstico ───
with t_diag:
    st.markdown("### 🔍 Diagnóstico de cálculo")
    st.caption(
        "Información para auditar el cálculo de la rotación: cuentas 14 "
        "encontradas, supuestos y datos faltantes."
    )

    st.markdown("**Cuentas 14 (Inventarios) detectadas al corte**")
    cuentas_14 = rot14_act.get("cuentas_detalle", pd.DataFrame())
    if cuentas_14 is not None and not cuentas_14.empty:
        st.dataframe(
            cuentas_14,
            column_config={
                "account_code": "Código",
                "account_name": st.column_config.TextColumn(
                    "Cuenta", width="large"
                ),
                "saldo": st.column_config.NumberColumn(
                    "Saldo", format="$%,.0f"
                ),
            },
            use_container_width=True, hide_index=True,
        )
    else:
        st.warning(
            "No se detectaron cuentas con código que empiece con 14 ni con "
            "subgrupo PUC 14 ni con account_type tipo stock/inventory. "
            "Revisa tu plan de cuentas."
        )

    st.markdown("---")
    st.markdown("**Supuestos del cálculo**")
    st.markdown(
        """
        - **Rotación general:** `Ventas del período / Saldo cuenta 14 al corte`,
          anualizada multiplicando por `365 / días_del_período`.
        - **Rotación mensual:** `Ventas del mes × 12 / Saldo cuenta 14 al cierre del mes`.
        - **Por categoría/producto:** `Costo de ventas / valor de stock actual`
          (usa snapshot de `stock.quant`, no histórico mensual).
        - **Ventas:** suma de `price_subtotal_signed` de líneas con
          `move_type` ∈ {out_invoice, out_refund}, **excluyendo** SOAT y ANTCL
          (mismas exclusiones que el Informe de Ventas).
        - **Saldo cuenta 14:** suma de (debit − credit) de cuentas cuyo
          código empiece con "14", o cuyo subgrupo PUC sea 14, o cuyo
          account_type contenga "stock"/"inventory".
        """
    )

    st.markdown("---")
    st.markdown("**Comparativo período actual vs anterior**")
    comp_df = pd.DataFrame([
        {
            "Métrica": "Ventas",
            "Actual": summary_act["total_ventas"],
            "Anterior": summary_prev["total_ventas"],
        },
        {
            "Métrica": "Saldo cuenta 14",
            "Actual": rot14_act["saldo_inventario"],
            "Anterior": rot14_prev["saldo_inventario"],
        },
        {
            "Métrica": "Rotación anual",
            "Actual": rot14_act["rotacion_anual"],
            "Anterior": rot14_prev["rotacion_anual"],
        },
        {
            "Métrica": "Días inventario",
            "Actual": rot14_act["dias_inventario"],
            "Anterior": rot14_prev["dias_inventario"],
        },
    ])
    st.dataframe(
        comp_df,
        column_config={
            "Actual": st.column_config.NumberColumn("Actual", format="%,.2f"),
            "Anterior": st.column_config.NumberColumn("Anterior", format="%,.2f"),
        },
        use_container_width=True, hide_index=True,
    )
