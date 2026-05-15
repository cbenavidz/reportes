# -*- coding: utf-8 -*-
"""
Motor de análisis financiero para Casa de los Mineros.

Calcula KPIs sobre las líneas de factura ya enriquecidas con:
  - price_subtotal_signed (ventas sin IVA, NC restan)
  - line_cost (costo total = standard_price × quantity × signo)
  - line_margin (margen bruto = subtotal − cost)
  - product_categ_name, product_default_code
  - partner_name, invoice_user_id, invoice_date

KPIs implementados:
  1. Resumen ejecutivo (top KPIs del período)
  2. P&L mensual con margen
  3. Margen por categoría / producto
  4. Pareto 80/20 de clientes y productos
  5. Crecimiento YoY y estacionalidad
  6. Concentración y riesgo (HHI, top N)
  7. Slow movers, churn, ticket promedio, frecuencia compra
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# =============================================================================
# Helpers comunes
# =============================================================================

# Productos a EXCLUIR de los reportes (SOAT, papeles, etc. — no son ventas
# operacionales típicas). Mismo set que sales_analyzer.
EXCLUDED_DEFAULT_CODES = ("SOAT1", "ANTCL")


def _filter_lines(
    lines: pd.DataFrame,
    fecha_desde: date | None = None,
    fecha_hasta: date | None = None,
    exclude_codes: tuple[str, ...] = EXCLUDED_DEFAULT_CODES,
) -> pd.DataFrame:
    """
    Filtra líneas para análisis financiero.
      - Solo líneas tipo product (no secciones/notas)
      - Estado posted
      - Fechas en el rango
      - Excluye productos no operacionales (SOAT, etc.)
    """
    if lines is None or lines.empty:
        return pd.DataFrame()

    df = lines.copy()
    if "display_type" in df.columns:
        df = df[df["display_type"].fillna("product") == "product"]
    if "parent_state" in df.columns:
        df = df[df["parent_state"] == "posted"]
    if "invoice_date" in df.columns:
        df["invoice_date"] = pd.to_datetime(df["invoice_date"], errors="coerce")
        df = df.dropna(subset=["invoice_date"])
        if fecha_desde is not None:
            df = df[df["invoice_date"] >= pd.Timestamp(fecha_desde)]
        if fecha_hasta is not None:
            df = df[df["invoice_date"] <= pd.Timestamp(fecha_hasta)]
    if exclude_codes and "product_default_code" in df.columns:
        df = df[~df["product_default_code"].fillna("").isin(exclude_codes)]
    return df


def _safe_pct(num: float, denom: float) -> float:
    """Porcentaje seguro: 0 si denom es 0."""
    if denom == 0 or pd.isna(denom):
        return 0.0
    return (num / denom) * 100


# =============================================================================
# 1. Resumen ejecutivo
# =============================================================================

def compute_executive_summary(
    lines: pd.DataFrame,
    fecha_desde: date,
    fecha_hasta: date,
) -> dict:
    """
    KPIs del resumen ejecutivo. Calcula período actual + comparativa anterior
    (mismo número de días) para el delta.
    """
    sub = _filter_lines(lines, fecha_desde, fecha_hasta)
    periodo_dias = (fecha_hasta - fecha_desde).days + 1
    fecha_desde_prev = fecha_desde - timedelta(days=periodo_dias)
    fecha_hasta_prev = fecha_desde - timedelta(days=1)
    sub_prev = _filter_lines(lines, fecha_desde_prev, fecha_hasta_prev)

    def _kpis(df):
        if df.empty:
            return {
                "ventas": 0.0, "costo": 0.0, "margen": 0.0, "margen_pct": 0.0,
                "n_facturas": 0, "n_clientes": 0, "n_productos": 0,
                "ticket_promedio": 0.0,
            }
        ventas = float(df["price_subtotal_signed"].sum())
        costo = float(df.get("line_cost", pd.Series([0])).sum())
        margen = float(df.get("line_margin", pd.Series([0])).sum())
        # Solo facturas (no NC) para conteo
        only_inv = df[df.get("move_type", "out_invoice") == "out_invoice"]
        n_facturas = only_inv["move_id"].nunique() if "move_id" in only_inv.columns else 0
        # Clientes con venta neta > 0
        per_partner = df.groupby("partner_id", dropna=True)["price_subtotal_signed"].sum()
        n_clientes = int((per_partner > 0).sum())
        n_productos = int(df["product_id"].dropna().nunique()) if "product_id" in df.columns else 0
        ticket = ventas / n_facturas if n_facturas else 0.0
        return {
            "ventas": ventas,
            "costo": costo,
            "margen": margen,
            "margen_pct": _safe_pct(margen, ventas),
            "n_facturas": n_facturas,
            "n_clientes": n_clientes,
            "n_productos": n_productos,
            "ticket_promedio": ticket,
        }

    actual = _kpis(sub)
    prev = _kpis(sub_prev)

    # Deltas %
    deltas = {}
    for k in actual.keys():
        if prev[k] == 0 or pd.isna(prev[k]):
            deltas[k] = None
        else:
            deltas[k] = ((actual[k] - prev[k]) / prev[k]) * 100

    return {
        "actual": actual,
        "anterior": prev,
        "deltas": deltas,
        "periodo_actual": (fecha_desde, fecha_hasta),
        "periodo_anterior": (fecha_desde_prev, fecha_hasta_prev),
    }


# =============================================================================
# 2. P&L mensual con margen
# =============================================================================

def compute_pnl_monthly(
    lines: pd.DataFrame,
    fecha_desde: date,
    fecha_hasta: date,
) -> pd.DataFrame:
    """
    P&L mensual: ventas, costo, margen bruto, margen %, # facturas.
    Devuelve DataFrame con un mes por fila.
    """
    sub = _filter_lines(lines, fecha_desde, fecha_hasta)
    if sub.empty:
        return pd.DataFrame(columns=[
            "mes", "ventas", "costo", "margen", "margen_pct", "n_facturas",
        ])

    sub = sub.copy()
    sub["mes"] = sub["invoice_date"].dt.to_period("M").dt.to_timestamp()

    only_inv = sub[sub.get("move_type", "out_invoice") == "out_invoice"]
    facturas_por_mes = (
        only_inv.groupby("mes")["move_id"].nunique()
        if "move_id" in only_inv.columns else pd.Series(dtype=int)
    )

    out = sub.groupby("mes", as_index=False).agg(
        ventas=("price_subtotal_signed", "sum"),
        costo=("line_cost", "sum"),
        margen=("line_margin", "sum"),
    )
    out["margen_pct"] = out.apply(
        lambda r: _safe_pct(r["margen"], r["ventas"]), axis=1
    )
    out["n_facturas"] = out["mes"].map(facturas_por_mes).fillna(0).astype(int)
    return out.sort_values("mes").reset_index(drop=True)


# =============================================================================
# 3. Margen por categoría / producto
# =============================================================================

def compute_margin_by_category(
    lines: pd.DataFrame,
    fecha_desde: date,
    fecha_hasta: date,
) -> pd.DataFrame:
    """Ventas, costo, margen por categoría de producto."""
    sub = _filter_lines(lines, fecha_desde, fecha_hasta)
    if sub.empty or "product_categ_name" not in sub.columns:
        return pd.DataFrame()

    out = sub.groupby("product_categ_name", as_index=False).agg(
        ventas=("price_subtotal_signed", "sum"),
        costo=("line_cost", "sum"),
        margen=("line_margin", "sum"),
        unidades=("quantity", "sum"),
        n_facturas=("move_id", "nunique"),
    )
    out["margen_pct"] = out.apply(
        lambda r: _safe_pct(r["margen"], r["ventas"]), axis=1
    )
    out = out.rename(columns={"product_categ_name": "categoria"})
    out = out[out["ventas"] > 0]
    return out.sort_values("margen", ascending=False).reset_index(drop=True)


def compute_top_products_by_margin(
    lines: pd.DataFrame,
    fecha_desde: date,
    fecha_hasta: date,
    n: int = 20,
) -> pd.DataFrame:
    """Top N productos por margen bruto absoluto."""
    sub = _filter_lines(lines, fecha_desde, fecha_hasta)
    if sub.empty:
        return pd.DataFrame()

    out = sub.groupby(["product_id", "product_name"], as_index=False).agg(
        ventas=("price_subtotal_signed", "sum"),
        costo=("line_cost", "sum"),
        margen=("line_margin", "sum"),
        unidades=("quantity", "sum"),
    )
    out["margen_pct"] = out.apply(
        lambda r: _safe_pct(r["margen"], r["ventas"]), axis=1
    )
    out = out[out["ventas"] > 0]
    return out.sort_values("margen", ascending=False).head(n).reset_index(drop=True)


def compute_negative_margin_products(
    lines: pd.DataFrame,
    fecha_desde: date,
    fecha_hasta: date,
) -> pd.DataFrame:
    """
    Productos vendidos REALMENTE por debajo del costo (alerta).

    IMPORTANTE: solo cuenta líneas de FACTURA (out_invoice), NO notas
    crédito. Una NC tiene ventas y costo negativos, lo que genera un
    "margen negativo" falso que distorsiona el reporte.

    Filtro: solo productos con
      - ventas netas POSITIVAS (se vendieron de verdad)
      - margen NEGATIVO (el costo superó el precio de venta)
      - al menos 1 unidad vendida
    """
    sub = _filter_lines(lines, fecha_desde, fecha_hasta)
    if sub.empty:
        return pd.DataFrame()

    # Excluir notas crédito — solo facturas de venta reales
    if "move_type" in sub.columns:
        sub = sub[sub["move_type"] == "out_invoice"]
    if sub.empty:
        return pd.DataFrame()

    out = sub.groupby(["product_id", "product_name"], as_index=False).agg(
        ventas=("price_subtotal_signed", "sum"),
        costo=("line_cost", "sum"),
        margen=("line_margin", "sum"),
        unidades=("quantity", "sum"),
    )
    out["margen_pct"] = out.apply(
        lambda r: _safe_pct(r["margen"], r["ventas"]), axis=1
    )
    # Solo productos con ventas positivas Y margen negativo Y unidades > 0
    out = out[
        (out["ventas"] > 0)
        & (out["margen"] < 0)
        & (out["unidades"] > 0)
    ]
    return out.sort_values("margen").reset_index(drop=True)


# =============================================================================
# 4. Pareto 80/20
# =============================================================================

def compute_pareto_clientes(
    lines: pd.DataFrame,
    fecha_desde: date,
    fecha_hasta: date,
) -> pd.DataFrame:
    """
    Pareto de clientes. Devuelve DataFrame con cumulative % y bucket A/B/C.
    Bucket A: 0-80% acumulado · B: 80-95% · C: 95-100%
    """
    sub = _filter_lines(lines, fecha_desde, fecha_hasta)
    if sub.empty:
        return pd.DataFrame()

    out = sub.groupby(["partner_id", "partner_name"], as_index=False).agg(
        ventas=("price_subtotal_signed", "sum"),
        margen=("line_margin", "sum"),
        n_facturas=("move_id", "nunique"),
    )
    out = out[out["ventas"] > 0].sort_values("ventas", ascending=False).reset_index(drop=True)
    if out.empty:
        return out

    total = out["ventas"].sum()
    out["pct"] = out["ventas"] / total * 100
    out["pct_acumulado"] = out["pct"].cumsum()
    out["rank"] = range(1, len(out) + 1)
    out["bucket"] = pd.cut(
        out["pct_acumulado"], bins=[0, 80, 95, 100],
        labels=["A", "B", "C"], include_lowest=True,
    )
    return out


def compute_pareto_productos(
    lines: pd.DataFrame,
    fecha_desde: date,
    fecha_hasta: date,
) -> pd.DataFrame:
    """Pareto de productos."""
    sub = _filter_lines(lines, fecha_desde, fecha_hasta)
    if sub.empty:
        return pd.DataFrame()

    out = sub.groupby(["product_id", "product_name"], as_index=False).agg(
        ventas=("price_subtotal_signed", "sum"),
        margen=("line_margin", "sum"),
        unidades=("quantity", "sum"),
    )
    out = out[out["ventas"] > 0].sort_values("ventas", ascending=False).reset_index(drop=True)
    if out.empty:
        return out

    total = out["ventas"].sum()
    out["pct"] = out["ventas"] / total * 100
    out["pct_acumulado"] = out["pct"].cumsum()
    out["rank"] = range(1, len(out) + 1)
    out["bucket"] = pd.cut(
        out["pct_acumulado"], bins=[0, 80, 95, 100],
        labels=["A", "B", "C"], include_lowest=True,
    )
    return out


# =============================================================================
# 5. Concentración y riesgo
# =============================================================================

def compute_concentration_risk(
    lines: pd.DataFrame,
    fecha_desde: date,
    fecha_hasta: date,
) -> dict:
    """
    Métricas de concentración:
      - HHI (Herfindahl-Hirschman Index): suma de cuadrados de % participación
      - % de ventas en top 5, top 10, top 20 clientes
      - "Riesgo de pérdida": cuánto pesan los top 3 clientes (si los pierdes...)
    """
    pareto = compute_pareto_clientes(lines, fecha_desde, fecha_hasta)
    if pareto.empty:
        return {"hhi": 0, "top5_pct": 0, "top10_pct": 0, "top20_pct": 0,
                "riesgo_top3": 0, "n_clientes_a": 0, "n_clientes_total": 0}

    # HHI: sum((pct_share)²) — escalado 0-10000
    hhi = float((pareto["pct"] ** 2).sum())
    n_total = len(pareto)
    return {
        "hhi": hhi,
        "top5_pct": float(pareto.head(5)["pct"].sum()) if n_total >= 5 else float(pareto["pct"].sum()),
        "top10_pct": float(pareto.head(10)["pct"].sum()) if n_total >= 10 else float(pareto["pct"].sum()),
        "top20_pct": float(pareto.head(20)["pct"].sum()) if n_total >= 20 else float(pareto["pct"].sum()),
        "riesgo_top3": float(pareto.head(3)["pct"].sum()),
        "n_clientes_a": int((pareto["bucket"] == "A").sum()),
        "n_clientes_total": n_total,
    }


# =============================================================================
# 6. Crecimiento YoY y estacionalidad
# =============================================================================

def compute_yoy_growth(lines: pd.DataFrame, n_meses: int = 24) -> pd.DataFrame:
    """
    Tendencia mensual de ventas para los últimos n_meses meses.
    Incluye columnas: mes, ventas, ventas_yoy (mismo mes año anterior),
    crecimiento_yoy_pct.
    """
    if lines is None or lines.empty:
        return pd.DataFrame()

    sub = _filter_lines(lines)
    if sub.empty or "invoice_date" not in sub.columns:
        return pd.DataFrame()

    sub = sub.copy()
    sub["mes"] = sub["invoice_date"].dt.to_period("M").dt.to_timestamp()
    monthly = sub.groupby("mes", as_index=False)["price_subtotal_signed"].sum()
    monthly = monthly.rename(columns={"price_subtotal_signed": "ventas"})

    # Para cada mes, buscar el mismo mes del año anterior
    monthly["mes_yoy"] = monthly["mes"] - pd.DateOffset(years=1)
    yoy = monthly.set_index("mes")["ventas"]
    monthly["ventas_yoy"] = monthly["mes_yoy"].map(yoy).astype(float)
    monthly["crecimiento_yoy_pct"] = monthly.apply(
        lambda r: _safe_pct(r["ventas"] - r["ventas_yoy"], r["ventas_yoy"])
        if pd.notna(r["ventas_yoy"]) else None,
        axis=1,
    )
    return monthly.sort_values("mes").tail(n_meses).reset_index(drop=True)


def compute_seasonality_heatmap(lines: pd.DataFrame) -> pd.DataFrame:
    """
    Heatmap mes vs año. Devuelve DataFrame en formato wide:
        index = año, columns = mes (1-12), values = ventas
    """
    sub = _filter_lines(lines)
    if sub.empty or "invoice_date" not in sub.columns:
        return pd.DataFrame()
    sub = sub.copy()
    sub["año"] = sub["invoice_date"].dt.year
    sub["mes"] = sub["invoice_date"].dt.month
    pivot = sub.pivot_table(
        index="año", columns="mes", values="price_subtotal_signed",
        aggfunc="sum", fill_value=0,
    )
    return pivot


# =============================================================================
# 7. Eficiencia: slow movers, churn, ticket
# =============================================================================

def compute_slow_movers(
    lines: pd.DataFrame,
    fecha_corte: date,
    days_threshold: int = 90,
) -> pd.DataFrame:
    """
    Productos sin ventas en los últimos `days_threshold` días.
    Útil para identificar inventario muerto / liquidación.
    """
    if lines is None or lines.empty:
        return pd.DataFrame()

    sub = _filter_lines(lines)
    if sub.empty:
        return pd.DataFrame()

    cutoff = pd.Timestamp(fecha_corte) - pd.Timedelta(days=days_threshold)

    last_sales = sub.groupby(["product_id", "product_name"], as_index=False).agg(
        ultima_venta=("invoice_date", "max"),
        ventas_total=("price_subtotal_signed", "sum"),
        unidades_total=("quantity", "sum"),
    )
    slow = last_sales[last_sales["ultima_venta"] < cutoff].copy()
    slow["dias_sin_vender"] = (
        pd.Timestamp(fecha_corte) - slow["ultima_venta"]
    ).dt.days
    slow = slow[slow["ventas_total"] > 0]  # solo productos que sí se vendieron alguna vez
    return slow.sort_values("dias_sin_vender", ascending=False).reset_index(drop=True)


def compute_churn_clientes(
    lines: pd.DataFrame,
    fecha_corte: date,
    days_threshold: int = 60,
) -> pd.DataFrame:
    """
    Clientes que no han comprado en los últimos `days_threshold` días pero
    sí compraban antes. Útil para identificar churn / clientes a recuperar.
    """
    if lines is None or lines.empty:
        return pd.DataFrame()

    sub = _filter_lines(lines)
    if sub.empty:
        return pd.DataFrame()

    cutoff = pd.Timestamp(fecha_corte) - pd.Timedelta(days=days_threshold)

    by_client = sub.groupby(["partner_id", "partner_name"], as_index=False).agg(
        ultima_compra=("invoice_date", "max"),
        primera_compra=("invoice_date", "min"),
        ventas_historicas=("price_subtotal_signed", "sum"),
        n_compras=("move_id", "nunique"),
    )
    by_client = by_client[by_client["ventas_historicas"] > 0]
    inactive = by_client[by_client["ultima_compra"] < cutoff].copy()
    inactive["dias_inactivo"] = (
        pd.Timestamp(fecha_corte) - inactive["ultima_compra"]
    ).dt.days
    return inactive.sort_values("ventas_historicas", ascending=False).reset_index(drop=True)


def compute_purchase_frequency(
    lines: pd.DataFrame,
    fecha_desde: date,
    fecha_hasta: date,
) -> dict:
    """Métricas de frecuencia de compra agregadas."""
    sub = _filter_lines(lines, fecha_desde, fecha_hasta)
    if sub.empty:
        return {"frecuencia_promedio_dias": 0, "compras_por_cliente": 0,
                "ticket_promedio": 0, "n_clientes_recurrentes": 0}

    only_inv = sub[sub.get("move_type", "out_invoice") == "out_invoice"]
    if only_inv.empty:
        return {"frecuencia_promedio_dias": 0, "compras_por_cliente": 0,
                "ticket_promedio": 0, "n_clientes_recurrentes": 0}

    facts_per_client = only_inv.groupby("partner_id")["move_id"].nunique()
    n_clientes_recurrentes = int((facts_per_client >= 2).sum())

    # Frecuencia: días promedio entre compras de cada cliente
    by_client_dates = only_inv.groupby("partner_id")["invoice_date"].agg(
        primera="min", ultima="max", n_compras="nunique",
    )
    by_client_dates = by_client_dates[by_client_dates["n_compras"] >= 2]
    if len(by_client_dates) > 0:
        by_client_dates["dias_total"] = (
            by_client_dates["ultima"] - by_client_dates["primera"]
        ).dt.days
        by_client_dates["frecuencia_dias"] = (
            by_client_dates["dias_total"] / (by_client_dates["n_compras"] - 1)
        )
        frecuencia_promedio = float(by_client_dates["frecuencia_dias"].median())
    else:
        frecuencia_promedio = 0

    compras_promedio = float(facts_per_client.mean()) if len(facts_per_client) else 0
    ticket = float(sub["price_subtotal_signed"].sum() / only_inv["move_id"].nunique()) \
        if only_inv["move_id"].nunique() else 0

    return {
        "frecuencia_promedio_dias": frecuencia_promedio,
        "compras_por_cliente": compras_promedio,
        "ticket_promedio": ticket,
        "n_clientes_recurrentes": n_clientes_recurrentes,
    }
