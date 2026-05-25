# -*- coding: utf-8 -*-
"""
Render compartido para las páginas de Auditoría de Órdenes.

`render_audit_page("venta")` y `render_audit_page("compra")` pintan la
página completa. Cada página en `pages/` es un envoltorio de una línea.
"""
from __future__ import annotations

import io
from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

from src.audit_analyzer import (
    ESTADO_ORDEN_LABELS,
    INVOICE_STATUS_LABELS,
    audit_by_month,
    audit_order_lines,
    compute_audit_kpis,
    explode_problem_types,
    summarize_audit_by_order,
)
from src.auth import logout_button, require_auth
from src.data_loader import (
    load_companies,
    load_purchase_order_audit,
    load_sale_order_audit,
)
from src.ui_components import render_company_context, render_sidebar_filters

_CFG = {
    "venta": {
        "page_title": "Auditoría Órdenes de Venta | Cartera",
        "titulo": "🔍 Auditoría de Órdenes de Venta",
        "socio_label": "Cliente",
        "entrega_label": "Entregada",
        "loader": "venta",
        "key_prefix": "ov",
    },
    "compra": {
        "page_title": "Auditoría Órdenes de Compra | Cartera",
        "titulo": "🔍 Auditoría de Órdenes de Compra",
        "socio_label": "Proveedor",
        "entrega_label": "Recibida",
        "loader": "compra",
        "key_prefix": "oc",
    },
}


def _fmt_fecha(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce").dt.strftime("%d/%m/%Y")


def _resumen_colcfg(socio_label: str, entrega_label: str) -> dict:
    return {
        "orden": st.column_config.TextColumn("Orden"),
        "fecha": st.column_config.TextColumn("Fecha"),
        "socio": st.column_config.TextColumn(socio_label, width="medium"),
        "empresa": st.column_config.TextColumn("Empresa"),
        "estado_orden": st.column_config.TextColumn("Estado"),
        "invoice_status": st.column_config.TextColumn("Facturación"),
        "n_lineas": st.column_config.NumberColumn("Líneas", format="%d"),
        "n_discrepancias": st.column_config.NumberColumn("Con dif.", format="%d"),
        "n_criticas": st.column_config.NumberColumn("Críticas", format="%d"),
        "cant_ordenada": st.column_config.NumberColumn("Ordenada", format="%.2f"),
        "cant_entregada": st.column_config.NumberColumn(entrega_label, format="%.2f"),
        "cant_facturada": st.column_config.NumberColumn("Facturada", format="%.2f"),
        "cant_por_facturar": st.column_config.NumberColumn("Por facturar", format="%.2f"),
        "dif_entrega": st.column_config.NumberColumn("Dif. orden↔entrega", format="%.2f"),
        "dif_factura": st.column_config.NumberColumn("Dif. orden↔factura", format="%.2f"),
        "dif_entrega_factura": st.column_config.NumberColumn("Dif. entrega↔factura", format="%.2f"),
        "severidad": st.column_config.TextColumn("Severidad"),
    }


def _lineas_colcfg(socio_label: str, entrega_label: str) -> dict:
    return {
        "orden": st.column_config.TextColumn("Orden"),
        "fecha": st.column_config.TextColumn("Fecha"),
        "socio": st.column_config.TextColumn(socio_label, width="medium"),
        "producto": st.column_config.TextColumn("Producto", width="medium"),
        "codigo": st.column_config.TextColumn("Código"),
        "descripcion": st.column_config.TextColumn("Descripción", width="large"),
        "cant_ordenada": st.column_config.NumberColumn("Ordenada", format="%.2f"),
        "cant_entregada": st.column_config.NumberColumn(entrega_label, format="%.2f"),
        "cant_facturada": st.column_config.NumberColumn("Facturada", format="%.2f"),
        "cant_por_facturar": st.column_config.NumberColumn("Por facturar", format="%.2f"),
        "dif_entrega": st.column_config.NumberColumn("Dif. orden↔entrega", format="%.2f"),
        "dif_factura": st.column_config.NumberColumn("Dif. orden↔factura", format="%.2f"),
        "dif_entrega_factura": st.column_config.NumberColumn("Dif. entrega↔factura", format="%.2f"),
        "tipo_discrepancia": st.column_config.TextColumn("Discrepancia", width="medium"),
        "severidad": st.column_config.TextColumn("Severidad"),
    }


_LINEA_COLS = [
    "orden", "fecha", "socio", "producto", "codigo", "descripcion",
    "cant_ordenada", "cant_entregada", "cant_facturada", "cant_por_facturar",
    "dif_entrega", "dif_factura", "dif_entrega_factura",
    "tipo_discrepancia", "severidad",
]


def _render_lineas(df: pd.DataFrame, socio_label: str,
                   entrega_label: str, height: int = 420) -> None:
    d = df.copy()
    d["fecha"] = _fmt_fecha(d["fecha"])
    st.dataframe(
        d[_LINEA_COLS], use_container_width=True, hide_index=True,
        height=height, column_config=_lineas_colcfg(socio_label, entrega_label),
    )


def _excel_auditoria(sheets: dict) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for name, df in sheets.items():
            (df if df is not None else pd.DataFrame()).to_excel(
                writer, sheet_name=name[:31], index=False,
            )
    return buffer.getvalue()


def _filtro_fecha(df_raw: pd.DataFrame, kp: str) -> pd.DataFrame:
    """Pinta el control de fecha (meses + rango) y devuelve el df filtrado."""
    st.markdown("**📅 Filtro de fecha**")
    modo = st.radio(
        "Filtrar por",
        ["Todo el histórico", "Por meses", "Por rango de fechas"],
        horizontal=True, key=f"{kp}_modo_fecha",
    )
    if df_raw is None or df_raw.empty or modo == "Todo el histórico":
        return df_raw

    fechas = pd.to_datetime(df_raw["fecha"], errors="coerce")

    if modo == "Por meses":
        periodos = sorted(
            {str(p) for p in fechas.dropna().dt.to_period("M")}, reverse=True,
        )
        sel = st.multiselect(
            "Meses", periodos, default=[], key=f"{kp}_meses",
            help="Deja vacío para incluir todos los meses.",
        )
        if sel:
            mask = fechas.dt.to_period("M").astype(str).isin(sel)
            return df_raw[mask].reset_index(drop=True)
        return df_raw

    # Por rango de fechas
    fmin, fmax = fechas.min(), fechas.max()
    if pd.isna(fmin) or pd.isna(fmax):
        return df_raw
    rango = st.date_input(
        "Rango de fechas",
        value=(fmin.date(), fmax.date()),
        min_value=fmin.date(), max_value=fmax.date(),
        key=f"{kp}_rango",
    )
    if isinstance(rango, (list, tuple)) and len(rango) == 2:
        d0 = pd.Timestamp(rango[0])
        d1 = pd.Timestamp(rango[1]) + pd.Timedelta(days=1)
        mask = (fechas >= d0) & (fechas < d1)
        return df_raw[mask].reset_index(drop=True)
    return df_raw


def render_audit_page(tipo: str) -> None:
    """Renderiza la página de auditoría completa para 'venta' o 'compra'."""
    cfg = _CFG[tipo]
    socio_label = cfg["socio_label"]
    entrega_label = cfg["entrega_label"]
    kp = cfg["key_prefix"]

    st.set_page_config(
        page_title=cfg["page_title"], page_icon="🔍", layout="wide",
    )
    require_auth()
    logout_button()

    st.title(cfg["titulo"])
    st.caption(
        f"Cruza cantidad ordenada vs {entrega_label.lower()} vs facturada en "
        f"las órdenes de {tipo} confirmadas. Detecta descuadres entre lo "
        "ordenado, lo movido físicamente y lo facturado que haya que corregir."
    )

    # ── Sidebar ──
    filters = render_sidebar_filters()
    if filters["company_ids"] is not None and len(filters["company_ids"]) == 0:
        st.warning("Selecciona al menos una empresa.")
        st.stop()

    if st.button("🔄 Recargar datos", key=f"{kp}_recargar"):
        st.cache_data.clear()
        st.rerun()

    companies_df = load_companies()
    render_company_context(companies_df, filters["company_ids"])
    company_ids = (
        tuple(filters["company_ids"]) if filters["company_ids"] else None
    )

    # ── Carga ──
    with st.spinner("Cargando órdenes confirmadas..."):
        if cfg["loader"] == "venta":
            df_raw = load_sale_order_audit(
                company_ids=company_ids, only_storable=True,
            )
        else:
            df_raw = load_purchase_order_audit(
                company_ids=company_ids, only_storable=True,
            )

    # ── Filtro de fecha (meses + rango) ──
    df_per = _filtro_fecha(df_raw, kp)
    df_aud = audit_order_lines(df_per)

    # ── Cómo leer ──
    with st.expander("ℹ️ Cómo leer esta auditoría", expanded=False):
        st.markdown(f"""
En una orden bien cerrada, **cantidad ordenada = {entrega_label.lower()} =
facturada**. Esta auditoría marca las líneas donde no es así:

- **Dif. orden↔entrega** — diferencia entre lo ordenado y lo
  {entrega_label.lower()}. Negativo = falta; positivo = de más.
- **Dif. orden↔factura** — diferencia entre lo ordenado y lo facturado.
  Negativo = falta facturar; positivo = se facturó de más.
- **Dif. entrega↔factura** — la más importante: si la mercancía
  {entrega_label.lower()} no coincide con la facturada hay un descuadre
  real. Esas líneas se marcan como **Crítica**.

Las demás diferencias (por ejemplo, solo falta facturar) se marcan como
**Revisar**.
""")

    if df_aud is None or df_aud.empty:
        st.info(
            "No hay órdenes confirmadas (de productos almacenables) en el "
            "período y empresa seleccionados."
        )
        return

    # ── KPIs ──
    kpis = compute_audit_kpis(df_aud)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Órdenes auditadas", f"{kpis['n_ordenes']:,}")
    c2.metric("Órdenes con discrepancia", f"{kpis['n_ordenes_discrepancia']:,}")
    c3.metric(
        "Líneas críticas", f"{kpis['n_criticas']:,}",
        help=f"{entrega_label} ≠ Facturada en la misma línea.",
    )
    c4.metric("Órdenes sin problema", f"{kpis['pct_ordenes_ok']:.1f}%")

    if kpis["n_ordenes_discrepancia"] == 0:
        st.success(
            "🎉 Todas las órdenes del período cuadran: lo ordenado, lo "
            f"{entrega_label.lower()} y lo facturado coinciden."
        )
        return

    # ===================================================================
    # GRÁFICAS
    # ===================================================================
    st.markdown("### 📈 Gráficas")
    g1, g2 = st.columns(2)

    # --- Evolución mensual ---
    with g1:
        mens = audit_by_month(df_aud)
        if not mens.empty:
            mlt = mens[
                ["mes_label", "lineas_discrepancia", "lineas_criticas"]
            ].melt(
                id_vars=["mes_label"],
                value_vars=["lineas_discrepancia", "lineas_criticas"],
                var_name="serie", value_name="cantidad",
            )
            mlt["serie"] = mlt["serie"].map({
                "lineas_discrepancia": "Con discrepancia",
                "lineas_criticas": "Críticas",
            })
            fig = px.bar(
                mlt, x="mes_label", y="cantidad", color="serie",
                barmode="group",
                color_discrete_map={
                    "Con discrepancia": "#F0A202", "Críticas": "#E94560",
                },
                labels={"mes_label": "Mes", "cantidad": "Líneas",
                        "serie": ""},
                title="Discrepancias por mes",
            )
            fig.update_layout(
                height=360, margin=dict(l=0, r=0, t=40, b=0),
                legend=dict(orientation="h", yanchor="bottom", y=1.02),
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.caption("Sin datos mensuales.")

    # --- Por tipo de problema ---
    with g2:
        expl_all = explode_problem_types(df_aud)
        if expl_all is not None and not expl_all.empty:
            tcount = (
                expl_all["problema"].value_counts()
                .rename_axis("Tipo").reset_index(name="Líneas")
            )
            fig = px.bar(
                tcount, x="Líneas", y="Tipo", orientation="h",
                color_discrete_sequence=["#E94560"],
                title="Líneas por tipo de problema",
            )
            fig.update_layout(
                height=360, margin=dict(l=0, r=0, t=40, b=0),
                yaxis=dict(categoryorder="total ascending"),
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.caption("Sin discrepancias que graficar.")

    # ===================================================================
    # FILTROS DE CATEGORIZACIÓN
    # ===================================================================
    st.markdown("### 🗂️ Detalle filtrado por tipo de problema")
    expl_all = explode_problem_types(df_aud)
    tipos_disp = (
        sorted(expl_all["problema"].dropna().unique())
        if expl_all is not None and not expl_all.empty else []
    )
    fc1, fc2 = st.columns([3, 1])
    with fc1:
        tipos_sel = st.multiselect(
            "Tipo de problema", tipos_disp, default=tipos_disp,
            key=f"{kp}_tipos",
        )
    with fc2:
        solo_crit = st.checkbox("Solo críticas", value=False,
                                key=f"{kp}_solocrit")
    if not tipos_sel:
        tipos_sel = tipos_disp

    # Filtrar líneas con discrepancia que tengan algún tipo seleccionado
    lineas_disc = df_aud[df_aud["tiene_discrepancia"]].copy()
    tset = set(tipos_sel)
    mask = lineas_disc["tipo_discrepancia"].apply(
        lambda td: bool(set(str(td).split(" · ")) & tset)
    )
    lineas_f = lineas_disc[mask].copy()
    if solo_crit:
        lineas_f = lineas_f[lineas_f["severidad"] == "Crítica"].copy()

    if lineas_f.empty:
        st.info("No hay líneas que cumplan los filtros seleccionados.")
        return

    # --- Resumen por orden (órdenes que tienen líneas filtradas) ---
    resumen = summarize_audit_by_order(df_aud)
    resumen_f = resumen[
        resumen["order_id"].isin(set(lineas_f["order_id"]))
    ].copy()
    st.markdown(f"#### 📋 Resumen por orden ({len(resumen_f):,})")
    r = resumen_f.copy()
    r["fecha"] = _fmt_fecha(r["fecha"])
    r["estado_orden"] = r["estado_orden"].map(
        lambda s: ESTADO_ORDEN_LABELS.get(s, s)
    )
    r["invoice_status"] = r["invoice_status"].map(
        lambda s: INVOICE_STATUS_LABELS.get(s, s)
    )
    r = r[[
        "orden", "fecha", "socio", "empresa", "estado_orden",
        "invoice_status", "n_lineas", "n_discrepancias", "n_criticas",
        "cant_ordenada", "cant_entregada", "cant_facturada",
        "cant_por_facturar", "dif_entrega", "dif_factura",
        "dif_entrega_factura", "severidad",
    ]]
    st.dataframe(
        r, use_container_width=True, hide_index=True, height=360,
        column_config=_resumen_colcfg(socio_label, entrega_label),
    )

    # --- Detalle por línea (todas las filtradas) ---
    st.markdown(f"#### 🔬 Detalle por línea ({len(lineas_f):,})")
    _render_lineas(lineas_f, socio_label, entrega_label, height=420)

    # --- Tablas separadas por tipo de problema ---
    st.markdown("#### 🗃️ Líneas agrupadas por tipo de problema")
    expl_f = explode_problem_types(lineas_f)
    expl_f = expl_f[expl_f["problema"].isin(tset)] if not expl_f.empty else expl_f
    if expl_f is None or expl_f.empty:
        st.caption("Sin líneas para agrupar.")
    else:
        orden_tipos = (
            expl_f["problema"].value_counts().index.tolist()
        )
        for prob in orden_tipos:
            sub = expl_f[expl_f["problema"] == prob]
            with st.expander(f"{prob} — {len(sub):,} líneas"):
                _render_lineas(sub, socio_label, entrega_label, height=320)

    # ===================================================================
    # EXPORTACIÓN A EXCEL
    # ===================================================================
    det_xlsx = lineas_f.copy()
    det_xlsx["fecha"] = _fmt_fecha(det_xlsx["fecha"])
    sheets = {
        "Resumen por orden": r,
        "Detalle por linea": det_xlsx[_LINEA_COLS],
        "Por mes": audit_by_month(df_aud),
        "Por tipo": (
            explode_problem_types(df_aud)["problema"].value_counts()
            .rename_axis("Tipo").reset_index(name="Lineas")
            if explode_problem_types(df_aud) is not None
            and not explode_problem_types(df_aud).empty
            else pd.DataFrame()
        ),
    }
    st.download_button(
        label="⬇️ Descargar auditoría (Excel)",
        data=_excel_auditoria(sheets),
        file_name=f"auditoria_ordenes_{tipo}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=f"{kp}_excel",
    )
