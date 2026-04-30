# -*- coding: utf-8 -*-
"""
Componentes reutilizables de Streamlit (KPIs, charts, filtros).
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st


# ---------------------------------------------------------------------------
# Filtros del sidebar
# ---------------------------------------------------------------------------


def render_sidebar_filters() -> dict:
    """
    Filtros globales del sidebar.

    Returns:
        dict con keys:
            - months_back: int
            - period_days: int
            - company_ids: tuple[int, ...] | None  (None = todas)
            - exclude_cash_sales: bool
            - analysis_window_days: int | None
    """
    # Cargar lista de empresas (cacheado en data_loader)
    from .config import get_data_floor_date
    from .data_loader import load_companies

    with st.sidebar:
        floor = get_data_floor_date()
        st.caption(
            f"📅 Datos confiables desde **{floor.strftime('%d %b %Y')}** "
            "(cargue del sistema). Todo lo anterior se ignora para que la "
            "rotación, DSO y hábito de pago no se contaminen con saldos iniciales."
        )
        st.markdown("### 🏢 Empresa")
        try:
            companies_df = load_companies()
        except Exception as exc:  # noqa: BLE001
            st.warning(f"No se pudieron cargar empresas: {exc}")
            companies_df = None

        company_ids: tuple[int, ...] | None = None
        if companies_df is not None and not companies_df.empty:
            options = companies_df["name"].tolist()
            id_by_name = dict(zip(companies_df["name"], companies_df["id"]))

            # Default: Casa de los Mineros (si existe). Las otras empresas
            # quedan disponibles para selección manual cuando el usuario
            # quiera consolidar varias.
            default_match = [
                n for n in options if "casa de los mineros" in n.lower()
            ]
            default_selected = default_match if default_match else [options[0]]

            selected = st.multiselect(
                "Filtrar por empresa(s)",
                options=options,
                default=default_selected,
                help=(
                    "Por defecto se muestra Casa de los Mineros. "
                    "Agrega otras empresas si quieres consolidar."
                ),
            )
            if selected:
                # Siempre pasar IDs explícitos cuando hay selección
                # (asegura que el dominio Odoo filtre correctamente).
                company_ids = tuple(int(id_by_name[n]) for n in selected)
            else:
                st.info("⚠️ Selecciona al menos una empresa.")
                company_ids = tuple()  # vacío → no datos

        st.markdown("---")
        st.markdown("### ⚙️ Parámetros")
        months_back = st.slider(
            "Meses de histórico a cargar",
            min_value=3,
            max_value=36,
            value=12,
            step=3,
            help="Cuánto histórico descargar de Odoo para calcular comportamiento de pago.",
        )
        period_label = st.selectbox(
            "Período para rotación",
            options=["Último año (365 días)", "Últimos 6 meses", "Últimos 90 días", "Últimos 30 días"],
            index=0,
        )
        period_map = {
            "Último año (365 días)": 365,
            "Últimos 6 meses": 180,
            "Últimos 90 días": 90,
            "Últimos 30 días": 30,
        }

        st.markdown("---")
        st.markdown("### 🔍 Cálculo de rotación")
        exclude_cash_sales = st.checkbox(
            "Excluir ventas de contado",
            value=True,
            help=(
                "Excluye facturas donde fecha de factura == fecha de vencimiento "
                "(pago inmediato/contado). Estas distorsionan la rotación porque "
                "nunca fueron cuentas por cobrar reales."
            ),
        )

        st.markdown("---")
        st.markdown("### 🎯 Lapso del estudio")
        analysis_label = st.selectbox(
            "Ventana de análisis del hábito de pago",
            options=[
                "Todo el histórico cargado",
                "Últimos 12 meses",
                "Últimos 6 meses",
                "Últimos 3 meses",
            ],
            index=0,
            help=(
                "Restringe el cálculo de hábito de pago, DSO por cliente y "
                "scoring a un período más reciente. Útil para evaluar el "
                "comportamiento actual de un cliente sin que pesen facturas "
                "de hace varios años."
            ),
        )
        analysis_map = {
            "Todo el histórico cargado": None,
            "Últimos 12 meses": 365,
            "Últimos 6 meses": 180,
            "Últimos 3 meses": 90,
        }

        st.markdown("---")
        # Botón para forzar recarga desde Odoo. Limpia el caché de
        # `@st.cache_data` (que dura 15 min por defecto). Útil si acabas
        # de registrar facturas/pagos y quieres ver los datos al instante.
        if st.button(
            "🔄 Recargar datos de Odoo",
            use_container_width=True,
            help=(
                "Borra el caché de la app y vuelve a descargar todo desde Odoo. "
                "Tarda lo mismo que la primera carga (~30-60s). Úsalo después "
                "de registrar facturas/pagos nuevos en Odoo."
            ),
        ):
            st.cache_data.clear()
            st.rerun()

        st.markdown("---")

    return {
        "months_back": months_back,
        "period_days": period_map[period_label],
        "company_ids": company_ids,
        "exclude_cash_sales": exclude_cash_sales,
        "analysis_window_days": analysis_map[analysis_label],
    }


# ---------------------------------------------------------------------------
# Filtro de vendedor (se renderiza después de cargar datos para tener la lista
# real de vendedores asignados a clientes)
# ---------------------------------------------------------------------------


def render_vendedor_filter(
    partners: "pd.DataFrame | None",
    key: str = "vendedor_filter",
    label: str = "👤 Filtrar por vendedor(es)",
) -> tuple[int, ...] | None:
    """
    Multiselect de vendedor renderizado INLINE (en el cuerpo de la página,
    no en el sidebar). Se ubica donde sea llamado, idealmente justo arriba
    del contenido principal para que el usuario lo vea sin abrir el sidebar.

    Se basa en `res.partner.user_id` (vendedor asignado al cliente).
    Devuelve una tupla de user_ids seleccionados, o None si no hay selección.

    La selección se persiste en `st.session_state[key]` así viaja entre
    páginas — la clave por defecto (`vendedor_filter`) es la misma que
    usaba la versión vieja del sidebar para compatibilidad.
    """
    if partners is None or partners.empty or "user_id" not in partners.columns:
        st.caption("Sin datos de vendedor disponibles.")
        return None

    df = partners[["user_id", "user_name"]].dropna(subset=["user_id"]).copy()
    if df.empty:
        st.caption("Ningún cliente tiene vendedor asignado.")
        return None
    df["user_id"] = df["user_id"].astype(int)
    df = df.drop_duplicates(subset="user_id").sort_values("user_name")

    options = df["user_name"].tolist()
    id_by_name = dict(zip(df["user_name"], df["user_id"]))

    selected = st.multiselect(
        label,
        options=options,
        default=st.session_state.get(key, []),
        help=(
            "Limita los clientes a los asignados a estos vendedores "
            "(`res.partner.user_id`). Vacío = todos los vendedores."
        ),
        key=key,
        placeholder="Todos los vendedores",
    )

    if not selected:
        return None
    return tuple(int(id_by_name[n]) for n in selected if n in id_by_name)


# Alias retrocompatible — apunta al filtro inline. Si en algún momento
# quieres volver al sidebar, basta con redefinirlo aquí. Las páginas
# siguen llamando `render_sidebar_vendedor_filter` y funcionan igual,
# pero el filtro ahora aparece en el cuerpo donde se llame.
render_sidebar_vendedor_filter = render_vendedor_filter


# ---------------------------------------------------------------------------
# Header de contexto (qué empresa(s) se están viendo)
# ---------------------------------------------------------------------------


def render_company_context(
    companies_df: "pd.DataFrame | None",
    selected_ids: tuple[int, ...] | None,
) -> None:
    """
    Banner superior que indica qué empresa(s) están siendo analizadas.

    Si selected_ids es None → muestra "Todas las empresas".
    Si selected_ids tiene IDs → muestra los nombres separados por coma.
    """
    if companies_df is None or companies_df.empty:
        return

    if selected_ids is None:
        names = companies_df["name"].tolist()
        label = f"📂 **Empresas activas (todas):** {', '.join(names)}"
    else:
        if not selected_ids:
            return
        mask = companies_df["id"].isin(list(selected_ids))
        names = companies_df.loc[mask, "name"].tolist()
        if not names:
            return
        if len(names) == 1:
            label = f"🏢 **Empresa activa:** {names[0]}"
        else:
            label = f"🏢 **Empresas activas ({len(names)}):** {', '.join(names)}"

    st.info(label)


# ---------------------------------------------------------------------------
# KPIs
# ---------------------------------------------------------------------------


def _fmt_money(v: float) -> str:
    if v is None or pd.isna(v):
        return "—"
    return f"${v:,.0f}"


def render_kpis(kpis: dict, cutoff_date: date | None = None) -> None:
    """Tarjetas de KPIs principales."""
    st.markdown(f"##### Corte: {cutoff_date.strftime('%d/%m/%Y') if cutoff_date else 'hoy'}")

    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        st.metric(
            "Saldo total cartera",
            _fmt_money(kpis["saldo_cartera"]),
            help="Saldo total pendiente de cobro a la fecha de corte.",
        )
    with c2:
        rot_dias = kpis["rotacion_dias"]
        st.metric(
            "Días de cartera (acumulado)",
            f"{rot_dias:.0f}" if rot_dias else "N/A",
            help=(
                "Saldo total / ventas del período × días del período. "
                "Incluye TODA la cartera abierta, incluyendo facturas viejas. "
                "Es el indicador para vigilar cartera vieja sin cobrar."
            ),
        )
    with c3:
        dso_90 = kpis.get("dso_ultimos_90", 0.0)
        st.metric(
            "DSO últimos 90 días",
            f"{dso_90:.1f} d" if dso_90 else "N/A",
            help=(
                "DSO calculado sobre saldo de fin de mes vs. ventas de los últimos 90 días, "
                "tomado del último mes del histórico. Refleja la salud reciente de cobranza, "
                "pero NO refleja cartera vieja que arrastras."
            ),
        )
    with c4:
        st.metric(
            "Rotación (veces/año)",
            f"{kpis['rotacion_veces']:.2f}" if kpis["rotacion_veces"] else "N/A",
            help="Cuántas veces gira la cartera al año (basado en días de cartera acumulado).",
        )
    with c5:
        pct = kpis["pct_vencido"]
        st.metric(
            "% Vencido",
            f"{pct:.1f}%" if pct else "0.0%",
            delta=None,
            delta_color="inverse",
            help="% del saldo que está vencido.",
        )

    c5, c6, c7, c8 = st.columns(4)
    with c5:
        st.metric("Ventas a crédito (período)", _fmt_money(kpis["ventas_credito"]))
    with c6:
        st.metric("Saldo promedio (período)", _fmt_money(kpis["saldo_promedio"]))
    with c7:
        st.metric(
            "Facturas abiertas / vencidas",
            f"{kpis['facturas_abiertas']} / {kpis['facturas_vencidas']}",
        )
    with c8:
        st.metric("Clientes con saldo", kpis["clientes_con_saldo"])

    # Detalle del cálculo de rotación (transparencia)
    excluidas = kpis.get("facturas_contado_excluidas", 0)
    a_credito = kpis.get("facturas_credito_periodo", 0)
    if kpis.get("exclude_cash_sales", True):
        st.caption(
            f"📌 Rotación calculada sobre **{a_credito}** facturas a crédito · "
            f"**{excluidas}** facturas de contado (mismo día) excluidas."
        )
    else:
        st.caption(
            "📌 Rotación incluye TODAS las facturas (contado + crédito)."
        )


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------


def render_aging_chart(aging: pd.DataFrame) -> None:
    """Gráfico de barras de aging por rango."""
    if aging.empty:
        st.info("No hay datos de aging para mostrar.")
        return

    df = aging.copy()
    colors = {
        "Corriente (no vencido)": "#10b981",  # verde
        "1 - 30 días": "#84cc16",
        "31 - 60 días": "#facc15",
        "61 - 90 días": "#f97316",
        "91 - 180 días": "#ef4444",
        "Más de 180 días": "#7f1d1d",
    }

    fig = px.bar(
        df,
        x="rango",
        y="monto",
        color="rango",
        color_discrete_map=colors,
        text_auto=".2s",
        labels={"rango": "Rango de antigüedad", "monto": "Monto pendiente"},
    )
    fig.update_layout(
        showlegend=False,
        height=380,
        margin=dict(l=10, r=10, t=10, b=10),
        xaxis_title=None,
    )
    fig.update_traces(textposition="outside")
    st.plotly_chart(fig, use_container_width=True)


def render_score_distribution(scored: pd.DataFrame) -> None:
    """Histograma + bar chart de distribución de calificaciones."""
    if scored.empty:
        st.info("No hay clientes para calificar.")
        return

    cuentas = (
        scored["calificacion"]
        .value_counts()
        .reindex(["A", "B", "C", "D", "SIN_HISTORICO"])
        .fillna(0)
        .reset_index()
    )
    cuentas.columns = ["calificacion", "num_clientes"]

    colors = {
        "A": "#10b981",
        "B": "#3b82f6",
        "C": "#f59e0b",
        "D": "#ef4444",
        "SIN_HISTORICO": "#9ca3af",
    }
    fig = px.bar(
        cuentas,
        x="calificacion",
        y="num_clientes",
        color="calificacion",
        color_discrete_map=colors,
        text_auto=True,
    )
    fig.update_layout(
        showlegend=False, height=320, margin=dict(l=10, r=10, t=10, b=10)
    )
    st.plotly_chart(fig, use_container_width=True)


def render_trend_invoices(invoices: pd.DataFrame) -> None:
    """Tendencia mensual de facturación vs. cobros (placeholder)."""
    if invoices.empty:
        st.info("No hay facturas para mostrar tendencia.")
        return

    df = invoices.copy()
    df = df[df["move_type"] == "out_invoice"]
    df["mes"] = pd.to_datetime(df["invoice_date"]).dt.to_period("M").astype(str)
    monthly = df.groupby("mes")["amount_total_signed"].apply(lambda s: s.abs().sum()).reset_index()
    monthly.columns = ["mes", "facturado"]

    fig = px.line(monthly, x="mes", y="facturado", markers=True)
    fig.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10))
    st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# Gráficas de histórico (dashboard)
# ---------------------------------------------------------------------------


def render_history_facturado_cobrado(history: pd.DataFrame) -> None:
    """Barras agrupadas: facturado a crédito vs. cobrado por mes."""
    if history is None or history.empty:
        st.info("Sin datos de histórico para mostrar.")
        return

    df = history.copy()
    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=df["mes_label"],
            y=df["facturado_credito"],
            name="Facturado a crédito",
            marker_color="#3b82f6",
            hovertemplate="%{x}<br>Facturado: $%{y:,.0f}<extra></extra>",
        )
    )
    fig.add_trace(
        go.Bar(
            x=df["mes_label"],
            y=df["cobrado"],
            name="Cobrado",
            marker_color="#10b981",
            hovertemplate="%{x}<br>Cobrado: $%{y:,.0f}<extra></extra>",
        )
    )
    fig.update_layout(
        barmode="group",
        height=340,
        margin=dict(l=10, r=10, t=30, b=10),
        legend=dict(orientation="h", y=1.12, x=0),
        xaxis_title=None,
        yaxis_title="$ COP",
        title=None,
    )
    st.plotly_chart(fig, use_container_width=True)


def render_history_dso(history: pd.DataFrame) -> None:
    """Línea: DSO rolling 90 días por mes."""
    if history is None or history.empty:
        st.info("Sin datos de histórico para mostrar.")
        return

    df = history.copy()
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=df["mes_label"],
            y=df["dso_rolling"],
            mode="lines+markers",
            line=dict(color="#f59e0b", width=3),
            marker=dict(size=8),
            name="DSO (90d)",
            hovertemplate="%{x}<br>DSO: %{y:.1f} días<extra></extra>",
        )
    )
    fig.update_layout(
        height=320,
        margin=dict(l=10, r=10, t=30, b=10),
        xaxis_title=None,
        yaxis_title="Días",
        showlegend=False,
    )
    st.plotly_chart(fig, use_container_width=True)


def render_history_saldo(history: pd.DataFrame) -> None:
    """Área: saldo de cartera estimado al cierre de cada mes."""
    if history is None or history.empty:
        st.info("Sin datos de histórico para mostrar.")
        return

    df = history.copy()
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=df["mes_label"],
            y=df["saldo_acumulado"],
            mode="lines",
            line=dict(color="#8b5cf6", width=2),
            fill="tozeroy",
            fillcolor="rgba(139,92,246,0.18)",
            name="Saldo cartera",
            hovertemplate="%{x}<br>Saldo: $%{y:,.0f}<extra></extra>",
        )
    )
    fig.update_layout(
        height=300,
        margin=dict(l=10, r=10, t=30, b=10),
        xaxis_title=None,
        yaxis_title="$ COP",
        showlegend=False,
    )
    st.plotly_chart(fig, use_container_width=True)
