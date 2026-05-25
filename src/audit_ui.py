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
    audit_order_lines,
    compute_audit_kpis,
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
        "accion": "entregado",
        "loader": "venta",
        "key_prefix": "ov",
    },
    "compra": {
        "page_title": "Auditoría Órdenes de Compra | Cartera",
        "titulo": "🔍 Auditoría de Órdenes de Compra",
        "socio_label": "Proveedor",
        "entrega_label": "Recibida",
        "accion": "recibido",
        "loader": "compra",
        "key_prefix": "oc",
    },
}


def _fmt_fecha(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce").dt.strftime("%d/%m/%Y")


def _excel_auditoria(resumen: pd.DataFrame, detalle: pd.DataFrame) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        (resumen if resumen is not None else pd.DataFrame()).to_excel(
            writer, sheet_name="Resumen por orden", index=False,
        )
        (detalle if detalle is not None else pd.DataFrame()).to_excel(
            writer, sheet_name="Detalle por línea", index=False,
        )
    return buffer.getvalue()


def render_audit_page(tipo: str) -> None:
    """Renderiza la página de auditoría completa para 'venta' o 'compra'."""
    cfg = _CFG[tipo]
    socio_label = cfg["socio_label"]
    entrega_label = cfg["entrega_label"]
    key_prefix = cfg["key_prefix"]

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

    # ── Configuración ──
    cfg1, cfg2 = st.columns([2, 1])
    with cfg1:
        periodo = st.selectbox(
            "Período (por fecha de la orden)",
            options=["Todo el histórico", "Último año",
                     "Últimos 6 meses", "Últimos 3 meses"],
            index=0, key=f"{key_prefix}_periodo",
        )
    with cfg2:
        if st.button("🔄 Recargar datos", use_container_width=True,
                     key=f"{key_prefix}_recargar"):
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

    # ── Filtro de período ──
    if df_raw is not None and not df_raw.empty and periodo != "Todo el histórico":
        hoy = date.today()
        dias = {"Último año": 365, "Últimos 6 meses": 182,
                "Últimos 3 meses": 91}[periodo]
        corte = pd.Timestamp(hoy - timedelta(days=dias))
        f = pd.to_datetime(df_raw["fecha"], errors="coerce")
        df_raw = df_raw[f >= corte].reset_index(drop=True)

    df_aud = audit_order_lines(df_raw)

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

    # ── Sin datos ──
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

    # ── Distribución de tipos de discrepancia ──
    con_disc = df_aud[df_aud["tiene_discrepancia"]]
    tipos = (
        con_disc["tipo_discrepancia"]
        .str.split(" · ").explode().value_counts()
        .rename_axis("Tipo").reset_index(name="Líneas")
    )
    if not tipos.empty:
        fig = px.bar(
            tipos, x="Líneas", y="Tipo", orientation="h",
            color_discrete_sequence=["#E94560"],
            title="Tipos de discrepancia (líneas afectadas)",
        )
        fig.update_layout(
            height=max(220, 46 * len(tipos)),
            margin=dict(l=0, r=0, t=40, b=0),
            yaxis=dict(categoryorder="total ascending"),
        )
        st.plotly_chart(fig, use_container_width=True)

    # ── Filtro de severidad ──
    filtro = st.radio(
        "Mostrar",
        ["Solo con discrepancia", "Solo críticas", "Todas"],
        horizontal=True, key=f"{key_prefix}_filtro",
    )
    resumen = summarize_audit_by_order(df_aud)
    if filtro == "Solo con discrepancia":
        resumen_f = resumen[resumen["severidad"] != "OK"]
        lineas_f = df_aud[df_aud["tiene_discrepancia"]]
    elif filtro == "Solo críticas":
        resumen_f = resumen[resumen["severidad"] == "Crítica"]
        lineas_f = df_aud[df_aud["severidad"] == "Crítica"]
    else:
        resumen_f = resumen
        lineas_f = df_aud

    # ── Resumen por orden ──
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
        r, use_container_width=True, hide_index=True, height=380,
        column_config={
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
        },
    )

    # ── Detalle por línea ──
    st.markdown(f"#### 🔬 Detalle por línea ({len(lineas_f):,})")
    d = lineas_f.copy()
    d["fecha"] = _fmt_fecha(d["fecha"])
    d = d[[
        "orden", "fecha", "socio", "producto", "codigo", "descripcion",
        "cant_ordenada", "cant_entregada", "cant_facturada",
        "cant_por_facturar", "dif_entrega", "dif_factura",
        "dif_entrega_factura", "tipo_discrepancia", "severidad",
    ]]
    st.dataframe(
        d, use_container_width=True, hide_index=True, height=420,
        column_config={
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
        },
    )

    # ── Exportación a Excel ──
    st.download_button(
        label="⬇️ Descargar auditoría (Excel)",
        data=_excel_auditoria(resumen_f, d),
        file_name=f"auditoria_ordenes_{tipo}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=f"{key_prefix}_excel",
    )
