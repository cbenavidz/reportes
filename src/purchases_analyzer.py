# -*- coding: utf-8 -*-
"""
src/purchases_analyzer.py
=========================
Motor de análisis de Compras vs Ventas y Rotación de Inventario.

Cruza:
  - Líneas de facturas de proveedor (in_invoice/in_refund) — qué compré.
  - Líneas de facturas de cliente (out_invoice/out_refund)  — qué vendí.
  - Stock actual (stock.quant)                              — qué tengo.

Genera:
  - Resumen general (totales compras vs ventas, margen bruto, # productos).
  - Cruce por producto / categoría (compras, ventas, gap, rotación).
  - Productos comprados que NO se vendieron en el período.
  - Stock muerto (con stock >0 y sin ventas en últimos N días).
  - Productos para comprar más (alta rotación + cobertura baja).
  - Productos con tendencia creciente (vs período previo).
  - KPIs de rotación de inventario: general y por categoría.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable

import numpy as np
import pandas as pd

from .sales_analyzer import _build_exclusion_mask


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _apply_default_exclusions(lines: pd.DataFrame) -> pd.DataFrame:
    """Aplica las mismas exclusiones que el Informe de Ventas (SOAT, ANTCL)."""
    if lines is None or lines.empty:
        return lines
    mask = _build_exclusion_mask(lines)
    return lines[~mask].copy()


def _normalize_sales_signed(sales: pd.DataFrame) -> pd.DataFrame:
    """Asegura que `sales` tenga `price_subtotal_signed` y `quantity_signed`."""
    if sales is None or sales.empty:
        return sales
    df = sales.copy()
    if "price_subtotal_signed" not in df.columns and "price_subtotal" in df.columns:
        sign = df.get("move_type", pd.Series([], dtype=object)).map(
            {"out_invoice": 1, "out_refund": -1}
        ).fillna(1)
        df["price_subtotal_signed"] = df["price_subtotal"] * sign
    if "quantity_signed" not in df.columns and "quantity" in df.columns:
        sign = df.get("move_type", pd.Series([], dtype=object)).map(
            {"out_invoice": 1, "out_refund": -1}
        ).fillna(1)
        df["quantity_signed"] = df["quantity"] * sign
    return df


def _filter_period(df: pd.DataFrame, date_from: date, date_to: date) -> pd.DataFrame:
    if df is None or df.empty or "invoice_date" not in df.columns:
        return df
    d = pd.to_datetime(df["invoice_date"], errors="coerce").dt.date
    return df[(d >= date_from) & (d <= date_to)].copy()


# ---------------------------------------------------------------------------
# 1. Resumen general
# ---------------------------------------------------------------------------


def compute_purchases_vs_sales_summary(
    purchases: pd.DataFrame,
    sales: pd.DataFrame,
    stock: pd.DataFrame,
    date_from: date,
    date_to: date,
) -> dict:
    """
    KPIs de cabecera para el período.

    Returns dict:
      - total_compras, total_ventas, margen_bruto, margen_pct
      - n_productos_comprados, n_productos_vendidos, n_productos_stock
      - rotacion_general (ventas en costo / stock valorado)
      - dias_inventario_general
    """
    pc = _filter_period(purchases, date_from, date_to) if purchases is not None else pd.DataFrame()
    sl = _filter_period(sales, date_from, date_to) if sales is not None else pd.DataFrame()

    pc = _apply_default_exclusions(pc)
    sl = _apply_default_exclusions(_normalize_sales_signed(sl))

    total_compras = float(pc["price_subtotal_signed"].sum()) if (
        not pc.empty and "price_subtotal_signed" in pc.columns
    ) else 0.0
    total_ventas = float(sl["price_subtotal_signed"].sum()) if (
        not sl.empty and "price_subtotal_signed" in sl.columns
    ) else 0.0
    costo_ventas = float(sl["line_cost"].sum()) if (
        not sl.empty and "line_cost" in sl.columns
    ) else 0.0
    margen_bruto = total_ventas - costo_ventas
    margen_pct = (margen_bruto / total_ventas * 100) if total_ventas else 0.0

    n_prod_compr = int(pc["product_id"].nunique()) if not pc.empty else 0
    n_prod_vend = int(sl["product_id"].nunique()) if not sl.empty else 0
    n_prod_stock = (
        int(stock[stock["qty_available"] > 0]["product_id"].nunique())
        if stock is not None and not stock.empty else 0
    )

    # Rotación general: costo de ventas / stock valorado promedio.
    # Sin stock histórico, usamos stock actual como proxy.
    stock_value = float(stock["stock_value"].sum()) if (
        stock is not None and not stock.empty and "stock_value" in stock.columns
    ) else 0.0
    # Si stock_value=0 (Odoo no lo tiene), usamos qty × standard_price actual
    if stock_value <= 0 and not pc.empty and "product_standard_price" in pc.columns:
        avg_cost = pc.groupby("product_id", as_index=False).agg(
            avg_cost=("product_standard_price", "first")
        )
        stk = stock.merge(avg_cost, on="product_id", how="left").fillna(0)
        stock_value = float((stk["qty_available"] * stk["avg_cost"]).sum())

    rotacion = (costo_ventas / stock_value) if stock_value else 0.0
    period_days = (date_to - date_from).days + 1
    # Anualizar rotación para que sea comparable
    rotacion_anual = rotacion * (365 / period_days) if period_days else 0.0
    dias_inv = (365 / rotacion_anual) if rotacion_anual else 0.0

    return {
        "total_compras": total_compras,
        "total_ventas": total_ventas,
        "costo_ventas": costo_ventas,
        "margen_bruto": margen_bruto,
        "margen_pct": margen_pct,
        "n_productos_comprados": n_prod_compr,
        "n_productos_vendidos": n_prod_vend,
        "n_productos_stock": n_prod_stock,
        "stock_value": stock_value,
        "rotacion_general": rotacion_anual,
        "dias_inventario_general": dias_inv,
        "period_days": period_days,
    }


# ---------------------------------------------------------------------------
# 2. Cruce por producto
# ---------------------------------------------------------------------------


def compute_product_crosstab(
    purchases: pd.DataFrame,
    sales: pd.DataFrame,
    stock: pd.DataFrame,
    date_from: date,
    date_to: date,
) -> pd.DataFrame:
    """
    Devuelve un DF a nivel producto con:
      product_id, product_default_code, product_name, product_categ_name,
      qty_comprada, monto_compras,
      qty_vendida, monto_ventas, costo_ventas, margen, margen_pct,
      stock_qty, stock_valor,
      rotacion (anual), dias_inventario, dias_cobertura.
    """
    pc = _filter_period(purchases, date_from, date_to) if purchases is not None else pd.DataFrame()
    sl = _filter_period(sales, date_from, date_to) if sales is not None else pd.DataFrame()
    pc = _apply_default_exclusions(pc)
    sl = _apply_default_exclusions(_normalize_sales_signed(sl))
    period_days = max((date_to - date_from).days + 1, 1)

    # ---- Lado compras ----
    if not pc.empty:
        gpc = pc.groupby("product_id", as_index=False, dropna=False).agg(
            qty_comprada=("quantity_signed", "sum"),
            monto_compras=("price_subtotal_signed", "sum"),
            product_default_code=("product_default_code", "first"),
            product_name=("product_name", "first"),
            product_categ_name=("product_categ_name", "first"),
            costo_unit_compra=("price_unit", "mean"),
        )
    else:
        gpc = pd.DataFrame(columns=[
            "product_id", "qty_comprada", "monto_compras",
            "product_default_code", "product_name", "product_categ_name",
            "costo_unit_compra",
        ])

    # ---- Lado ventas ----
    if not sl.empty:
        agg_dict = {
            "qty_vendida": ("quantity_signed", "sum"),
            "monto_ventas": ("price_subtotal_signed", "sum"),
            "product_default_code": ("product_default_code", "first"),
            "product_name": ("product_name", "first"),
            "product_categ_name": ("product_categ_name", "first"),
        }
        if "line_cost" in sl.columns:
            agg_dict["costo_ventas"] = ("line_cost", "sum")
        if "line_margin" in sl.columns:
            agg_dict["margen"] = ("line_margin", "sum")
        gsl = sl.groupby("product_id", as_index=False, dropna=False).agg(**agg_dict)
        if "costo_ventas" not in gsl.columns:
            gsl["costo_ventas"] = 0.0
        if "margen" not in gsl.columns:
            gsl["margen"] = gsl["monto_ventas"] - gsl["costo_ventas"]
    else:
        gsl = pd.DataFrame(columns=[
            "product_id", "qty_vendida", "monto_ventas",
            "product_default_code", "product_name", "product_categ_name",
            "costo_ventas", "margen",
        ])

    # ---- Merge con outer join (productos comprados pero no vendidos
    #       y viceversa) ----
    df = pd.merge(
        gpc, gsl, on="product_id", how="outer",
        suffixes=("_pc", "_sl"),
    )

    # Consolidar columnas duplicadas (product_default_code/name/categ_name)
    for base in ["product_default_code", "product_name", "product_categ_name"]:
        col_pc = f"{base}_pc"
        col_sl = f"{base}_sl"
        if col_pc in df.columns and col_sl in df.columns:
            df[base] = df[col_pc].fillna(df[col_sl])
            df = df.drop(columns=[col_pc, col_sl])
        elif col_pc in df.columns:
            df = df.rename(columns={col_pc: base})
        elif col_sl in df.columns:
            df = df.rename(columns={col_sl: base})

    # Fill NaN
    num_cols = [
        "qty_comprada", "monto_compras", "qty_vendida", "monto_ventas",
        "costo_ventas", "margen",
    ]
    for c in num_cols:
        if c in df.columns:
            df[c] = df[c].fillna(0)
        else:
            df[c] = 0

    # ---- Stock ----
    if stock is not None and not stock.empty:
        df = df.merge(
            stock[["product_id", "qty_available", "stock_value"]].rename(
                columns={"qty_available": "stock_qty", "stock_value": "stock_valor"}
            ),
            on="product_id", how="left",
        )
    else:
        df["stock_qty"] = 0
        df["stock_valor"] = 0
    df["stock_qty"] = df["stock_qty"].fillna(0)
    df["stock_valor"] = df["stock_valor"].fillna(0)

    # Si stock_valor es 0 pero hay costo_unit_compra, estimar
    if "costo_unit_compra" in df.columns:
        mask = (df["stock_valor"] == 0) & (df["stock_qty"] > 0)
        df.loc[mask, "stock_valor"] = (
            df.loc[mask, "stock_qty"] * df.loc[mask, "costo_unit_compra"].fillna(0)
        )

    # ---- Métricas calculadas ----
    df["gap_qty"] = df["qty_comprada"] - df["qty_vendida"]
    df["gap_monto"] = df["monto_compras"] - df["monto_ventas"]
    df["margen_pct"] = np.where(
        df["monto_ventas"] != 0,
        df["margen"] / df["monto_ventas"] * 100, 0,
    )

    # Velocidad de venta (unidades/día en el período)
    df["unidades_por_dia"] = df["qty_vendida"] / period_days

    # Días de cobertura: stock_qty / unidades_por_dia
    df["dias_cobertura"] = np.where(
        df["unidades_por_dia"] > 0,
        df["stock_qty"] / df["unidades_por_dia"],
        np.inf,
    )
    # Capear infinito para mejor visualización (999 días "ilimitado")
    df["dias_cobertura"] = df["dias_cobertura"].replace([np.inf, -np.inf], 999.0)
    # Donde no se vendió, dias_cobertura como NaN para distinguir
    df.loc[df["qty_vendida"] <= 0, "dias_cobertura"] = np.nan

    # Rotación anual (cantidad): qty_vendida / stock_qty × 365/period_days
    df["rotacion_anual"] = np.where(
        df["stock_qty"] > 0,
        (df["qty_vendida"] / df["stock_qty"]) * (365 / period_days),
        np.where(df["qty_vendida"] > 0, np.inf, 0),  # sin stock pero con venta = "rota muy rápido"
    )
    df["rotacion_anual"] = df["rotacion_anual"].replace([np.inf, -np.inf], 999.0)

    # Días de inventario: 365 / rotación
    df["dias_inventario"] = np.where(
        df["rotacion_anual"] > 0,
        365 / df["rotacion_anual"],
        np.nan,
    )

    # Ordenar por monto_ventas descendente por default
    df = df.sort_values("monto_ventas", ascending=False).reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# 3. Por categoría
# ---------------------------------------------------------------------------


def compute_category_crosstab(crosstab: pd.DataFrame) -> pd.DataFrame:
    """Agregar el crosstab de producto por categoría."""
    if crosstab is None or crosstab.empty:
        return pd.DataFrame()
    df = crosstab.copy()
    df["product_categ_name"] = df["product_categ_name"].fillna("(Sin categoría)")
    cat = df.groupby("product_categ_name", as_index=False).agg(
        n_productos=("product_id", "nunique"),
        qty_comprada=("qty_comprada", "sum"),
        monto_compras=("monto_compras", "sum"),
        qty_vendida=("qty_vendida", "sum"),
        monto_ventas=("monto_ventas", "sum"),
        costo_ventas=("costo_ventas", "sum"),
        margen=("margen", "sum"),
        stock_qty=("stock_qty", "sum"),
        stock_valor=("stock_valor", "sum"),
    )
    cat["gap_monto"] = cat["monto_compras"] - cat["monto_ventas"]
    cat["margen_pct"] = np.where(
        cat["monto_ventas"] != 0, cat["margen"] / cat["monto_ventas"] * 100, 0
    )
    # Rotación de categoría (anual) = costo_ventas / stock_valor
    cat["rotacion_anual"] = np.where(
        cat["stock_valor"] > 0, cat["costo_ventas"] / cat["stock_valor"], 0,
    )
    cat["dias_inventario"] = np.where(
        cat["rotacion_anual"] > 0, 365 / cat["rotacion_anual"], np.nan,
    )
    cat = cat.sort_values("monto_ventas", ascending=False).reset_index(drop=True)
    return cat


# ---------------------------------------------------------------------------
# 4. Listados especiales
# ---------------------------------------------------------------------------


def find_purchased_not_sold(crosstab: pd.DataFrame) -> pd.DataFrame:
    """Productos comprados en el período pero con 0 ventas en el mismo."""
    if crosstab is None or crosstab.empty:
        return pd.DataFrame()
    df = crosstab[
        (crosstab["qty_comprada"] > 0) & (crosstab["qty_vendida"] <= 0)
    ].copy()
    df = df.sort_values("monto_compras", ascending=False).reset_index(drop=True)
    return df


def find_dead_stock(
    crosstab: pd.DataFrame,
    sales_full: pd.DataFrame,
    cutoff_date: date,
    days_no_sale: int = 90,
) -> pd.DataFrame:
    """
    Stock muerto: stock > 0 y SIN ventas en los últimos `days_no_sale` días
    (independiente del período del informe — esto mira histórico completo
    contra `cutoff_date`).
    """
    if crosstab is None or crosstab.empty:
        return pd.DataFrame()
    sl = sales_full.copy() if sales_full is not None and not sales_full.empty else pd.DataFrame()
    sl = _apply_default_exclusions(_normalize_sales_signed(sl))

    fecha_corte = cutoff_date - timedelta(days=days_no_sale)
    if not sl.empty and "invoice_date" in sl.columns:
        sl["_d"] = pd.to_datetime(sl["invoice_date"], errors="coerce").dt.date
        recientes = (
            sl[(sl["_d"] >= fecha_corte) & (sl["quantity_signed"] > 0)]
            ["product_id"].dropna().astype(int).unique().tolist()
        )
    else:
        recientes = []

    df = crosstab[
        (crosstab["stock_qty"] > 0)
        & (~crosstab["product_id"].isin(recientes))
    ].copy()
    df["dias_sin_venta"] = days_no_sale  # mínimo garantizado
    df = df.sort_values("stock_valor", ascending=False).reset_index(drop=True)
    return df


def find_to_purchase(
    crosstab: pd.DataFrame,
    cobertura_max_dias: int = 30,
) -> pd.DataFrame:
    """
    Productos para comprar más: alta velocidad de venta y stock que duraría
    menos de `cobertura_max_dias` al ritmo actual.
    """
    if crosstab is None or crosstab.empty:
        return pd.DataFrame()
    df = crosstab[
        (crosstab["qty_vendida"] > 0)
        & (crosstab["dias_cobertura"].notna())
        & (crosstab["dias_cobertura"] < cobertura_max_dias)
    ].copy()
    # Sugerencia de compra: cuantas unidades faltan para alcanzar 60 días
    target_days = max(cobertura_max_dias * 2, 60)
    df["unidades_sugeridas"] = (
        df["unidades_por_dia"] * target_days - df["stock_qty"]
    ).clip(lower=0)
    df = df.sort_values("monto_ventas", ascending=False).reset_index(drop=True)
    return df


def find_trending_up(
    sales_current: pd.DataFrame,
    sales_prev: pd.DataFrame,
    min_growth_pct: float = 30.0,
    min_qty_current: float = 1.0,
) -> pd.DataFrame:
    """
    Productos con tendencia creciente: crecieron al menos `min_growth_pct`%
    en ventas vs el período anterior.
    """
    sc = _apply_default_exclusions(_normalize_sales_signed(sales_current))
    sp = _apply_default_exclusions(_normalize_sales_signed(sales_prev))
    if sc is None or sc.empty:
        return pd.DataFrame()

    cur = sc.groupby("product_id", as_index=False, dropna=False).agg(
        qty_act=("quantity_signed", "sum"),
        monto_act=("price_subtotal_signed", "sum"),
        product_default_code=("product_default_code", "first"),
        product_name=("product_name", "first"),
        product_categ_name=("product_categ_name", "first"),
    )
    if sp is None or sp.empty:
        prev = pd.DataFrame(columns=["product_id", "qty_prev", "monto_prev"])
    else:
        prev = sp.groupby("product_id", as_index=False, dropna=False).agg(
            qty_prev=("quantity_signed", "sum"),
            monto_prev=("price_subtotal_signed", "sum"),
        )

    df = cur.merge(prev, on="product_id", how="left").fillna(
        {"qty_prev": 0, "monto_prev": 0}
    )
    df["delta_monto"] = df["monto_act"] - df["monto_prev"]
    df["pct_var_monto"] = np.where(
        df["monto_prev"] > 0,
        df["delta_monto"] / df["monto_prev"] * 100,
        np.where(df["monto_act"] > 0, 100.0, 0),
    )
    df = df[
        (df["qty_act"] >= min_qty_current)
        & (df["pct_var_monto"] >= min_growth_pct)
    ].copy()
    df = df.sort_values("delta_monto", ascending=False).reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# 5. Evolución mensual de compras vs ventas
# ---------------------------------------------------------------------------


def compute_monthly_evolution(
    purchases: pd.DataFrame,
    sales: pd.DataFrame,
    date_from: date,
    date_to: date,
) -> pd.DataFrame:
    """Serie mensual de compras vs ventas."""
    pc = _filter_period(purchases, date_from, date_to) if purchases is not None else pd.DataFrame()
    sl = _filter_period(sales, date_from, date_to) if sales is not None else pd.DataFrame()
    pc = _apply_default_exclusions(pc)
    sl = _apply_default_exclusions(_normalize_sales_signed(sl))

    frames = []
    if not pc.empty:
        pc_m = pc.copy()
        pc_m["mes"] = pd.to_datetime(
            pc_m["invoice_date"]
        ).dt.to_period("M").dt.to_timestamp()
        f = pc_m.groupby("mes", as_index=False)["price_subtotal_signed"].sum()
        f = f.rename(columns={"price_subtotal_signed": "compras"})
        frames.append(f)
    if not sl.empty:
        sl_m = sl.copy()
        sl_m["mes"] = pd.to_datetime(
            sl_m["invoice_date"]
        ).dt.to_period("M").dt.to_timestamp()
        f = sl_m.groupby("mes", as_index=False)["price_subtotal_signed"].sum()
        f = f.rename(columns={"price_subtotal_signed": "ventas"})
        frames.append(f)
    if not frames:
        return pd.DataFrame(columns=["mes", "compras", "ventas"])
    out = frames[0]
    for f in frames[1:]:
        out = out.merge(f, on="mes", how="outer")
    out = out.fillna(0).sort_values("mes").reset_index(drop=True)
    if "compras" not in out.columns:
        out["compras"] = 0
    if "ventas" not in out.columns:
        out["ventas"] = 0
    out["gap"] = out["compras"] - out["ventas"]
    return out
