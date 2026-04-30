# -*- coding: utf-8 -*-
"""
Página: Informe de Ventas.

Reporte multidimensional de ventas a partir de account.move:
  - KPIs (ventas netas, # facturas, ticket promedio, var vs período anterior)
  - Tendencia mensual con comparativo
  - Por vendedor / cliente / producto / categoría

⚠️ FECHA UTILIZADA — invoice_date (fecha de FACTURACIÓN), NO date_order
   (fecha de la orden de venta). Toda venta se imputa al período en que
   se emitió la factura, no en que se generó la orden comercial. Esto
   es lo que pide la operación: ventas reconocidas en libros.
"""
from __future__ import annotations

import io
from datetime import date, timedelta

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from src.auth import logout_button, require_auth
from src.data_loader import compute_full_analysis, load_invoice_lines
from src.sales_analyzer import (
    EXCLUDED_SALES_DEFAULT_CODES,
    adjust_invoices_for_excluded_products,
    compute_sales_by_partner,
    compute_sales_by_product,
    compute_sales_by_vendedor,
    compute_sales_growth,
    compute_sales_kpis,
    compute_sales_monthly,
    filter_sales_invoices,
)
from src.ui_components import (
    render_company_context,
    render_sidebar_filters,
    render_vendedor_filter,
)

st.set_page_config(
    page_title="Informe de Ventas | Cartera",
    page_icon="💰",
    layout="wide",
)

require_auth()
logout_button()

st.title("💰 Informe de Ventas")
st.caption(
    "Ventas facturadas (account.move) por período, vendedor, cliente y producto. "
    "**Montos: subtotal SIN IVA** (igual que el reporte oficial de Odoo). "
    "**Fecha utilizada: fecha de FACTURA** (`invoice_date`), no fecha de orden. "
    "Las notas crédito (out_refund) restan automáticamente. Se mezclan ventas "
    "de contado y crédito. **Productos excluidos por referencia interna** "
    "(recaudos a terceros, no son ingresos operacionales reales): "
    + ", ".join(f"`{c}`" for c in EXCLUDED_SALES_DEFAULT_CODES)
    + "."
)

filters = render_sidebar_filters()
if filters["company_ids"] is not None and len(filters["company_ids"]) == 0:
    st.warning("Selecciona al menos una empresa en el sidebar para ver datos.")
    st.stop()

# Cargamos el análisis completo solo para reutilizar `raw_invoices`,
# `raw_partners` y `companies` ya cacheados (mismo TTL que el resto del app).
data = compute_full_analysis(
    months_back=filters["months_back"],
    rotation_period_days=filters["period_days"],
    company_ids=filters["company_ids"],
    exclude_cash_sales=filters["exclude_cash_sales"],
    analysis_window_days=filters.get("analysis_window_days"),
)

render_company_context(data.get("companies"), filters["company_ids"])

invoices_all = data.get("raw_invoices")
partners_all = data.get("raw_partners")

if invoices_all is None or invoices_all.empty:
    st.info("No hay facturas disponibles en el rango cargado.")
    st.stop()

# ---------------------------------------------------------------------------
# Filtro de vendedor (inline, arriba del contenido)
# ---------------------------------------------------------------------------
vendedor_user_ids = render_vendedor_filter(
    partners_all,
    key="vendedor_filter_ventas",
)
if vendedor_user_ids and partners_all is not None and not partners_all.empty:
    # Filtrar facturas por partners asignados a los vendedores seleccionados
    keep_partner_ids = set(
        partners_all.loc[
            partners_all["user_id"].isin(list(vendedor_user_ids)), "id"
        ].dropna().astype(int).tolist()
    )
    if keep_partner_ids:
        invoices_all = invoices_all[
            invoices_all["partner_id"].isin(keep_partner_ids)
        ].copy()
        if not invoice_lines_all.empty and "partner_id" in invoice_lines_all.columns:
            invoice_lines_all = invoice_lines_all[
                invoice_lines_all["partner_id"].isin(keep_partner_ids)
            ].copy()
        names = (
            partners_all.loc[
                partners_all["user_id"].isin(list(vendedor_user_ids)), "user_name"
            ].dropna().unique().tolist()
        )
        if names:
            st.info(f"👤 Filtrando por vendedor(es): **{', '.join(names)}**")
    else:
        st.warning("Los vendedores seleccionados no tienen clientes asignados.")
        st.stop()

# Cargar líneas de factura UNA sola vez para:
#   1) ajustar `amount_total_signed` de cada factura descontando SOAT/papeles
#      (afecta KPIs principales, mensual, vendedor y cliente).
#   2) calcular las tablas por producto y categoría.
# Si la carga falla (Odoo lento, etc.), seguimos sin filtro y mostramos
# advertencia — el informe sigue siendo usable, solo no excluye SOAT.
try:
    invoice_lines_all = load_invoice_lines(
        months_back=filters["months_back"],
        company_ids=filters["company_ids"],
    )
except Exception as exc:  # noqa: BLE001
    st.warning(
        f"No se pudieron cargar las líneas de factura: {exc}. "
        "El informe NO descontará SOAT/papeles esta vez."
    )
    invoice_lines_all = pd.DataFrame()

if not invoice_lines_all.empty:
    invoices_all = adjust_invoices_for_excluded_products(
        invoices_all, invoice_lines_all
    )

# Panel de diagnóstico — ayuda a verificar que los filtros se aplican.
with st.expander("🔍 Diagnóstico de datos (clic para abrir)", expanded=False):
    st.write(f"**Empresas seleccionadas en sidebar**: `{filters['company_ids']}`")
    if invoices_all is not None and not invoices_all.empty:
        st.write(f"**Facturas crudas cargadas**: {len(invoices_all):,}")
        if "company_id" in invoices_all.columns:
            por_empresa = (
                invoices_all.groupby("company_id")
                .agg(n_facturas=("id", "count"),
                     monto_total=("amount_total_signed",
                                  lambda s: float(pd.to_numeric(s, errors="coerce").abs().sum())))
            )
            st.write("**Por empresa (en facturas crudas, todo el histórico cargado):**")
            st.dataframe(por_empresa, use_container_width=True)
    if invoice_lines_all is not None and not invoice_lines_all.empty:
        st.write(f"**Líneas de factura cargadas**: {len(invoice_lines_all):,}")
        if "company_name" in invoice_lines_all.columns:
            por_emp_l = invoice_lines_all.groupby("company_name").size().rename("n_lineas")
            st.write("**Líneas por empresa:**")
            st.dataframe(por_emp_l, use_container_width=True)

# ---------------------------------------------------------------------------
# Filtros propios del informe — período de ventas
# ---------------------------------------------------------------------------
st.markdown("### 🗓️ Período del informe")
col_p1, col_p2, col_p3 = st.columns([1, 1, 2])

cutoff = data["cutoff_date"]
default_to = pd.Timestamp(cutoff).date() if cutoff else date.today()

# Por defecto: mes actual completo
default_from = default_to.replace(day=1)

with col_p1:
    fecha_desde = st.date_input(
        "Desde",
        value=default_from,
        max_value=default_to,
        help="Fecha inicial (basada en `invoice_date` de la factura).",
    )
with col_p2:
    fecha_hasta = st.date_input(
        "Hasta",
        value=default_to,
        max_value=default_to,
        help="Fecha final (basada en `invoice_date` de la factura).",
    )
with col_p3:
    quick = st.radio(
        "Atajos",
        options=["Personalizado", "Mes actual", "Mes anterior", "Trimestre actual",
                 "Año a la fecha", "Últimos 30 días", "Últimos 90 días"],
        index=1,
        horizontal=False,
    )

# Aplicar atajos (sobrescribe fechas si no es personalizado)
def _resolve_quick(today: date, choice: str) -> tuple[date, date]:
    if choice == "Mes actual":
        return today.replace(day=1), today
    if choice == "Mes anterior":
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        first_prev = last_prev.replace(day=1)
        return first_prev, last_prev
    if choice == "Trimestre actual":
        q_first_month = ((today.month - 1) // 3) * 3 + 1
        return today.replace(month=q_first_month, day=1), today
    if choice == "Año a la fecha":
        return today.replace(month=1, day=1), today
    if choice == "Últimos 30 días":
        return today - timedelta(days=30), today
    if choice == "Últimos 90 días":
        return today - timedelta(days=90), today
    return None, None

if quick != "Personalizado":
    f, t = _resolve_quick(default_to, quick)
    if f and t:
        fecha_desde, fecha_hasta = f, t

if fecha_desde > fecha_hasta:
    st.error("La fecha 'Desde' debe ser anterior o igual a 'Hasta'.")
    st.stop()

st.caption(
    f"Período activo: **{fecha_desde.strftime('%d %b %Y')}** → "
    f"**{fecha_hasta.strftime('%d %b %Y')}** "
    f"({(fecha_hasta - fecha_desde).days + 1} días)"
)

# ---------------------------------------------------------------------------
# KPIs principales + comparativo vs período anterior
# ---------------------------------------------------------------------------
st.markdown("### 📊 KPIs")

growth = compute_sales_growth(
    invoices=invoices_all,
    date_from=fecha_desde,
    date_to=fecha_hasta,
    company_ids=filters["company_ids"],
)
kpis = growth["actual"]
kpis_prev = growth["anterior"]


def _fmt_pct(v):
    if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))):
        return "—"
    return f"{v:+.1f}%"


def _fmt_money(v):
    return f"${v:,.0f}"


col1, col2, col3, col4 = st.columns(4)
col1.metric(
    "💰 Ventas netas",
    _fmt_money(kpis.ventas_netas),
    _fmt_pct(growth["var_ventas_pct"]),
    help=(
        f"Período actual − notas crédito.\n\n"
        f"Anterior ({growth['anterior_periodo'][0].date()} → "
        f"{growth['anterior_periodo'][1].date()}): {_fmt_money(kpis_prev.ventas_netas)}"
    ),
)
col2.metric(
    "🧾 # Facturas",
    f"{kpis.n_facturas:,}",
    _fmt_pct(growth["var_facturas_pct"]),
    help=f"Anterior: {kpis_prev.n_facturas:,} facturas",
)
col3.metric(
    "🎫 Ticket promedio",
    _fmt_money(kpis.ticket_promedio),
    _fmt_pct(growth["var_ticket_pct"]),
    help=f"Anterior: {_fmt_money(kpis_prev.ticket_promedio)}",
)
col4.metric(
    "👥 Clientes únicos",
    f"{kpis.n_clientes_unicos:,}",
    help=(
        f"Clientes que compraron al menos 1 vez en el período.\n\n"
        f"Anterior: {kpis_prev.n_clientes_unicos:,} clientes"
    ),
)

col5, col6, col7, col8 = st.columns(4)
col5.metric(
    "🟢 Ventas brutas",
    _fmt_money(kpis.ventas_brutas),
    help=f"{kpis.n_facturas:,} facturas (out_invoice).",
)
col6.metric(
    "🔴 Notas crédito ($)",
    _fmt_money(kpis.notas_credito),
    help=(
        f"Monto total de notas crédito (out_refund) emitidas en el período. "
        f"Se restan automáticamente de las ventas netas."
    ),
)
col7.metric(
    "📄 # Notas crédito",
    f"{kpis.n_notas_credito:,}",
    help=(
        "Cantidad de documentos NC en el período. "
        f"Anterior: {kpis_prev.n_notas_credito:,} NC."
    ),
)
nc_pct = (kpis.notas_credito / kpis.ventas_brutas * 100) if kpis.ventas_brutas else 0.0
col8.metric(
    "📉 NC / Ventas",
    f"{nc_pct:.1f}%",
    help="% de notas crédito sobre ventas brutas. Indicador de calidad.",
)

# ---------------------------------------------------------------------------
# Tendencia mensual con comparativo
# ---------------------------------------------------------------------------
st.markdown("### 📈 Tendencia mensual")

months_show = st.slider(
    "Meses a mostrar",
    min_value=3, max_value=24, value=12, step=1,
    help="Tendencia histórica de ventas mensuales (basada en invoice_date).",
)

monthly = compute_sales_monthly(
    invoices=invoices_all,
    months=months_show,
    cutoff_date=fecha_hasta,
    company_ids=filters["company_ids"],
)

if not monthly.empty:
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=monthly["mes_label"],
        y=monthly["ventas_brutas"],
        name="Ventas brutas",
        marker_color="#3b82f6",
        hovertemplate="<b>%{x}</b><br>Brutas: $%{y:,.0f}<extra></extra>",
    ))
    fig.add_trace(go.Bar(
        x=monthly["mes_label"],
        y=-monthly["notas_credito"],
        name="Notas crédito",
        marker_color="#ef4444",
        hovertemplate="<b>%{x}</b><br>NC: −$%{y:,.0f}<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=monthly["mes_label"],
        y=monthly["ventas_netas"],
        name="Ventas netas",
        mode="lines+markers",
        line=dict(color="#10b981", width=3),
        marker=dict(size=8),
        hovertemplate="<b>%{x}</b><br>Netas: $%{y:,.0f}<extra></extra>",
    ))
    fig.update_layout(
        barmode="relative",
        height=420,
        margin=dict(l=0, r=0, t=10, b=0),
        legend=dict(orientation="h", y=-0.15),
        yaxis_title="$ COP",
        hovermode="x unified",
    )
    st.plotly_chart(fig, use_container_width=True)

    # Tabla resumen con var_mom y var_yoy
    show = monthly.copy()
    show["var_mom_str"] = show["var_mom"].apply(_fmt_pct)
    show["var_yoy_str"] = show["var_yoy"].apply(_fmt_pct)
    st.dataframe(
        show[[
            "mes_label", "ventas_brutas", "notas_credito",
            "ventas_netas", "n_facturas", "ticket_promedio",
            "var_mom_str", "var_yoy_str",
        ]].rename(columns={
            "mes_label": "Mes",
            "ventas_brutas": "Brutas",
            "notas_credito": "NC",
            "ventas_netas": "Netas",
            "n_facturas": "# Fact.",
            "ticket_promedio": "Ticket prom.",
            "var_mom_str": "% vs mes ant.",
            "var_yoy_str": "% vs año ant.",
        }),
        column_config={
            "Brutas": st.column_config.NumberColumn(format="$ %,.0f"),
            "NC": st.column_config.NumberColumn(format="$ %,.0f"),
            "Netas": st.column_config.NumberColumn(format="$ %,.0f"),
            "Ticket prom.": st.column_config.NumberColumn(format="$ %,.0f"),
        },
        use_container_width=True, hide_index=True,
    )

# ---------------------------------------------------------------------------
# Tabs por dimensión
# ---------------------------------------------------------------------------
st.markdown("### 🔍 Desglose por dimensión")

tab_vend, tab_clientes, tab_prod, tab_cat = st.tabs([
    "👤 Vendedor", "🏢 Cliente (Pareto)", "📦 Producto", "🗂️ Categoría",
])

# --- Vendedor ---
with tab_vend:
    # Mapeamos id de vendedor → nombre desde res.users implícito vía partners
    # (no traemos res.users por separado, así que uso invoice_user_id de la
    # factura. Si está disponible un nombre desde la extracción, lo uso).
    vendedor_names: dict[int, str] = {}
    # Intento sacarlo de partners si ya viene resuelto allí
    if partners_all is not None and "user_id" in partners_all.columns:
        # Some pipelines añaden user_name al partner
        if "user_name" in partners_all.columns:
            tmp = (
                partners_all[["user_id", "user_name"]]
                .dropna()
                .drop_duplicates("user_id")
            )
            vendedor_names = dict(zip(tmp["user_id"].astype(int), tmp["user_name"]))

    by_vend = compute_sales_by_vendedor(
        invoices=invoices_all,
        date_from=fecha_desde,
        date_to=fecha_hasta,
        company_ids=filters["company_ids"],
        vendedor_names=vendedor_names or None,
    )

    if by_vend.empty:
        st.info("No hay ventas en el período.")
    else:
        # Gráfico de barras
        fig_v = px.bar(
            by_vend,
            x="vendedor_nombre", y="ventas_netas",
            color="ventas_netas",
            color_continuous_scale="Blues",
            text="participacion_pct",
            labels={"vendedor_nombre": "Vendedor", "ventas_netas": "Ventas netas $"},
        )
        fig_v.update_traces(texttemplate="%{text:.1f}%", textposition="outside")
        fig_v.update_layout(height=400, margin=dict(l=0, r=0, t=10, b=0),
                            coloraxis_showscale=False)
        st.plotly_chart(fig_v, use_container_width=True)

        st.dataframe(
            by_vend.rename(columns={
                "vendedor_id": "ID",
                "vendedor_nombre": "Vendedor",
                "ventas_netas": "Ventas netas",
                "ventas_brutas": "Ventas brutas",
                "notas_credito": "NC",
                "n_facturas": "# Fact.",
                "ticket_promedio": "Ticket prom.",
                "n_clientes": "# Clientes",
                "participacion_pct": "% del total",
            }),
            column_config={
                "Ventas netas": st.column_config.NumberColumn(format="$ %,.0f"),
                "Ventas brutas": st.column_config.NumberColumn(format="$ %,.0f"),
                "NC": st.column_config.NumberColumn(format="$ %,.0f"),
                "Ticket prom.": st.column_config.NumberColumn(format="$ %,.0f"),
                "% del total": st.column_config.NumberColumn(format="%.1f %%"),
            },
            use_container_width=True, hide_index=True,
        )

# --- Cliente (Pareto) ---
with tab_clientes:
    top_n = st.slider("Top N clientes", 5, 100, 20, key="top_clientes")
    by_part = compute_sales_by_partner(
        invoices=invoices_all,
        date_from=fecha_desde,
        date_to=fecha_hasta,
        company_ids=filters["company_ids"],
        top_n=top_n,
    )

    if by_part.empty:
        st.info("No hay ventas en el período.")
    else:
        # Gráfico Pareto: barras + línea acumulada
        fig_p = go.Figure()
        fig_p.add_trace(go.Bar(
            x=by_part["partner_nombre"],
            y=by_part["ventas_netas"],
            name="Ventas netas",
            marker_color="#3b82f6",
            yaxis="y1",
        ))
        fig_p.add_trace(go.Scatter(
            x=by_part["partner_nombre"],
            y=by_part["participacion_acum_pct"],
            name="% acumulado",
            mode="lines+markers",
            line=dict(color="#f59e0b", width=2),
            yaxis="y2",
        ))
        # Línea horizontal al 80%
        fig_p.add_hline(y=80, line_dash="dash", line_color="#ef4444",
                        opacity=0.5, yref="y2")
        fig_p.update_layout(
            height=450,
            margin=dict(l=0, r=0, t=10, b=80),
            xaxis_tickangle=-45,
            yaxis=dict(title="Ventas netas $", side="left"),
            yaxis2=dict(title="% acumulado", side="right",
                        overlaying="y", range=[0, 105]),
            legend=dict(orientation="h", y=1.05),
        )
        st.plotly_chart(fig_p, use_container_width=True)

        n_pareto = int(by_part["es_pareto_80"].sum())
        st.caption(
            f"📌 **{n_pareto} clientes** concentran el 80% de las ventas "
            f"(de un total de {len(by_part)} en el top mostrado)."
        )

        st.dataframe(
            by_part.rename(columns={
                "partner_id": "ID",
                "partner_nombre": "Cliente",
                "ventas_netas": "Ventas netas",
                "ventas_brutas": "Ventas brutas",
                "notas_credito": "NC",
                "n_facturas": "# Fact.",
                "ticket_promedio": "Ticket prom.",
                "participacion_pct": "% del total",
                "participacion_acum_pct": "% acum.",
                "es_pareto_80": "Pareto 80",
            }),
            column_config={
                "Ventas netas": st.column_config.NumberColumn(format="$ %,.0f"),
                "Ventas brutas": st.column_config.NumberColumn(format="$ %,.0f"),
                "NC": st.column_config.NumberColumn(format="$ %,.0f"),
                "Ticket prom.": st.column_config.NumberColumn(format="$ %,.0f"),
                "% del total": st.column_config.NumberColumn(format="%.1f %%"),
                "% acum.": st.column_config.NumberColumn(format="%.1f %%"),
            },
            use_container_width=True, hide_index=True,
        )

# --- Producto ---
with tab_prod:
    st.caption(
        "Requiere descarga adicional de líneas de factura (account.move.line). "
        "Puede tardar unos segundos la primera vez."
    )
    cargar = st.checkbox("Cargar análisis por producto", value=False,
                         key="cargar_prod")
    if cargar:
        # Reutilizamos las líneas ya cargadas arriba (evita un segundo
        # round-trip a Odoo). Si por alguna razón están vacías, intentamos
        # de nuevo.
        lines = invoice_lines_all
        if lines is None or lines.empty:
            try:
                lines = load_invoice_lines(
                    months_back=filters["months_back"],
                    company_ids=filters["company_ids"],
                )
            except Exception as exc:  # noqa: BLE001
                st.error(f"No se pudieron descargar líneas de factura: {exc}")
                lines = pd.DataFrame()

        if lines is None or lines.empty:
            st.info("No hay líneas de factura en el período.")
        else:
            top_n_p = st.slider("Top N productos", 5, 100, 20, key="top_prod")
            by_prod = compute_sales_by_product(
                invoice_lines=lines,
                date_from=fecha_desde,
                date_to=fecha_hasta,
                company_ids=filters["company_ids"],
                group_by="product",
                top_n=top_n_p,
            )
            if by_prod.empty:
                st.info("No hay ventas con productos en el período.")
            else:
                fig_pr = px.bar(
                    by_prod, x="product_nombre", y="ventas_netas",
                    color="ventas_netas", color_continuous_scale="Greens",
                    labels={"product_nombre": "Producto",
                            "ventas_netas": "Ventas netas $"},
                )
                fig_pr.update_layout(height=450, margin=dict(l=0, r=0, t=10, b=80),
                                     xaxis_tickangle=-45, coloraxis_showscale=False)
                st.plotly_chart(fig_pr, use_container_width=True)
                st.dataframe(
                    by_prod.rename(columns={
                        "product_id": "ID",
                        "product_nombre": "Producto",
                        "cantidad": "Cantidad",
                        "ventas_netas": "Ventas netas",
                        "n_facturas": "# Fact.",
                        "participacion_pct": "% del total",
                    }),
                    column_config={
                        "Ventas netas": st.column_config.NumberColumn(format="$ %,.0f"),
                        "% del total": st.column_config.NumberColumn(format="%.1f %%"),
                    },
                    use_container_width=True, hide_index=True,
                )

# --- Categoría ---
with tab_cat:
    cargar_cat = st.checkbox("Cargar análisis por categoría", value=False,
                             key="cargar_cat")
    if cargar_cat:
        # Reutilizamos las líneas ya cargadas arriba.
        lines = invoice_lines_all
        if lines is None or lines.empty:
            try:
                lines = load_invoice_lines(
                    months_back=filters["months_back"],
                    company_ids=filters["company_ids"],
                )
            except Exception as exc:  # noqa: BLE001
                st.error(f"No se pudieron descargar líneas de factura: {exc}")
                lines = pd.DataFrame()

        if lines is None or lines.empty:
            st.info("No hay líneas de factura en el período.")
        else:
            by_cat = compute_sales_by_product(
                invoice_lines=lines,
                date_from=fecha_desde,
                date_to=fecha_hasta,
                company_ids=filters["company_ids"],
                group_by="category",
            )
            if by_cat.empty:
                st.info("No hay ventas con categoría en el período.")
            else:
                fig_c = px.pie(
                    by_cat, values="ventas_netas", names="categoria_nombre",
                    hole=0.4,
                )
                fig_c.update_layout(height=400, margin=dict(l=0, r=0, t=10, b=0))
                st.plotly_chart(fig_c, use_container_width=True)
                st.dataframe(
                    by_cat.rename(columns={
                        "product_categ_id": "ID",
                        "categoria_nombre": "Categoría",
                        "cantidad": "Cantidad",
                        "ventas_netas": "Ventas netas",
                        "n_facturas": "# Fact.",
                        "participacion_pct": "% del total",
                    }),
                    column_config={
                        "Ventas netas": st.column_config.NumberColumn(format="$ %,.0f"),
                        "% del total": st.column_config.NumberColumn(format="%.1f %%"),
                    },
                    use_container_width=True, hide_index=True,
                )

# ---------------------------------------------------------------------------
# Exportación
# ---------------------------------------------------------------------------
st.markdown("### 📥 Exportar a Excel")

if st.button("Generar Excel del informe", type="primary"):
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        # KPIs
        pd.DataFrame([{
            "Período desde": fecha_desde,
            "Período hasta": fecha_hasta,
            "Ventas netas": kpis.ventas_netas,
            "Ventas brutas": kpis.ventas_brutas,
            "Notas crédito": kpis.notas_credito,
            "# Facturas": kpis.n_facturas,
            "# NC": kpis.n_notas_credito,
            "Ticket promedio": kpis.ticket_promedio,
            "Clientes únicos": kpis.n_clientes_unicos,
            "Var % vs anterior (ventas)": growth["var_ventas_pct"],
            "Var % vs anterior (facturas)": growth["var_facturas_pct"],
            "Var % vs anterior (ticket)": growth["var_ticket_pct"],
        }]).to_excel(writer, sheet_name="KPIs", index=False)

        if not monthly.empty:
            monthly.drop(columns=["mes"]).to_excel(
                writer, sheet_name="Tendencia mensual", index=False
            )
        if not by_vend.empty:
            by_vend.to_excel(writer, sheet_name="Por vendedor", index=False)

        # Recarga sin top_n para el export
        by_part_full = compute_sales_by_partner(
            invoices=invoices_all,
            date_from=fecha_desde,
            date_to=fecha_hasta,
            company_ids=filters["company_ids"],
        )
        if not by_part_full.empty:
            by_part_full.to_excel(writer, sheet_name="Por cliente", index=False)

        # Detalle facturas (audit trail)
        det = filter_sales_invoices(
            invoices_all, date_from=fecha_desde, date_to=fecha_hasta,
            company_ids=filters["company_ids"],
        )
        cols_export = [c for c in [
            "name", "invoice_date", "partner_name", "amount_total_signed",
            "move_type", "state", "payment_state", "invoice_user_id",
        ] if c in det.columns]
        if not det.empty and cols_export:
            det[cols_export].to_excel(writer, sheet_name="Detalle facturas", index=False)

    st.download_button(
        "⬇️ Descargar Excel",
        data=output.getvalue(),
        file_name=f"informe_ventas_{fecha_desde}_{fecha_hasta}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
