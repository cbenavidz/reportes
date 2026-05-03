# -*- coding: utf-8 -*-
"""
Análisis profundo por cliente (drilldown).

Funciones para enriquecer la página de Detalle Cliente:
  - compute_customer_sales_kpis: ventas netas, # facturas, ticket, volumen
  - compute_customer_top_products: top productos comprados por el cliente
  - compute_customer_by_category: ventas por categoría del cliente
  - predict_next_purchase: estimación de cuándo volverá a comprar
  - compute_peer_comparison: cliente vs promedio de su grupo
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Iterable

import numpy as np
import pandas as pd

from .sales_analyzer import _filter_lines_for_sales, filter_excluded_products

logger = logging.getLogger(__name__)


# =============================================================================
# KPIs de ventas del cliente
# =============================================================================

def compute_customer_sales_kpis(
    invoice_lines: pd.DataFrame,
    partner_id: int,
    date_from: date | pd.Timestamp | None = None,
    date_to: date | pd.Timestamp | None = None,
    company_ids: Iterable[int] | None = None,
) -> dict:
    """
    KPIs de venta para un cliente específico en el período.

    Devuelve dict con:
      - ventas_netas: subtotal sin IVA, NC restando.
      - n_facturas: facturas únicas (out_invoice).
      - ticket_promedio
      - volumen_total: suma de quantity × product.volume
      - n_productos_distintos: cuántos productos distintos compró
      - n_categorias_distintas: cuántas categorías
    """
    empty = {
        "ventas_netas": 0.0, "n_facturas": 0, "ticket_promedio": 0.0,
        "volumen_total": 0.0, "n_productos_distintos": 0,
        "n_categorias_distintas": 0,
    }
    if invoice_lines is None or invoice_lines.empty:
        return empty

    df = _filter_lines_for_sales(
        invoice_lines, date_from=date_from, date_to=date_to, company_ids=company_ids,
    )
    if df.empty:
        return empty
    df = df[df["partner_id"] == int(partner_id)].copy()
    if df.empty:
        return empty

    # Volumen físico signed
    sign = df["move_type"].map({"out_invoice": 1, "out_refund": -1}).fillna(1)
    qty = pd.to_numeric(df["quantity"], errors="coerce").fillna(0)
    unit_vol = pd.to_numeric(df.get("product_volume", 0), errors="coerce").fillna(0)
    df["_vol_signed"] = qty * unit_vol * sign

    is_fac = df["move_type"] == "out_invoice"
    ventas_netas = float(df["price_subtotal_signed"].sum())
    ventas_brutas = float(df.loc[is_fac, "price_subtotal_signed"].sum())
    n_fac = int(df.loc[is_fac, "move_id"].nunique())
    ticket = ventas_brutas / n_fac if n_fac else 0.0
    volumen = float(df["_vol_signed"].sum())
    n_prod = int(df["product_id"].dropna().nunique()) if "product_id" in df.columns else 0
    n_cat = (
        int(df["product_categ_name"].dropna().nunique())
        if "product_categ_name" in df.columns else 0
    )

    return {
        "ventas_netas": ventas_netas, "n_facturas": n_fac,
        "ticket_promedio": ticket, "volumen_total": volumen,
        "n_productos_distintos": n_prod, "n_categorias_distintas": n_cat,
    }


# =============================================================================
# Top productos / categorías
# =============================================================================

def compute_customer_top_products(
    invoice_lines: pd.DataFrame,
    partner_id: int,
    date_from: date | pd.Timestamp | None = None,
    date_to: date | pd.Timestamp | None = None,
    company_ids: Iterable[int] | None = None,
    top_n: int = 20,
) -> pd.DataFrame:
    """
    Productos más comprados por el cliente en el período.
    Excluye SOAT/ANTCL.
    """
    cols = [
        "product_id", "product_name", "product_default_code",
        "product_categ_name", "cantidad", "volumen",
        "ventas_netas", "n_facturas", "participacion_pct",
    ]
    if invoice_lines is None or invoice_lines.empty:
        return pd.DataFrame(columns=cols)

    df = _filter_lines_for_sales(
        invoice_lines, date_from=date_from, date_to=date_to, company_ids=company_ids,
    )
    if df.empty:
        return pd.DataFrame(columns=cols)
    df = df[df["partner_id"] == int(partner_id)].copy()
    df = filter_excluded_products(df)
    if df.empty:
        return pd.DataFrame(columns=cols)

    # Volumen físico
    sign = df["move_type"].map({"out_invoice": 1, "out_refund": -1}).fillna(1)
    qty = pd.to_numeric(df["quantity"], errors="coerce").fillna(0)
    unit_vol = pd.to_numeric(df.get("product_volume", 0), errors="coerce").fillna(0)
    df["_vol_signed"] = qty * unit_vol * sign
    df["_qty_signed"] = qty * sign

    df["product_id"] = pd.to_numeric(df["product_id"], errors="coerce").fillna(-1).astype(int)

    grp = df.groupby("product_id")
    res = pd.DataFrame({
        "cantidad": grp["_qty_signed"].sum(),
        "volumen": grp["_vol_signed"].sum(),
        "ventas_netas": grp["price_subtotal_signed"].sum(),
        "n_facturas": grp["move_id"].nunique(),
    }).reset_index()

    # Nombre / código / categoría
    nombres = (
        df.dropna(subset=["product_name"])
        .drop_duplicates("product_id")
        .set_index("product_id")
        [["product_name", "product_default_code", "product_categ_name"]]
        .to_dict(orient="index")
    )
    res["product_name"] = res["product_id"].map(
        lambda i: nombres.get(int(i), {}).get("product_name", "—")
    )
    res["product_default_code"] = res["product_id"].map(
        lambda i: nombres.get(int(i), {}).get("product_default_code") or "—"
    )
    res["product_categ_name"] = res["product_id"].map(
        lambda i: nombres.get(int(i), {}).get("product_categ_name") or "—"
    )

    total = float(res["ventas_netas"].sum())
    res["participacion_pct"] = (
        res["ventas_netas"] / total * 100 if total else 0.0
    )

    res = res.sort_values("ventas_netas", ascending=False).reset_index(drop=True)
    if top_n is not None:
        res = res.head(top_n)
    return res[cols]


def compute_customer_by_category(
    invoice_lines: pd.DataFrame,
    partner_id: int,
    date_from: date | pd.Timestamp | None = None,
    date_to: date | pd.Timestamp | None = None,
    company_ids: Iterable[int] | None = None,
) -> pd.DataFrame:
    """Ventas del cliente agrupadas por categoría."""
    cols = ["product_categ_name", "cantidad", "volumen",
            "ventas_netas", "n_facturas", "participacion_pct"]
    if invoice_lines is None or invoice_lines.empty:
        return pd.DataFrame(columns=cols)

    df = _filter_lines_for_sales(
        invoice_lines, date_from=date_from, date_to=date_to, company_ids=company_ids,
    )
    if df.empty:
        return pd.DataFrame(columns=cols)
    df = df[df["partner_id"] == int(partner_id)].copy()
    df = filter_excluded_products(df)
    if df.empty:
        return pd.DataFrame(columns=cols)

    sign = df["move_type"].map({"out_invoice": 1, "out_refund": -1}).fillna(1)
    qty = pd.to_numeric(df["quantity"], errors="coerce").fillna(0)
    unit_vol = pd.to_numeric(df.get("product_volume", 0), errors="coerce").fillna(0)
    df["_vol_signed"] = qty * unit_vol * sign
    df["_qty_signed"] = qty * sign
    df["product_categ_name"] = (
        df["product_categ_name"].fillna("Sin categoría").replace("", "Sin categoría")
    )

    grp = df.groupby("product_categ_name")
    res = pd.DataFrame({
        "cantidad": grp["_qty_signed"].sum(),
        "volumen": grp["_vol_signed"].sum(),
        "ventas_netas": grp["price_subtotal_signed"].sum(),
        "n_facturas": grp["move_id"].nunique(),
    }).reset_index()
    total = float(res["ventas_netas"].sum())
    res["participacion_pct"] = (
        res["ventas_netas"] / total * 100 if total else 0.0
    )
    return res.sort_values("ventas_netas", ascending=False).reset_index(drop=True)[cols]


# =============================================================================
# Predicción de próxima compra
# =============================================================================

def predict_next_purchase(
    invoice_lines: pd.DataFrame,
    partner_id: int,
    cutoff: date | pd.Timestamp | None = None,
    company_ids: Iterable[int] | None = None,
) -> dict:
    """
    Estima cuándo volverá a comprar el cliente.

    Método: usa la mediana del intervalo entre compras históricas como
    "frecuencia típica". Si la última compra fue hace `dias_desde_ultima`,
    la próxima compra esperada es en `mediana_intervalo - dias_desde_ultima`
    días desde hoy. Si ya pasó, está "atrasado".

    Devuelve dict con:
      - n_compras_historicas
      - dias_desde_ultima
      - intervalo_mediano (días)
      - intervalo_promedio
      - fecha_proxima_estimada
      - dias_para_proxima_compra (negativo = atrasado)
      - estado: 'al_dia' | 'atrasado' | 'sin_historico_suficiente'
    """
    cutoff_ts = pd.Timestamp(cutoff) if cutoff is not None else pd.Timestamp(date.today())
    empty = {
        "n_compras_historicas": 0, "dias_desde_ultima": None,
        "intervalo_mediano": None, "intervalo_promedio": None,
        "fecha_proxima_estimada": None, "dias_para_proxima_compra": None,
        "estado": "sin_historico_suficiente",
    }
    if invoice_lines is None or invoice_lines.empty:
        return empty

    df = _filter_lines_for_sales(invoice_lines, company_ids=company_ids)
    if df.empty:
        return empty
    df = df[df["partner_id"] == int(partner_id)]
    df = df[df["move_type"] == "out_invoice"]  # solo facturas, no NC
    if df.empty:
        return empty

    fechas = (
        df.drop_duplicates("move_id")["_d"]
        .sort_values().reset_index(drop=True)
    )
    if len(fechas) < 2:
        return {
            **empty,
            "n_compras_historicas": int(len(fechas)),
            "dias_desde_ultima": (cutoff_ts - fechas.iloc[-1]).days if len(fechas) else None,
        }

    intervalos = fechas.diff().dropna().dt.days
    mediano = float(intervalos.median())
    promedio = float(intervalos.mean())
    ultima = fechas.iloc[-1]
    dias_desde_ultima = int((cutoff_ts - ultima).days)
    dias_para_proxima = int(mediano - dias_desde_ultima)
    fecha_proxima = cutoff_ts + pd.Timedelta(days=dias_para_proxima)

    if dias_para_proxima >= 0:
        estado = "al_dia"
    else:
        estado = "atrasado"

    return {
        "n_compras_historicas": int(len(fechas)),
        "dias_desde_ultima": dias_desde_ultima,
        "intervalo_mediano": mediano,
        "intervalo_promedio": promedio,
        "fecha_proxima_estimada": fecha_proxima,
        "dias_para_proxima_compra": dias_para_proxima,
        "estado": estado,
    }


# =============================================================================
# Comparativa vs grupo
# =============================================================================

def compute_peer_comparison(
    scored_df: pd.DataFrame,
    partner_id: int,
    group_by: str = "calificacion",
) -> dict:
    """
    Compara el cliente contra el promedio de su grupo (calificación,
    ciudad, etc.).

    Devuelve dict con:
      - cliente: dict de métricas del cliente
      - peer: dict de métricas promedio del grupo
      - delta_pct: dict con diferencia porcentual del cliente vs grupo
    """
    if scored_df is None or scored_df.empty:
        return {"cliente": {}, "peer": {}, "delta_pct": {}}

    if group_by not in scored_df.columns:
        return {"cliente": {}, "peer": {}, "delta_pct": {}}

    cli_row = scored_df[scored_df["partner_id"] == int(partner_id)]
    if cli_row.empty:
        return {"cliente": {}, "peer": {}, "delta_pct": {}}

    cli = cli_row.iloc[0]
    grupo_val = cli.get(group_by)
    if pd.isna(grupo_val):
        return {"cliente": {}, "peer": {}, "delta_pct": {}}

    peer = scored_df[
        (scored_df[group_by] == grupo_val)
        & (scored_df["partner_id"] != int(partner_id))
    ]
    if peer.empty:
        return {"cliente": {}, "peer": {}, "delta_pct": {}}

    metrics = [
        "saldo_actual", "monto_vencido", "plazo_promedio_dias",
        "dso_cliente", "dias_vencido_max", "pct_pagado_a_tiempo",
        "num_facturas_pagadas", "credit_limit",
    ]
    metrics = [m for m in metrics if m in scored_df.columns]

    cliente_dict = {m: float(pd.to_numeric(cli.get(m), errors="coerce") or 0) for m in metrics}
    peer_dict = {m: float(peer[m].apply(pd.to_numeric, errors="coerce").mean() or 0) for m in metrics}
    delta_pct = {}
    for m in metrics:
        c, p = cliente_dict[m], peer_dict[m]
        if p:
            delta_pct[m] = (c - p) / p * 100
        else:
            delta_pct[m] = None

    return {
        "cliente": cliente_dict,
        "peer": peer_dict,
        "delta_pct": delta_pct,
        "grupo_etiqueta": str(grupo_val),
        "n_peers": int(len(peer)),
    }


# =============================================================================
# Tendencia mensual de ventas del cliente
# =============================================================================

def compute_customer_monthly_sales(
    invoice_lines: pd.DataFrame,
    partner_id: int,
    months: int = 12,
    cutoff_date: date | None = None,
    company_ids: Iterable[int] | None = None,
) -> pd.DataFrame:
    """
    Tendencia mensual de ventas del cliente (subtotal sin IVA).
    """
    if cutoff_date is None:
        cutoff_date = date.today()
    cutoff_ts = pd.Timestamp(cutoff_date)
    end_period = cutoff_ts.to_period("M").to_timestamp(how="end")
    start_period = (
        end_period.to_period("M") - (months - 1)
    ).to_timestamp(how="start")

    df = _filter_lines_for_sales(
        invoice_lines, date_from=start_period, date_to=end_period,
        company_ids=company_ids,
    )
    full_index = pd.period_range(start=start_period, end=end_period, freq="M")
    base = pd.DataFrame(index=full_index)
    base.index.name = "mes"

    if df.empty or partner_id is None:
        out = base.assign(ventas_netas=0.0, volumen=0.0, n_facturas=0).reset_index()
    else:
        df = df[df["partner_id"] == int(partner_id)].copy()
        if df.empty:
            out = base.assign(ventas_netas=0.0, volumen=0.0, n_facturas=0).reset_index()
        else:
            df["mes"] = df["_d"].dt.to_period("M")
            sign = df["move_type"].map({"out_invoice": 1, "out_refund": -1}).fillna(1)
            qty = pd.to_numeric(df["quantity"], errors="coerce").fillna(0)
            unit_vol = pd.to_numeric(df.get("product_volume", 0), errors="coerce").fillna(0)
            df["_vol_signed"] = qty * unit_vol * sign
            is_fac = df["move_type"] == "out_invoice"
            agg = pd.DataFrame({
                "ventas_netas": df.groupby("mes")["price_subtotal_signed"].sum(),
                "volumen": df.groupby("mes")["_vol_signed"].sum(),
                "n_facturas": df.loc[is_fac].groupby("mes")["move_id"].nunique(),
            })
            agg = base.join(agg, how="left").fillna(0.0)
            agg["n_facturas"] = agg["n_facturas"].astype(int)
            out = agg.reset_index()
    out["mes_label"] = out["mes"].astype(str)
    return out
