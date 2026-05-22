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
from .financial_statements import enrich_chart_with_puc


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
        _pc_agg = {
            "qty_comprada": ("quantity_signed", "sum"),
            "monto_compras": ("price_subtotal_signed", "sum"),
            "product_default_code": ("product_default_code", "first"),
            "product_name": ("product_name", "first"),
            "product_categ_name": ("product_categ_name", "first"),
            "costo_unit_compra": ("price_unit", "mean"),
        }
        # Tipo de producto (storable/service) — para poder filtrar servicios
        if "product_type" in pc.columns:
            _pc_agg["product_type"] = ("product_type", "first")
        if "product_is_storable" in pc.columns:
            _pc_agg["product_is_storable"] = ("product_is_storable", "first")
        gpc = pc.groupby("product_id", as_index=False, dropna=False).agg(
            **_pc_agg
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


# ---------------------------------------------------------------------------
# 6. Rotación de inventario desde la cuenta 14 del balance contable
# ---------------------------------------------------------------------------


def _saldo_cuenta_14_from_balances(
    balances_aggregated: pd.DataFrame,
    chart_e: pd.DataFrame,
) -> tuple[float, pd.DataFrame]:
    """
    Helper: dado un balance agregado y un chart enriquecido, devuelve
    (saldo_total_cuenta_14, df_detalle_por_cuenta).
    """
    if balances_aggregated is None or balances_aggregated.empty:
        return 0.0, pd.DataFrame()
    if chart_e is None or chart_e.empty:
        return 0.0, pd.DataFrame()

    keep_cols = [c for c in [
        "id", "code", "name", "puc_subgroup", "subgrupo", "account_type",
    ] if c in chart_e.columns]
    keep = chart_e[keep_cols].rename(columns={
        "id": "account_id", "code": "account_code", "name": "account_name",
    })
    df = balances_aggregated.merge(keep, on="account_id", how="left").copy()
    df["account_code"] = df["account_code"].astype(str)
    inv = df[df["account_code"].str.startswith("14")].copy()
    if inv.empty and "puc_subgroup" in df.columns:
        inv = df[df["puc_subgroup"].astype(str) == "14"].copy()
    if inv.empty and "account_type" in df.columns:
        inv = df[
            df["account_type"].astype(str).str.contains(
                "stock|inventory", case=False, na=False,
            )
        ].copy()
    if inv.empty:
        return 0.0, pd.DataFrame()
    inv["saldo"] = inv.get("debit", 0).fillna(0) - inv.get("credit", 0).fillna(0)
    saldo = float(inv["saldo"].sum())
    detalle = inv[[
        c for c in ["account_code", "account_name", "saldo"]
        if c in inv.columns
    ]].sort_values("saldo", ascending=False).reset_index(drop=True)
    return saldo, detalle


def get_inventory_account_ids(chart: pd.DataFrame) -> list[int]:
    """
    Devuelve los IDs de las cuentas de inventario (cuenta 14 PUC).

    Usa la misma cascada de detección que _saldo_cuenta_14_from_balances:
      1) código que empieza por '14'
      2) puc_subgroup == '14'
      3) account_type que contenga 'stock'/'inventory'

    Se usa para optimizar la serie mensual de saldo de inventario
    (load_inventory_balance_monthly_series): en lugar de N consultas de
    balance, se filtran los movimientos directamente por estas cuentas.
    """
    if chart is None or chart.empty:
        return []
    chart_e = enrich_chart_with_puc(chart)
    if "id" not in chart_e.columns:
        return []

    mask = None
    if "code" in chart_e.columns:
        mask = chart_e["code"].astype(str).str.startswith("14")
    if (mask is None or not mask.any()) and "puc_subgroup" in chart_e.columns:
        mask = chart_e["puc_subgroup"].astype(str) == "14"
    if (mask is None or not mask.any()) and "account_type" in chart_e.columns:
        mask = chart_e["account_type"].astype(str).str.contains(
            "stock|inventory", case=False, na=False,
        )
    if mask is None or not mask.any():
        return []
    return chart_e.loc[mask, "id"].dropna().astype(int).tolist()


def compute_rotacion_cuenta_14(
    balances_inicial: pd.DataFrame,
    balances_final: pd.DataFrame,
    chart: pd.DataFrame,
    total_ventas: float,
    total_costo_ventas: float,
    date_from: date,
    date_to: date,
) -> dict:
    """
    Rotación de inventario con inventario PROMEDIO y dos métodos.

    Denominador: (Saldo cuenta 14 al inicio + Saldo cuenta 14 al cierre) / 2
                 Si solo hay uno disponible, se usa ese.

    Métodos calculados:
      - Método 1 (Ventas / Inv. promedio):
          Más generoso. Es la simplificación inicial que pediste.
      - Método 2 (Costo de ventas / Inv. promedio) — NIIF clásico:
          La fórmula contable estándar usada en informes financieros.

    Devuelve dict con:
      - saldo_inicial, saldo_final, saldo_promedio
      - rotacion_ventas_periodo, rotacion_ventas_anual, dias_ventas
      - rotacion_costo_periodo, rotacion_costo_anual, dias_costo
      - period_days
      - cuentas_detalle: cuentas 14 al CIERRE con saldo
    """
    period_days = max((date_to - date_from).days + 1, 1)

    if chart is None or chart.empty:
        return {
            "saldo_inicial": 0.0, "saldo_final": 0.0, "saldo_promedio": 0.0,
            "rotacion_ventas_periodo": 0.0, "rotacion_ventas_anual": 0.0,
            "dias_ventas": 0.0,
            "rotacion_costo_periodo": 0.0, "rotacion_costo_anual": 0.0,
            "dias_costo": 0.0,
            "period_days": period_days, "cuentas_detalle": pd.DataFrame(),
        }

    chart_e = enrich_chart_with_puc(chart)
    saldo_ini, _ = _saldo_cuenta_14_from_balances(balances_inicial, chart_e)
    saldo_fin, detalle = _saldo_cuenta_14_from_balances(balances_final, chart_e)

    # Inventario promedio: si ambos disponibles, promedio; si solo uno, ese.
    if saldo_ini > 0 and saldo_fin > 0:
        saldo_prom = (saldo_ini + saldo_fin) / 2.0
    elif saldo_fin > 0:
        saldo_prom = saldo_fin
    elif saldo_ini > 0:
        saldo_prom = saldo_ini
    else:
        saldo_prom = 0.0

    # Método 1: Ventas / Inv. promedio
    rot_v_per = (float(total_ventas) / saldo_prom) if saldo_prom else 0.0
    rot_v_anu = rot_v_per * (365.0 / period_days) if period_days else 0.0
    dias_v = (365.0 / rot_v_anu) if rot_v_anu > 0 else 0.0

    # Método 2 (NIIF): Costo de ventas / Inv. promedio
    rot_c_per = (float(total_costo_ventas) / saldo_prom) if saldo_prom else 0.0
    rot_c_anu = rot_c_per * (365.0 / period_days) if period_days else 0.0
    dias_c = (365.0 / rot_c_anu) if rot_c_anu > 0 else 0.0

    return {
        "saldo_inicial": saldo_ini,
        "saldo_final": saldo_fin,
        "saldo_promedio": saldo_prom,
        # Retrocompatibilidad: la antigua propiedad
        "saldo_inventario": saldo_fin,
        "rotacion_ventas_periodo": rot_v_per,
        "rotacion_ventas_anual": rot_v_anu,
        "dias_ventas": dias_v,
        "rotacion_costo_periodo": rot_c_per,
        "rotacion_costo_anual": rot_c_anu,
        "dias_costo": dias_c,
        # Retrocompatibilidad con la API anterior
        "rotacion_periodo": rot_v_per,
        "rotacion_anual": rot_v_anu,
        "dias_inventario": dias_v,
        "period_days": period_days,
        "cuentas_detalle": detalle,
    }


# ---------------------------------------------------------------------------
# 7. Recomendaciones automáticas de inventario
# ---------------------------------------------------------------------------


def compute_inventory_recommendations(
    rot14_act: dict,
    rot14_prev: dict,
    cat_tab: pd.DataFrame,
    crosstab: pd.DataFrame,
    monthly_series: pd.DataFrame,
    summary_act: dict,
    summary_prev: dict,
) -> list[dict]:
    """
    Genera lista de recomendaciones accionables sobre inventario.

    Cada recomendación: {tipo, prioridad, titulo, detalle, accion}.
    Prioridad: 'alta', 'media', 'baja'.
    """
    recs: list[dict] = []

    rot_act = float(rot14_act.get("rotacion_anual", 0) or 0)
    dias_act = float(rot14_act.get("dias_inventario", 0) or 0)
    saldo_act = float(rot14_act.get("saldo_inventario", 0) or 0)
    saldo_prev = float(rot14_prev.get("saldo_inventario", 0) or 0)
    ventas_act = float(summary_act.get("total_ventas", 0) or 0)
    ventas_prev = float(summary_prev.get("total_ventas", 0) or 0)

    # --- 1. Velocidad general ---
    if rot_act > 0:
        if dias_act > 90:
            recs.append({
                "tipo": "🐢 Rotación lenta",
                "prioridad": "alta",
                "titulo": f"Inventario rota {rot_act:.1f}x al año "
                         f"({dias_act:.0f} días)",
                "detalle": (
                    f"Tienes ${saldo_act:,.0f} en cuenta 14 que tarda "
                    f"{dias_act:.0f} días en convertirse en venta. "
                    "Capital atrapado en mercancía."
                ),
                "accion": (
                    "Revisa stock muerto en la pestaña 'Por producto'. "
                    "Considera liquidación o promoción de referencias lentas. "
                    "Negocia con proveedores mejores condiciones de plazo."
                ),
            })
        elif dias_act < 20:
            recs.append({
                "tipo": "⚡ Rotación muy alta",
                "prioridad": "media",
                "titulo": f"Inventario rota {rot_act:.1f}x al año "
                         f"({dias_act:.0f} días)",
                "detalle": (
                    "La rotación es muy alta, lo cual es bueno, pero también "
                    "indica riesgo de quiebres de stock. Si el cliente no "
                    "encuentra el producto, se va a la competencia."
                ),
                "accion": (
                    "Revisa la pestaña 'Comprar más' para identificar "
                    "referencias con cobertura baja. Aumenta el stock de "
                    "seguridad en los productos top."
                ),
            })
        else:
            recs.append({
                "tipo": "✅ Rotación saludable",
                "prioridad": "baja",
                "titulo": f"Inventario rota {rot_act:.1f}x al año "
                         f"({dias_act:.0f} días)",
                "detalle": (
                    "La rotación está en un rango sano (20-90 días). "
                    "Mantén el monitoreo mensual."
                ),
                "accion": "Sin acción urgente.",
            })

    # --- 2. Inventario creciendo más rápido que ventas ---
    if saldo_prev > 0 and ventas_prev > 0:
        crecimiento_inv = (saldo_act - saldo_prev) / saldo_prev * 100
        crecimiento_ventas = (ventas_act - ventas_prev) / ventas_prev * 100
        if crecimiento_inv > crecimiento_ventas + 10:
            recs.append({
                "tipo": "📈 Inventario crece más que ventas",
                "prioridad": "alta",
                "titulo": (
                    f"Inventario +{crecimiento_inv:.1f}% vs ventas "
                    f"+{crecimiento_ventas:.1f}%"
                ),
                "detalle": (
                    "Estás acumulando mercancía a un ritmo mayor que tus "
                    "ventas. Esto deteriora la rotación y consume liquidez."
                ),
                "accion": (
                    "Detén o reduce las compras durante 1-2 meses hasta "
                    "alinear stock con velocidad de venta. Revisa pedidos "
                    "pendientes a proveedores."
                ),
            })
        elif crecimiento_inv < crecimiento_ventas - 15 and crecimiento_ventas > 0:
            recs.append({
                "tipo": "⚠️ Inventario crece menos que ventas",
                "prioridad": "media",
                "titulo": (
                    f"Ventas +{crecimiento_ventas:.1f}% pero inventario "
                    f"{crecimiento_inv:+.1f}%"
                ),
                "detalle": (
                    "Las ventas crecen más que el inventario — buen signo de "
                    "eficiencia, pero atento a posibles desabastos si la "
                    "demanda sigue acelerando."
                ),
                "accion": (
                    "Refuerza compras de los productos con tendencia "
                    "creciente (ver pestaña 'Tendencia ↑' en Compras vs Ventas)."
                ),
            })

    # --- 3. Tendencia mensual de rotación ---
    if monthly_series is not None and len(monthly_series) >= 4:
        ms = monthly_series.dropna(
            subset=["rotacion_anual_mes"]
        ).sort_values("mes")
        if len(ms) >= 4:
            recientes = ms.tail(3)["rotacion_anual_mes"].mean()
            anteriores = ms.head(max(len(ms) - 3, 1))["rotacion_anual_mes"].mean()
            if anteriores > 0:
                delta_rot = (recientes - anteriores) / anteriores * 100
                if delta_rot < -15:
                    recs.append({
                        "tipo": "📉 Rotación deteriorándose",
                        "prioridad": "alta",
                        "titulo": (
                            f"Últimos 3 meses: {recientes:.1f}x vs "
                            f"meses previos: {anteriores:.1f}x "
                            f"({delta_rot:+.0f}%)"
                        ),
                        "detalle": (
                            "La rotación promedio de los últimos 3 meses bajó "
                            "respecto al histórico. Algo está cambiando: "
                            "demanda más lenta, compras infladas o ambas."
                        ),
                        "accion": (
                            "Revisa los meses anómalos en la pestaña "
                            "'Evolución mensual'. Identifica si fue caída de "
                            "ventas o aumento de stock."
                        ),
                    })
                elif delta_rot > 15:
                    recs.append({
                        "tipo": "📈 Rotación mejorando",
                        "prioridad": "baja",
                        "titulo": (
                            f"Últimos 3 meses: {recientes:.1f}x vs "
                            f"meses previos: {anteriores:.1f}x "
                            f"({delta_rot:+.0f}%)"
                        ),
                        "detalle": "La rotación viene mejorando — buena gestión.",
                        "accion": "Mantén el ritmo y documenta qué funcionó.",
                    })

    # --- 4. Top categorías más lentas ---
    if cat_tab is not None and not cat_tab.empty:
        cat_validas = cat_tab[
            (cat_tab["stock_valor"] > 0) & (cat_tab["dias_inventario"].notna())
        ].copy()
        if not cat_validas.empty:
            top_lentas = cat_validas.nlargest(3, "dias_inventario")
            for _, r in top_lentas.iterrows():
                if r["dias_inventario"] > 60:
                    recs.append({
                        "tipo": "🐢 Categoría lenta",
                        "prioridad": "media",
                        "titulo": (
                            f"{r['product_categ_name']}: "
                            f"{r['dias_inventario']:.0f} días de inventario"
                        ),
                        "detalle": (
                            f"Stock valorado en ${r['stock_valor']:,.0f} y "
                            f"rotación de {r['rotacion_anual']:.1f}x. "
                            f"Margen: {r['margen_pct']:.1f}%."
                        ),
                        "accion": (
                            "Revisa surtido de esta categoría. Considera "
                            "promoción o reducción de stock."
                        ),
                    })

            top_rapidas = cat_validas.nsmallest(3, "dias_inventario")
            for _, r in top_rapidas.iterrows():
                if r["dias_inventario"] < 15:
                    recs.append({
                        "tipo": "🚀 Categoría rápida",
                        "prioridad": "media",
                        "titulo": (
                            f"{r['product_categ_name']}: "
                            f"{r['dias_inventario']:.0f} días de cobertura"
                        ),
                        "detalle": (
                            f"Rotación {r['rotacion_anual']:.1f}x al año — "
                            "alta. Riesgo de quiebres."
                        ),
                        "accion": (
                            "Asegura stock de seguridad. Negocia entregas "
                            "más frecuentes con el proveedor."
                        ),
                    })

    # --- 5. Stock muerto y referencias sin venta ---
    if crosstab is not None and not crosstab.empty:
        sin_venta_con_stock = crosstab[
            (crosstab["stock_qty"] > 0)
            & (crosstab["qty_vendida"] <= 0)
        ]
        if not sin_venta_con_stock.empty:
            valor_muerto = float(sin_venta_con_stock["stock_valor"].sum())
            if valor_muerto > 0:
                recs.append({
                    "tipo": "💀 Stock sin venta",
                    "prioridad": "alta" if valor_muerto > saldo_act * 0.1 else "media",
                    "titulo": (
                        f"{len(sin_venta_con_stock):,} referencias con stock "
                        "y sin ventas en el período"
                    ),
                    "detalle": (
                        f"Capital inmovilizado: ${valor_muerto:,.0f} "
                        f"({(valor_muerto / saldo_act * 100) if saldo_act else 0:.1f}% "
                        "del inventario)."
                    ),
                    "accion": (
                        "Revisa la pestaña 'Stock muerto' en Compras vs "
                        "Ventas. Plan: descuento progresivo, bundle con "
                        "productos rápidos, o devolución a proveedor."
                    ),
                })

    # Ordenar por prioridad
    prio_order = {"alta": 0, "media": 1, "baja": 2}
    recs.sort(key=lambda r: prio_order.get(r.get("prioridad", "baja"), 3))
    return recs


# ---------------------------------------------------------------------------
# 8. Rotación por categoría en múltiples ventanas (30/90/180/365 días)
# ---------------------------------------------------------------------------


def compute_rotacion_categoria_multi_ventana(
    sales_lines: pd.DataFrame,
    stock_df: pd.DataFrame,
    today: date | None = None,
    ventanas_dias: list[int] | None = None,
    anualizar: bool = True,
) -> pd.DataFrame:
    """
    Rotación por categoría en múltiples ventanas temporales ancladas a HOY.

    Numerador: ventas (price_subtotal_signed) de la ventana, agrupado por
    categoría de producto.
    Denominador: stock_valor de la categoría (snapshot actual de stock.quant
    valuado a costo promedio cuando no hay valor en quant).

    Si `anualizar=True` (default), cada ventana se anualiza:
      - 30d → ventas/stock × 12
      - 90d → ventas/stock × 365/90
      - 180d → ventas/stock × 365/180
      - 365d → ventas/stock × 1

    Args:
        sales_lines: DataFrame con líneas de facturas de venta (debe cubrir
            al menos la ventana más larga = ventanas_dias[-1]).
        stock_df: stock por producto (qty_available, stock_value).
        today: fecha de hoy (default date.today()).
        ventanas_dias: lista de tamaños de ventana en días. Default
            [30, 90, 180, 365].
        anualizar: si True, multiplica por 365/días_ventana.

    Returns:
        DataFrame con columnas:
          - product_categ_name
          - stock_valor_categoria
          - ventas_<N>d (para cada ventana)
          - rotacion_<N>d (para cada ventana)
    """
    if today is None:
        today = date.today()
    if ventanas_dias is None:
        ventanas_dias = [30, 90, 180, 365]

    sl = _apply_default_exclusions(_normalize_sales_signed(sales_lines))
    if sl is None or sl.empty:
        return pd.DataFrame()

    # Anclar fechas
    if "invoice_date" in sl.columns:
        sl["_d"] = pd.to_datetime(sl["invoice_date"], errors="coerce").dt.date
    else:
        return pd.DataFrame()

    # Fill categoría
    if "product_categ_name" not in sl.columns:
        sl["product_categ_name"] = "(Sin categoría)"
    sl["product_categ_name"] = sl["product_categ_name"].fillna("(Sin categoría)")

    # Calcular ventas por categoría para cada ventana
    rows: dict[str, dict] = {}
    for days in ventanas_dias:
        rng_start = today - timedelta(days=days)
        sl_v = sl[(sl["_d"] >= rng_start) & (sl["_d"] <= today)]
        ventas_cat = sl_v.groupby(
            "product_categ_name", as_index=False
        )["price_subtotal_signed"].sum()
        for _, r in ventas_cat.iterrows():
            cat = r["product_categ_name"]
            rows.setdefault(cat, {"product_categ_name": cat})
            rows[cat][f"ventas_{days}d"] = float(r["price_subtotal_signed"])

    # Stock por categoría (necesita el crosstab para mapear product_id → categoría)
    # Como recibimos solo stock_df (sin categoría), unimos vía sales_lines
    # que sí tiene product_id ↔ product_categ_name
    if stock_df is not None and not stock_df.empty:
        # Map product_id -> product_categ_name (de las líneas de venta)
        prod_cat = (
            sl[["product_id", "product_categ_name"]]
            .drop_duplicates(subset="product_id")
            .dropna(subset=["product_id"])
        )
        stock_join = stock_df.merge(prod_cat, on="product_id", how="left")
        stock_join["product_categ_name"] = stock_join[
            "product_categ_name"
        ].fillna("(Sin categoría)")
        stock_por_cat = stock_join.groupby(
            "product_categ_name", as_index=False
        ).agg(
            stock_qty=("qty_available", "sum"),
            stock_valor=("stock_value", "sum"),
        )
    else:
        stock_por_cat = pd.DataFrame(columns=[
            "product_categ_name", "stock_qty", "stock_valor",
        ])

    # Combinar ventas + stock
    df = pd.DataFrame(list(rows.values()))
    if df.empty:
        return df
    df = df.merge(stock_por_cat, on="product_categ_name", how="left")
    df["stock_valor"] = df["stock_valor"].fillna(0)
    df["stock_qty"] = df["stock_qty"].fillna(0)

    # Asegurar columnas de ventas (0 si no había datos)
    for days in ventanas_dias:
        col = f"ventas_{days}d"
        if col not in df.columns:
            df[col] = 0
        df[col] = df[col].fillna(0)

    # Calcular rotación por ventana
    for days in ventanas_dias:
        ventas_col = f"ventas_{days}d"
        rot_col = f"rotacion_{days}d"
        if anualizar:
            factor = 365.0 / days
            df[rot_col] = np.where(
                df["stock_valor"] > 0,
                df[ventas_col] / df["stock_valor"] * factor,
                0,
            )
        else:
            df[rot_col] = np.where(
                df["stock_valor"] > 0,
                df[ventas_col] / df["stock_valor"],
                0,
            )

    # Renombrar para claridad
    df = df.rename(columns={"stock_valor": "stock_valor_categoria"})

    # Ordenar por rotación del más largo (más estable)
    sort_col = f"rotacion_{ventanas_dias[-1]}d"
    if sort_col in df.columns:
        df = df.sort_values(sort_col, ascending=False).reset_index(drop=True)
    return df


def compute_rotacion_categoria_30d_historica(
    sales_lines: pd.DataFrame,
    stock_df: pd.DataFrame,
    today: date | None = None,
    meses: int = 12,
    anualizar: bool = False,
) -> pd.DataFrame:
    """
    Serie histórica mes a mes de la rotación a 30 días por categoría.

    Para cada mes del histórico se calcula la rotación usando una ventana
    de 30 días que termina el último día de ese mes (para el mes en curso,
    termina HOY):

        rotacion_30d(mes) = ventas[30 días previos al cierre del mes]
                            / stock_valor de la categoría

    El denominador es el stock ACTUAL (snapshot de stock.quant) — igual que
    en compute_rotacion_categoria_multi_ventana. Así, la variación mes a mes
    refleja cambios en la velocidad de venta (numerador).

    Args:
        sales_lines: líneas de facturas de venta (deben cubrir el histórico
            que se quiere graficar; idealmente ≥ 365 días).
        stock_df: stock por producto (qty_available, stock_value).
        today: fecha de referencia (default date.today()).
        meses: cantidad de meses hacia atrás a calcular.
        anualizar: si True multiplica la rotación de 30 días × 12.

    Returns:
        DataFrame en formato largo con columnas:
          - mes (timestamp del primer día del mes)
          - mes_label (str 'YYYY-MM')
          - product_categ_name
          - ventas_30d
          - stock_valor
          - rotacion_30d
    """
    import calendar as _cal

    if today is None:
        today = date.today()

    sl = _apply_default_exclusions(_normalize_sales_signed(sales_lines))
    if sl is None or sl.empty or "invoice_date" not in sl.columns:
        return pd.DataFrame()

    sl["_d"] = pd.to_datetime(sl["invoice_date"], errors="coerce").dt.date
    if "product_categ_name" not in sl.columns:
        sl["product_categ_name"] = "(Sin categoría)"
    sl["product_categ_name"] = sl["product_categ_name"].fillna("(Sin categoría)")

    # ── Construir las fechas de cierre de cada mes (ancla de la ventana) ──
    anchors: list[tuple[pd.Timestamp, date]] = []
    y, m = today.year, today.month
    for _ in range(meses):
        last_day = _cal.monthrange(y, m)[1]
        if y == today.year and m == today.month:
            anchor = today
        else:
            anchor = date(y, m, last_day)
        anchors.append((pd.Timestamp(date(y, m, 1)), anchor))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    anchors.reverse()  # orden cronológico

    # ── Stock por categoría (snapshot actual) ──
    if stock_df is not None and not stock_df.empty:
        prod_cat = (
            sl[["product_id", "product_categ_name"]]
            .drop_duplicates(subset="product_id")
            .dropna(subset=["product_id"])
        )
        stock_join = stock_df.merge(prod_cat, on="product_id", how="left")
        stock_join["product_categ_name"] = stock_join[
            "product_categ_name"
        ].fillna("(Sin categoría)")
        stock_por_cat = stock_join.groupby("product_categ_name")[
            "stock_value"
        ].sum().to_dict()
    else:
        stock_por_cat = {}

    factor = 12.0 if anualizar else 1.0

    # ── Ventas de la ventana de 30 días por cada mes ──
    rows = []
    for mes_ts, anchor in anchors:
        win_start = anchor - timedelta(days=30)
        sl_v = sl[(sl["_d"] > win_start) & (sl["_d"] <= anchor)]
        if sl_v.empty:
            ventas_cat = pd.DataFrame(
                columns=["product_categ_name", "price_subtotal_signed"]
            )
        else:
            ventas_cat = sl_v.groupby(
                "product_categ_name", as_index=False
            )["price_subtotal_signed"].sum()
        for _, r in ventas_cat.iterrows():
            cat = r["product_categ_name"]
            ventas = float(r["price_subtotal_signed"])
            stock_val = float(stock_por_cat.get(cat, 0.0) or 0.0)
            rot = (ventas / stock_val * factor) if stock_val > 0 else 0.0
            rows.append({
                "mes": mes_ts,
                "mes_label": mes_ts.strftime("%Y-%m"),
                "product_categ_name": cat,
                "ventas_30d": ventas,
                "stock_valor": stock_val,
                "rotacion_30d": rot,
            })

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(
        ["product_categ_name", "mes"]
    ).reset_index(drop=True)


def compute_rotacion_30d_historica_consolidada(
    sales_lines: pd.DataFrame,
    denominador: float,
    today: date | None = None,
    meses: int = 12,
    anualizar: bool = False,
) -> pd.DataFrame:
    """
    Serie histórica mes a mes de la rotación a 30 días CONSOLIDADA para
    toda la empresa (sin desglose por categoría).

    Para cada mes se calcula la rotación con una ventana de 30 días que
    termina el último día del mes (para el mes en curso, termina HOY):

        rotacion_30d(mes) = ventas[30 días previos al cierre del mes]
                            / denominador

    El `denominador` es FIJO (típicamente el saldo actual de inventario,
    cuenta 14). Así la variación mes a mes refleja la velocidad de venta.

    Args:
        sales_lines: líneas de facturas de venta (idealmente ≥ 365 días).
        denominador: valor fijo del inventario (saldo cuenta 14).
        today: fecha de referencia (default date.today()).
        meses: cantidad de meses hacia atrás a calcular.
        anualizar: si True multiplica la rotación de 30 días × 12.

    Returns:
        DataFrame con columnas:
          - mes (timestamp del primer día del mes)
          - mes_label (str 'YYYY-MM')
          - ventas_30d
          - rotacion_30d
    """
    import calendar as _cal

    if today is None:
        today = date.today()

    sl = _apply_default_exclusions(_normalize_sales_signed(sales_lines))
    if sl is None or sl.empty or "invoice_date" not in sl.columns:
        return pd.DataFrame()

    sl["_d"] = pd.to_datetime(sl["invoice_date"], errors="coerce").dt.date

    # Fechas de cierre de cada mes (ancla de la ventana de 30 días)
    anchors: list[tuple[pd.Timestamp, date]] = []
    y, m = today.year, today.month
    for _ in range(meses):
        last_day = _cal.monthrange(y, m)[1]
        if y == today.year and m == today.month:
            anchor = today
        else:
            anchor = date(y, m, last_day)
        anchors.append((pd.Timestamp(date(y, m, 1)), anchor))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    anchors.reverse()

    factor = 12.0 if anualizar else 1.0
    denom = float(denominador or 0.0)

    rows = []
    for mes_ts, anchor in anchors:
        win_start = anchor - timedelta(days=30)
        sl_v = sl[(sl["_d"] > win_start) & (sl["_d"] <= anchor)]
        ventas = float(
            sl_v["price_subtotal_signed"].sum()
        ) if not sl_v.empty else 0.0
        rot = (ventas / denom * factor) if denom > 0 else 0.0
        rows.append({
            "mes": mes_ts,
            "mes_label": mes_ts.strftime("%Y-%m"),
            "ventas_30d": ventas,
            "rotacion_30d": rot,
        })

    return pd.DataFrame(rows)
