# -*- coding: utf-8 -*-
"""
Página: KPIs comparativos por vendedor.

Muestra cómo se comporta la cartera de cada vendedor (responsable comercial
asignado al cliente vía `res.partner.user_id`) con métricas de cartera, hábito
de pago y ventas. Incluye observaciones automáticas con puntos a mejorar.
"""
from __future__ import annotations

import io

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from src.auth import logout_button, require_auth
from src.data_loader import compute_full_analysis
from src.ui_components import render_company_context, render_sidebar_filters
from src.vendedores import (
    SIN_ASIGNAR_LABEL,
    compute_kpis_por_vendedor,
    generate_observaciones,
)

st.set_page_config(
    page_title="KPI por vendedor | Cartera",
    page_icon="👤",
    layout="wide",
)

require_auth()
logout_button()

st.title("👤 KPIs por vendedor")
st.caption(
    "Comportamiento comparativo de cartera por responsable comercial "
    "(`res.partner.user_id`). Útil para identificar puntos a mejorar en la "
    "gestión y replicar lo que está funcionando."
)

filters = render_sidebar_filters()
if filters["company_ids"] is not None and len(filters["company_ids"]) == 0:
    st.warning("Selecciona al menos una empresa en el sidebar para ver datos.")
    st.stop()

# OJO: en esta página NO aplicamos `filter_analysis_by_vendedor` porque
# precisamente queremos comparar a todos los vendedores entre sí.
data = compute_full_analysis(
    months_back=filters["months_back"],
    rotation_period_days=filters["period_days"],
    company_ids=filters["company_ids"],
    exclude_cash_sales=filters["exclude_cash_sales"],
    analysis_window_days=filters.get("analysis_window_days"),
)

render_company_context(data.get("companies"), filters["company_ids"])

scored = data.get("scored")
if scored is None or scored.empty:
    st.info("No hay clientes con datos suficientes para analizar por vendedor.")
    st.stop()

# ---------------------------------------------------------------------------
# Cálculo del comparativo
# ---------------------------------------------------------------------------
kpis = compute_kpis_por_vendedor(
    scored=data["scored"],
    open_invoices=data["open_invoices"],
    raw_invoices=data["raw_invoices"],
    partners=data["raw_partners"],
    plan_cobro=data.get("plan_cobro"),
    cutoff_date=data["cutoff_date"],
    period_days=filters["period_days"],
    exclude_cash_sales=filters["exclude_cash_sales"],
)

if kpis.empty:
    st.info("No hay vendedores con cartera para mostrar.")
    st.stop()

# Filtro: ocultar "Sin asignar" si el usuario no quiere verlo
col_top1, col_top2, col_top3 = st.columns([1, 1, 2])
with col_top1:
    incluir_sin_asignar = st.checkbox(
        "Incluir clientes sin vendedor asignado",
        value=False,
        help=(
            "Hay clientes que en Odoo no tienen `user_id` asignado. Activa esta "
            "casilla para verlos como una fila aparte ('Sin asignar')."
        ),
    )
with col_top2:
    solo_con_saldo = st.checkbox(
        "Solo vendedores con cartera > 0",
        value=True,
        help="Esconde vendedores cuyos clientes no tienen saldo abierto hoy.",
    )

if not incluir_sin_asignar:
    kpis = kpis[kpis["user_name"] != SIN_ASIGNAR_LABEL].reset_index(drop=True)
if solo_con_saldo:
    kpis = kpis[kpis["saldo_total"] > 0].reset_index(drop=True)

if kpis.empty:
    st.info("No hay vendedores que cumplan los filtros.")
    st.stop()

# ---------------------------------------------------------------------------
# Resumen global
# ---------------------------------------------------------------------------
st.markdown("---")
g1, g2, g3, g4, g5 = st.columns(5)
g1.metric("Vendedores", len(kpis))
g2.metric("Cartera total", f"${kpis['saldo_total'].sum():,.0f}")
g3.metric(
    "Vencido total",
    f"${kpis['monto_vencido'].sum():,.0f}",
    delta=(
        f"{kpis['monto_vencido'].sum() / kpis['saldo_total'].sum() * 100:.1f}% del total"
        if kpis["saldo_total"].sum() > 0 else None
    ),
    delta_color="inverse",
)
g4.metric(
    "Clientes con saldo",
    int(kpis["num_clientes_con_saldo"].sum()),
)
g5.metric(
    "Facturas abiertas",
    int(kpis["num_facturas_abiertas"].sum()),
)

st.markdown("---")

# ---------------------------------------------------------------------------
# Gráficas comparativas
# ---------------------------------------------------------------------------
st.subheader("📊 Comparativo visual")
chart_col1, chart_col2 = st.columns(2)

with chart_col1:
    st.markdown("**Saldo de cartera (al día vs. vencido)**")
    chart_df = kpis.assign(
        al_dia=lambda d: (d["saldo_total"] - d["monto_vencido"]).clip(lower=0),
    ).sort_values("saldo_total", ascending=True)
    fig = go.Figure()
    fig.add_trace(go.Bar(
        y=chart_df["user_name"],
        x=chart_df["al_dia"],
        name="Al día",
        orientation="h",
        marker_color="#10b981",
        hovertemplate="<b>%{y}</b><br>Al día: $%{x:,.0f}<extra></extra>",
    ))
    fig.add_trace(go.Bar(
        y=chart_df["user_name"],
        x=chart_df["monto_vencido"],
        name="Vencido",
        orientation="h",
        marker_color="#ef4444",
        hovertemplate="<b>%{y}</b><br>Vencido: $%{x:,.0f}<extra></extra>",
    ))
    fig.update_layout(
        barmode="stack",
        height=max(300, 38 * len(chart_df) + 80),
        margin=dict(l=10, r=10, t=10, b=30),
        xaxis_title="Saldo ($)",
        yaxis_title=None,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    st.plotly_chart(fig, use_container_width=True)

with chart_col2:
    st.markdown("**% de cartera vencida por vendedor**")
    pct_df = kpis.sort_values("pct_vencido", ascending=True)
    colors = [
        "#ef4444" if p > 30 else ("#f97316" if p > 15 else "#10b981")
        for p in pct_df["pct_vencido"]
    ]
    fig = go.Figure(go.Bar(
        y=pct_df["user_name"],
        x=pct_df["pct_vencido"],
        orientation="h",
        marker_color=colors,
        text=[f"{p:.0f}%" for p in pct_df["pct_vencido"]],
        textposition="outside",
        hovertemplate="<b>%{y}</b><br>Vencido: %{x:.1f}%<extra></extra>",
    ))
    fig.update_layout(
        height=max(300, 38 * len(pct_df) + 80),
        margin=dict(l=10, r=40, t=10, b=30),
        xaxis_title="% vencido",
        yaxis_title=None,
    )
    st.plotly_chart(fig, use_container_width=True)

chart_col3, chart_col4 = st.columns(2)

with chart_col3:
    st.markdown("**Distribución de calificación A/B/C/D por vendedor**")
    # 100% stacked bar
    melt = kpis[["user_name", "num_a", "num_b", "num_c", "num_d", "num_sin_hist"]].copy()
    melt = melt.rename(columns={
        "num_a": "A", "num_b": "B", "num_c": "C", "num_d": "D",
        "num_sin_hist": "S/H",
    })
    melt = melt.melt(id_vars="user_name", var_name="Calificación", value_name="Clientes")
    color_map = {
        "A": "#10b981",
        "B": "#84cc16",
        "C": "#f59e0b",
        "D": "#ef4444",
        "S/H": "#94a3b8",
    }
    fig = px.bar(
        melt,
        x="Clientes",
        y="user_name",
        color="Calificación",
        orientation="h",
        color_discrete_map=color_map,
        category_orders={"Calificación": ["A", "B", "C", "D", "S/H"]},
    )
    fig.update_layout(
        barmode="stack",
        height=max(300, 38 * kpis.shape[0] + 80),
        margin=dict(l=10, r=10, t=10, b=30),
        yaxis_title=None,
        xaxis_title="# clientes",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    st.plotly_chart(fig, use_container_width=True)

with chart_col4:
    st.markdown("**DSO ponderado vs. mora promedio (puntos a mejorar arriba a la derecha)**")
    scatter_df = kpis.copy()
    scatter_df["bubble"] = scatter_df["saldo_total"].clip(lower=1)
    fig = px.scatter(
        scatter_df,
        x="dso_ponderado",
        y="mora_prom_ponderada",
        size="bubble",
        text="user_name",
        size_max=55,
        color="pct_vencido",
        color_continuous_scale=["#10b981", "#f59e0b", "#ef4444"],
        labels={
            "dso_ponderado": "DSO ponderado (días)",
            "mora_prom_ponderada": "Mora promedio (días)",
            "pct_vencido": "% vencido",
        },
    )
    fig.update_traces(textposition="top center")
    fig.add_hline(y=0, line_dash="dash", line_color="#94a3b8")
    fig.update_layout(
        height=max(300, 38 * kpis.shape[0] + 80),
        margin=dict(l=10, r=10, t=10, b=30),
    )
    st.plotly_chart(fig, use_container_width=True)
    st.caption(
        "Tamaño de la burbuja = saldo. Color = % vencido. "
        "Esquina superior derecha = vendedores que necesitan refuerzo en gestión."
    )

st.markdown("---")

# ---------------------------------------------------------------------------
# Tabla comparativa
# ---------------------------------------------------------------------------
st.subheader("📋 Tabla comparativa")

tabla = kpis[[
    "user_name",
    "num_clientes_con_saldo",
    "saldo_total",
    "monto_vencido",
    "pct_vencido",
    "dso_ponderado",
    "mora_prom_ponderada",
    "score_prom_ponderado",
    "pct_pagado_a_tiempo_ponderado",
    "num_a",
    "num_b",
    "num_c",
    "num_d",
    "num_sin_hist",
    "num_urgente",
    "num_alta",
    "num_facturas_abiertas",
    "num_facturas_vencidas",
    "num_facturas_vencidas_90",
    "ventas_credito_periodo",
    "num_facturas_periodo",
    "ticket_promedio",
    "plazo_otorgado_promedio",
]].rename(columns={
    "user_name": "Vendedor",
    "num_clientes_con_saldo": "# clientes",
    "saldo_total": "Saldo",
    "monto_vencido": "Vencido",
    "pct_vencido": "% vencido",
    "dso_ponderado": "DSO pond. (d)",
    "mora_prom_ponderada": "Mora prom. (d)",
    "score_prom_ponderado": "Score prom.",
    "pct_pagado_a_tiempo_ponderado": "% a tiempo",
    "num_a": "A", "num_b": "B", "num_c": "C", "num_d": "D",
    "num_sin_hist": "S/H",
    "num_urgente": "🔴 URG", "num_alta": "🟠 ALTA",
    "num_facturas_abiertas": "Fact. abiertas",
    "num_facturas_vencidas": "Fact. vencidas",
    "num_facturas_vencidas_90": ">90d",
    "ventas_credito_periodo": "Ventas crédito",
    "num_facturas_periodo": "# fact. ventana",
    "ticket_promedio": "Ticket prom.",
    "plazo_otorgado_promedio": "Plazo otorg. (d)",
})

st.dataframe(
    tabla,
    hide_index=True,
    use_container_width=True,
    height=min(580, 40 * len(tabla) + 50),
    column_config={
        "Saldo": st.column_config.NumberColumn(format="$%,.0f"),
        "Vencido": st.column_config.NumberColumn(format="$%,.0f"),
        "% vencido": st.column_config.NumberColumn(format="%.1f%%"),
        "% a tiempo": st.column_config.NumberColumn(format="%.0f%%"),
        "DSO pond. (d)": st.column_config.NumberColumn(format="%.0f"),
        "Mora prom. (d)": st.column_config.NumberColumn(format="%+.0f"),
        "Score prom.": st.column_config.ProgressColumn(
            format="%.1f", min_value=0, max_value=100,
        ),
        "Ventas crédito": st.column_config.NumberColumn(format="$%,.0f"),
        "Ticket prom.": st.column_config.NumberColumn(format="$%,.0f"),
        "Plazo otorg. (d)": st.column_config.NumberColumn(format="%.0f"),
    },
)

st.caption(
    "💡 **DSO ponderado** y **Mora prom.** se calculan ponderando por saldo de cada cliente — "
    "así pesan más los clientes con más cartera. **Score prom.** y **% a tiempo** también van ponderados."
)

st.markdown("---")

# ---------------------------------------------------------------------------
# Ranking + observaciones automáticas
# ---------------------------------------------------------------------------
st.subheader("🏆 Ranking y puntos a mejorar")

rcol1, rcol2 = st.columns(2)

with rcol1:
    st.markdown("**🥇 Mejores carteras (menor % vencido + mejor score)**")
    rank_best = kpis.copy()
    rank_best["composite"] = (
        100 - rank_best["pct_vencido"]
    ) * 0.5 + rank_best["score_prom_ponderado"] * 0.5
    rank_best = rank_best.sort_values("composite", ascending=False).head(5)
    for i, r in rank_best.iterrows():
        st.markdown(
            f"- **{r['user_name']}** · "
            f"{r['pct_vencido']:.0f}% vencido · "
            f"DSO {r['dso_ponderado']:.0f}d · "
            f"score {r['score_prom_ponderado']:.0f}"
        )

with rcol2:
    st.markdown("**⚠️ Carteras que necesitan atención**")
    rank_worst = kpis.copy()
    rank_worst["composite"] = (
        rank_worst["pct_vencido"] * 0.4
        + rank_worst["mora_prom_ponderada"].clip(lower=0) * 0.3
        + rank_worst["num_urgente"] * 5
    )
    rank_worst = rank_worst.sort_values("composite", ascending=False).head(5)
    for _, r in rank_worst.iterrows():
        st.markdown(
            f"- **{r['user_name']}** · "
            f"{r['pct_vencido']:.0f}% vencido · "
            f"mora {r['mora_prom_ponderada']:+.0f}d · "
            f"{int(r['num_urgente'])} urgentes"
        )

# Observaciones específicas por vendedor
obs = generate_observaciones(kpis)
if obs:
    st.markdown("---")
    st.markdown("**🔍 Observaciones específicas y acciones recomendadas**")
    # Agrupar por vendedor
    obs_df = pd.DataFrame(obs)
    for vend, grp in obs_df.groupby("vendedor"):
        with st.expander(f"👤 {vend}  ·  {len(grp)} observación(es)"):
            for _, item in grp.iterrows():
                if item["nivel"] == "critical":
                    st.error(item["mensaje"])
                elif item["nivel"] == "warning":
                    st.warning(item["mensaje"])
                else:
                    st.success(item["mensaje"])

# ---------------------------------------------------------------------------
# Drill-down: ver clientes problemáticos del vendedor seleccionado
# ---------------------------------------------------------------------------
st.markdown("---")
st.subheader("🔎 Drill-down por vendedor")

vend_pick = st.selectbox(
    "Selecciona un vendedor para ver sus clientes prioritarios",
    options=kpis["user_name"].tolist(),
    index=0,
)
vend_row = kpis[kpis["user_name"] == vend_pick].iloc[0]
uid_pick = int(vend_row["user_id"])

partners_df = data["raw_partners"]
partner_ids_vend = (
    partners_df.loc[partners_df["user_id"] == uid_pick, "id"].astype(int).tolist()
    if (uid_pick != 0 and partners_df is not None and not partners_df.empty)
    else []
)

if uid_pick == 0:
    # Sin asignar: clientes con user_id nulo o 0
    if partners_df is not None and not partners_df.empty:
        mask = partners_df["user_id"].fillna(0).astype(int) == 0
        partner_ids_vend = partners_df.loc[mask, "id"].astype(int).tolist()

plan = data.get("plan_cobro")
if plan is not None and not plan.empty and partner_ids_vend:
    plan_v = plan[plan["partner_id"].isin(partner_ids_vend)].head(15)
else:
    plan_v = pd.DataFrame()

if plan_v.empty:
    st.info(f"No hay clientes con saldo pendiente para {vend_pick}.")
else:
    st.markdown(f"**Top clientes a gestionar — {vend_pick}**")
    cols_show = [
        "prioridad", "partner_name", "saldo_actual", "monto_vencido",
        "dias_vencido_max", "calificacion", "score_total", "accion",
    ]
    cols_present = [c for c in cols_show if c in plan_v.columns]
    st.dataframe(
        plan_v[cols_present].rename(columns={
            "prioridad": "Prioridad",
            "partner_name": "Cliente",
            "saldo_actual": "Saldo",
            "monto_vencido": "Vencido",
            "dias_vencido_max": "Mora máx (d)",
            "calificacion": "Cal.",
            "score_total": "Score",
            "accion": "Acción sugerida",
        }),
        hide_index=True,
        use_container_width=True,
        column_config={
            "Saldo": st.column_config.NumberColumn(format="$%,.0f"),
            "Vencido": st.column_config.NumberColumn(format="$%,.0f"),
            "Score": st.column_config.NumberColumn(format="%.1f"),
        },
    )

# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
st.markdown("---")
buf = io.BytesIO()
with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
    tabla.to_excel(writer, sheet_name="KPI por vendedor", index=False)
    if obs:
        pd.DataFrame(obs).to_excel(writer, sheet_name="Observaciones", index=False)

st.download_button(
    "⬇️ Descargar reporte por vendedor (Excel)",
    data=buf.getvalue(),
    file_name=f"kpi_vendedores_{data['cutoff_date']}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)
