# -*- coding: utf-8 -*-
"""
Render compartido para las páginas de Auditoría de Órdenes.

`render_audit_page("venta")` y `render_audit_page("compra")` pintan la
página completa. Cada página en `pages/` es un envoltorio de una línea.

El informe se enfoca en dos saldos calculados (Carlos) que deben ser cero:
  - Cant. a facturar (Carlos)  = ordenada − facturada
  - Cant. por recibir (Carlos) = facturada − recibida/entregada
"""
from __future__ import annotations

import io
import re

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
        "porrec_label": "Cant. por entregar (Carlos)",
        "loader": "venta",
        "key_prefix": "ov",
    },
    "compra": {
        "page_title": "Auditoría Órdenes de Compra | Cartera",
        "titulo": "🔍 Auditoría de Órdenes de Compra",
        "socio_label": "Proveedor",
        "entrega_label": "Recibida",
        "porrec_label": "Cant. por recibir (Carlos)",
        "loader": "compra",
        "key_prefix": "oc",
    },
}

_AF_LABEL = "Cant. a facturar (Carlos)"


def _fmt_fecha(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce").dt.strftime("%d/%m/%Y")


def _resumen_colcfg(socio_label, entrega_label, porrec_label) -> dict:
    return {
        "orden": st.column_config.TextColumn("Orden"),
        "fecha": st.column_config.TextColumn("Fecha"),
        "socio": st.column_config.TextColumn(socio_label, width="medium"),
        "empresa": st.column_config.TextColumn("Empresa"),
        "estado_orden": st.column_config.TextColumn("Estado"),
        "invoice_status": st.column_config.TextColumn("Facturación"),
        "n_lineas": st.column_config.NumberColumn("Líneas", format="%d"),
        "n_discrepancias": st.column_config.NumberColumn("Con dif.", format="%d"),
        "cant_ordenada": st.column_config.NumberColumn("Ordenada", format="%.2f"),
        "cant_entregada": st.column_config.NumberColumn(entrega_label, format="%.2f"),
        "cant_facturada": st.column_config.NumberColumn("Facturada", format="%.2f"),
        "cant_a_facturar": st.column_config.NumberColumn(_AF_LABEL, format="%.2f"),
        "cant_por_recibir": st.column_config.NumberColumn(porrec_label, format="%.2f"),
    }


def _lineas_colcfg(socio_label, entrega_label, porrec_label) -> dict:
    return {
        "orden": st.column_config.TextColumn("Orden"),
        "fecha": st.column_config.TextColumn("Fecha"),
        "socio": st.column_config.TextColumn(socio_label, width="medium"),
        "producto": st.column_config.TextColumn("Producto", width="large"),
        "codigo": st.column_config.TextColumn("Código"),
        "cant_ordenada": st.column_config.NumberColumn("Ordenada", format="%.2f"),
        "cant_entregada": st.column_config.NumberColumn(entrega_label, format="%.2f"),
        "cant_facturada": st.column_config.NumberColumn("Facturada", format="%.2f"),
        "cant_a_facturar": st.column_config.NumberColumn(_AF_LABEL, format="%.2f"),
        "cant_por_recibir": st.column_config.NumberColumn(porrec_label, format="%.2f"),
        "tipo_discrepancia": st.column_config.TextColumn("Problema", width="medium"),
    }


_LINEA_COLS = [
    "orden", "fecha", "socio", "producto", "codigo",
    "cant_ordenada", "cant_entregada", "cant_facturada",
    "cant_a_facturar", "cant_por_recibir", "tipo_discrepancia",
]
_RESUMEN_COLS = [
    "orden", "fecha", "socio", "empresa", "estado_orden", "invoice_status",
    "n_lineas", "n_discrepancias", "cant_ordenada", "cant_entregada",
    "cant_facturada", "cant_a_facturar", "cant_por_recibir",
]


def _render_lineas(df, socio_label, entrega_label, porrec_label,
                   height=420) -> None:
    d = df.copy()
    d["fecha"] = _fmt_fecha(d["fecha"])
    st.dataframe(
        d[_LINEA_COLS], use_container_width=True, hide_index=True,
        height=height,
        column_config=_lineas_colcfg(socio_label, entrega_label, porrec_label),
    )


def _excel_auditoria(sheets: dict) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for name, df in sheets.items():
            (df if df is not None else pd.DataFrame()).to_excel(
                writer, sheet_name=name[:31], index=False,
            )
    return buffer.getvalue()


def _filtro_meses(df_raw: pd.DataFrame, kp: str) -> pd.DataFrame:
    """Filtro por meses (multiselección). Si no se elige nada, no filtra."""
    st.markdown("**📅 Filtro de meses**")
    if df_raw is None or df_raw.empty:
        return df_raw
    fechas = pd.to_datetime(df_raw["fecha"], errors="coerce")
    periodos = sorted(
        {str(p) for p in fechas.dropna().dt.to_period("M")}, reverse=True,
    )
    sel = st.multiselect(
        "Meses", periodos, default=[], key=f"{kp}_meses",
        help="Deja vacío para incluir todos los meses.",
    )
    if not sel:
        return df_raw
    mask = fechas.dt.to_period("M").astype(str).isin(sel)
    return df_raw[mask].reset_index(drop=True)


def render_audit_page(tipo: str) -> None:
    """Renderiza la página de auditoría completa para 'venta' o 'compra'."""
    cfg = _CFG[tipo]
    socio_label = cfg["socio_label"]
    entrega_label = cfg["entrega_label"]
    porrec_label = cfg["porrec_label"]
    kp = cfg["key_prefix"]

    st.set_page_config(
        page_title=cfg["page_title"], page_icon="🔍", layout="wide",
    )
    require_auth()
    logout_button()

    st.title(cfg["titulo"])
    st.caption(
        "Detecta órdenes con saldos pendientes en dos columnas clave: "
        f"**{_AF_LABEL}** (ordenada − facturada) y "
        f"**{porrec_label}** (facturada − {entrega_label.lower()}). "
        "Solo se listan las líneas con saldo distinto de cero."
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

    # ── Filtro por meses ──
    df_per = _filtro_meses(df_raw, kp)
    df_aud = audit_order_lines(df_per)

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
    c2.metric("Órdenes con pendientes", f"{kpis['n_ordenes_discrepancia']:,}")
    c3.metric(f"Líneas con {_AF_LABEL}", f"{kpis['n_lineas_por_facturar']:,}")
    c4.metric(f"Líneas con {porrec_label}", f"{kpis['n_lineas_por_recibir']:,}")

    if kpis["n_ordenes_discrepancia"] == 0:
        st.success(
            "🎉 No hay saldos pendientes: en todas las órdenes del período "
            "lo ordenado, lo facturado y lo movido coinciden."
        )
        return

    # ── Gráficas ──
    st.markdown("### 📈 Gráficas")
    serie_af = f"Con {_AF_LABEL}"
    serie_pr = f"Con {porrec_label}"
    g1, g2 = st.columns(2)
    with g1:
        mens = audit_by_month(df_aud)
        if not mens.empty:
            mlt = mens[
                ["mes_label", "lineas_por_facturar", "lineas_por_recibir"]
            ].melt(
                id_vars=["mes_label"], var_name="serie", value_name="cantidad",
            )
            mlt["serie"] = mlt["serie"].map({
                "lineas_por_facturar": serie_af,
                "lineas_por_recibir": serie_pr,
            })
            fig = px.bar(
                mlt, x="mes_label", y="cantidad", color="serie",
                barmode="group",
                color_discrete_map={serie_af: "#F0A202", serie_pr: "#E94560"},
                labels={"mes_label": "Mes", "cantidad": "Líneas", "serie": ""},
                title="Líneas con saldo pendiente por mes",
            )
            fig.update_layout(
                height=360, margin=dict(l=0, r=0, t=40, b=0),
                legend=dict(orientation="h", yanchor="bottom", y=1.02),
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.caption("Sin datos mensuales.")
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

    # ── Filtro por tipo de problema ──
    st.markdown("### 🗂️ Detalle filtrado por tipo de problema")
    expl_all = explode_problem_types(df_aud)
    tipos_disp = (
        sorted(expl_all["problema"].dropna().unique())
        if expl_all is not None and not expl_all.empty else []
    )
    tipos_sel = st.multiselect(
        "Tipo de problema", tipos_disp, default=tipos_disp, key=f"{kp}_tipos",
    )
    if not tipos_sel:
        tipos_sel = tipos_disp
    tset = set(tipos_sel)

    lineas_disc = df_aud[df_aud["tiene_discrepancia"]].copy()
    # Filtro vectorizado: regex con todos los tipos seleccionados (mucho
    # más rápido que un `.apply` por fila).
    pat = "|".join(re.escape(t) for t in tipos_sel)
    mask = lineas_disc["tipo_discrepancia"].astype(str).str.contains(
        pat, regex=True, na=False,
    )
    # Orden: fecha más reciente primero (se propaga al detalle y a las
    # tablas agrupadas por tipo de problema).
    lineas_f = (
        lineas_disc[mask]
        .sort_values("fecha", ascending=False)
        .reset_index(drop=True)
        .copy()
    )

    if lineas_f.empty:
        st.info("No hay líneas que cumplan los filtros seleccionados.")
        return

    # ── Resumen por orden ──
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
    r = r[_RESUMEN_COLS]
    st.dataframe(
        r, use_container_width=True, hide_index=True, height=340,
        column_config=_resumen_colcfg(socio_label, entrega_label, porrec_label),
    )

    # ── Detalle por línea ──
    st.markdown(f"#### 🔬 Detalle por línea ({len(lineas_f):,})")
    _render_lineas(lineas_f, socio_label, entrega_label, porrec_label,
                   height=420)

    # ── Tablas agrupadas por tipo de problema ──
    st.markdown("#### 🗃️ Líneas agrupadas por tipo de problema")
    expl_f = explode_problem_types(lineas_f)
    if expl_f is not None and not expl_f.empty:
        expl_f = expl_f[expl_f["problema"].isin(tset)]
    if expl_f is None or expl_f.empty:
        st.caption("Sin líneas para agrupar.")
    else:
        for prob in expl_f["problema"].value_counts().index.tolist():
            sub = expl_f[expl_f["problema"] == prob]
            with st.expander(f"{prob} — {len(sub):,} líneas"):
                _render_lineas(sub, socio_label, entrega_label,
                               porrec_label, height=320)

    # ── Exportación a Excel ──
    det_xlsx = lineas_f.copy()
    det_xlsx["fecha"] = _fmt_fecha(det_xlsx["fecha"])
    sheets = {
        "Resumen por orden": r,
        "Detalle por linea": det_xlsx[_LINEA_COLS],
        "Por mes": audit_by_month(df_aud),
    }
    st.download_button(
        label="⬇️ Descargar auditoría (Excel)",
        data=_excel_auditoria(sheets),
        file_name=f"auditoria_ordenes_{tipo}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=f"{kp}_excel",
    )
