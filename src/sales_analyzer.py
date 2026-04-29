# -*- coding: utf-8 -*-
"""
Análisis de ventas a partir de account.move (facturas + notas crédito).

⚠️  IMPORTANTE — fecha utilizada:
    Todas las funciones usan `invoice_date` (fecha de FACTURACIÓN), NO
    `date_order` (fecha de la orden de venta). Razones:
      - `date_order` vive en `sale.order` y no en `account.move`.
      - El reporte refleja ventas REALMENTE FACTURADAS en el período, no
        órdenes pendientes ni compromisos comerciales.
      - Si una orden se hace en marzo y se factura en abril, esa venta
        cuenta para abril (cuando se reconoció el ingreso).

⚠️  Notas crédito:
    Se incluyen `out_refund` con signo negativo (Odoo guarda
    `amount_total_signed` ya con signo correcto). Sumar la columna netea
    automáticamente: ventas brutas − notas crédito = ventas netas.

⚠️  Mezcla contado/crédito:
    Por defecto se mezcla todo (ventas totales). No filtramos por
    `payment_term_name` ni por settlement.

Funciones públicas:
    - filter_sales_invoices(invoices, date_from, date_to)
    - compute_sales_kpis(invoices, ...)
    - compute_sales_monthly(invoices, ...)
    - compute_sales_by_vendedor(invoices, partners, ...)
    - compute_sales_by_partner(invoices, partners, ...)
    - compute_sales_by_product(invoice_lines, ...)  [requiere extracción de líneas]
    - compute_sales_growth(invoices, ...)  [comparativo período actual vs anterior]
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constantes / helpers
# ---------------------------------------------------------------------------

# `move_type` que cuentan como "venta" en account.move:
#   - out_invoice : factura de cliente (suma)
#   - out_refund  : nota crédito de cliente (resta — viene con signo negativo)
SALES_MOVE_TYPES = ("out_invoice", "out_refund")

# Estados que consideramos "venta confirmada":
#   - posted : asentada en contabilidad
# Las facturas en `draft` o `cancel` NO cuentan.
SALES_VALID_STATES = ("posted",)


def _ensure_datetime(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce")


def _safe_div(num: float, den: float) -> float:
    return float(num) / float(den) if den else 0.0


# ---------------------------------------------------------------------------
# Filtro base — la única puerta de entrada al universo "ventas"
# ---------------------------------------------------------------------------

def filter_sales_invoices(
    invoices: pd.DataFrame,
    date_from: date | pd.Timestamp | None = None,
    date_to: date | pd.Timestamp | None = None,
    company_ids: Iterable[int] | None = None,
) -> pd.DataFrame:
    """
    Devuelve el subconjunto de `invoices` que cuenta como "venta" en el
    período indicado. Filtros aplicados (en orden):

      1) `move_type` ∈ {out_invoice, out_refund}
      2) `state` ∈ {posted}
      3) `invoice_date` ∈ [date_from, date_to]   ← FECHA DE FACTURACIÓN
      4) `company_id` ∈ company_ids (si se pasa)

    El DataFrame devuelto conserva todas las columnas originales más una
    columna `invoice_date_dt` (datetime normalizado).
    """
    if invoices is None or invoices.empty:
        return invoices.iloc[0:0] if invoices is not None else pd.DataFrame()

    df = invoices.copy()

    # 1) move_type
    if "move_type" in df.columns:
        df = df[df["move_type"].isin(SALES_MOVE_TYPES)]

    # 2) state
    if "state" in df.columns:
        df = df[df["state"].isin(SALES_VALID_STATES)]

    # 3) invoice_date (NO date_order — ese campo no existe en account.move)
    df["invoice_date_dt"] = _ensure_datetime(df["invoice_date"])
    df = df.dropna(subset=["invoice_date_dt"])

    if date_from is not None:
        df = df[df["invoice_date_dt"] >= pd.Timestamp(date_from)]
    if date_to is not None:
        df = df[df["invoice_date_dt"] <= pd.Timestamp(date_to)]

    # 4) empresa
    if company_ids is not None and "company_id" in df.columns:
        cset = set(int(c) for c in company_ids)
        df = df[df["company_id"].isin(cset)]

    return df.reset_index(drop=True)


# ---------------------------------------------------------------------------
# KPIs principales
# ---------------------------------------------------------------------------

@dataclass
class SalesKPIs:
    ventas_netas: float            # suma de amount_total_signed (NC restan)
    ventas_brutas: float           # solo out_invoice
    notas_credito: float           # |suma de out_refund|
    n_facturas: int                # count out_invoice (sin NC)
    n_notas_credito: int           # count out_refund
    ticket_promedio: float         # ventas_brutas / n_facturas
    n_clientes_unicos: int         # partner_id distintos
    fecha_desde: pd.Timestamp | None
    fecha_hasta: pd.Timestamp | None

    def to_dict(self) -> dict:
        return {
            "ventas_netas": self.ventas_netas,
            "ventas_brutas": self.ventas_brutas,
            "notas_credito": self.notas_credito,
            "n_facturas": self.n_facturas,
            "n_notas_credito": self.n_notas_credito,
            "ticket_promedio": self.ticket_promedio,
            "n_clientes_unicos": self.n_clientes_unicos,
            "fecha_desde": self.fecha_desde,
            "fecha_hasta": self.fecha_hasta,
        }


def compute_sales_kpis(
    invoices: pd.DataFrame,
    date_from: date | pd.Timestamp | None = None,
    date_to: date | pd.Timestamp | None = None,
    company_ids: Iterable[int] | None = None,
) -> SalesKPIs:
    """
    KPIs base del informe de ventas. Usa `invoice_date` para el filtro
    temporal. NC restan automáticamente vía `amount_total_signed`.
    """
    df = filter_sales_invoices(invoices, date_from, date_to, company_ids)
    if df.empty:
        return SalesKPIs(0.0, 0.0, 0.0, 0, 0, 0.0, 0,
                         pd.Timestamp(date_from) if date_from else None,
                         pd.Timestamp(date_to) if date_to else None)

    fac = df[df["move_type"] == "out_invoice"]
    nc = df[df["move_type"] == "out_refund"]

    ventas_brutas = float(fac["amount_total_signed"].sum())
    nc_total = float(nc["amount_total_signed"].sum())   # ya negativo
    ventas_netas = ventas_brutas + nc_total              # netea
    n_fac = int(len(fac))
    n_nc = int(len(nc))
    ticket = _safe_div(ventas_brutas, n_fac)
    n_clientes = int(df["partner_id"].nunique()) if "partner_id" in df.columns else 0

    return SalesKPIs(
        ventas_netas=ventas_netas,
        ventas_brutas=ventas_brutas,
        notas_credito=abs(nc_total),
        n_facturas=n_fac,
        n_notas_credito=n_nc,
        ticket_promedio=ticket,
        n_clientes_unicos=n_clientes,
        fecha_desde=df["invoice_date_dt"].min(),
        fecha_hasta=df["invoice_date_dt"].max(),
    )


# ---------------------------------------------------------------------------
# Tendencia mensual (con comparativo vs período anterior)
# ---------------------------------------------------------------------------

def compute_sales_monthly(
    invoices: pd.DataFrame,
    months: int = 12,
    cutoff_date: date | None = None,
    company_ids: Iterable[int] | None = None,
) -> pd.DataFrame:
    """
    Serie mensual de ventas (últimos `months` meses hasta `cutoff_date`).

    Columnas devueltas:
      - mes              : Period[M]
      - mes_label        : str "YYYY-MM"
      - ventas_netas     : float (out_invoice + out_refund con signo)
      - ventas_brutas    : float (solo out_invoice)
      - notas_credito    : float (|out_refund|)
      - n_facturas       : int
      - ticket_promedio  : float
      - var_mom          : float (variación % vs mes anterior; NaN en el primero)
      - var_yoy          : float (variación % vs mismo mes año anterior; NaN si falta)

    Usa `invoice_date` para asignar el mes.
    """
    if cutoff_date is None:
        cutoff_date = date.today()
    cutoff_ts = pd.Timestamp(cutoff_date)
    # Usamos último día del mes del cutoff para incluir el mes corriente entero
    end_period = cutoff_ts.to_period("M").to_timestamp(how="end")
    # `months` hacia atrás + buffer de 12 meses para poder calcular YoY
    buffer_months = months + 12
    start_period = (end_period.to_period("M") - (buffer_months - 1)).to_timestamp(how="start")

    df = filter_sales_invoices(
        invoices,
        date_from=start_period,
        date_to=end_period,
        company_ids=company_ids,
    )

    # Construimos un índice mensual completo (aunque haya meses sin ventas)
    full_index = pd.period_range(start=start_period, end=end_period, freq="M")
    base = pd.DataFrame(index=full_index)
    base.index.name = "mes"

    if df.empty:
        out = base.assign(
            ventas_netas=0.0, ventas_brutas=0.0, notas_credito=0.0,
            n_facturas=0, ticket_promedio=0.0,
        ).reset_index()
    else:
        df["mes"] = df["invoice_date_dt"].dt.to_period("M")
        is_fac = df["move_type"] == "out_invoice"
        is_nc = df["move_type"] == "out_refund"

        agg = pd.DataFrame({
            "ventas_netas": df.groupby("mes")["amount_total_signed"].sum(),
            "ventas_brutas": df.loc[is_fac].groupby("mes")["amount_total_signed"].sum(),
            "nc_signed": df.loc[is_nc].groupby("mes")["amount_total_signed"].sum(),
            "n_facturas": df.loc[is_fac].groupby("mes").size(),
        })
        agg = base.join(agg, how="left").fillna(0.0)
        agg["notas_credito"] = agg["nc_signed"].abs()
        agg["n_facturas"] = agg["n_facturas"].astype(int)
        agg["ticket_promedio"] = np.where(
            agg["n_facturas"] > 0,
            agg["ventas_brutas"] / agg["n_facturas"].replace(0, np.nan),
            0.0,
        )
        agg = agg.drop(columns=["nc_signed"])
        out = agg.reset_index()

    out["mes_label"] = out["mes"].astype(str)

    # Variaciones — si el denominador (mes anterior) es 0, devolvemos NaN
    # en vez de ±inf para que la UI muestre "—" en vez de "inf%".
    out = out.sort_values("mes").reset_index(drop=True)
    prev_month = out["ventas_netas"].shift(1).replace(0, np.nan)
    out["var_mom"] = (out["ventas_netas"] / prev_month - 1) * 100
    prev_year = out["ventas_netas"].shift(12).replace(0, np.nan)
    out["var_yoy"] = (out["ventas_netas"] / prev_year - 1) * 100

    # Recortamos al rango pedido (los `buffer_months` extra eran solo para YoY)
    out = out.tail(months).reset_index(drop=True)
    return out[["mes", "mes_label", "ventas_netas", "ventas_brutas",
                "notas_credito", "n_facturas", "ticket_promedio",
                "var_mom", "var_yoy"]]


# ---------------------------------------------------------------------------
# Comparativo período actual vs período anterior (mismo largo)
# ---------------------------------------------------------------------------

def compute_sales_growth(
    invoices: pd.DataFrame,
    date_from: date | pd.Timestamp,
    date_to: date | pd.Timestamp,
    company_ids: Iterable[int] | None = None,
) -> dict:
    """
    Compara ventas del período [date_from, date_to] vs período inmediatamente
    anterior de igual duración.

    Ej: si date_from=2026-04-01, date_to=2026-04-30, compara abril vs marzo.

    Devuelve dict con:
      - actual: SalesKPIs del período actual
      - anterior: SalesKPIs del período anterior
      - var_ventas_pct: variación % en ventas_netas
      - var_facturas_pct: variación % en n_facturas
      - var_ticket_pct: variación % en ticket_promedio
      - delta_ventas_abs: ventas_netas actual − anterior
    """
    actual_from = pd.Timestamp(date_from)
    actual_to = pd.Timestamp(date_to)
    duration = actual_to - actual_from

    prev_to = actual_from - timedelta(days=1)
    prev_from = prev_to - duration

    actual = compute_sales_kpis(invoices, actual_from, actual_to, company_ids)
    anterior = compute_sales_kpis(invoices, prev_from, prev_to, company_ids)

    var_ventas = (
        (actual.ventas_netas / anterior.ventas_netas - 1) * 100
        if anterior.ventas_netas else np.nan
    )
    var_fact = (
        (actual.n_facturas / anterior.n_facturas - 1) * 100
        if anterior.n_facturas else np.nan
    )
    var_ticket = (
        (actual.ticket_promedio / anterior.ticket_promedio - 1) * 100
        if anterior.ticket_promedio else np.nan
    )

    return {
        "actual": actual,
        "anterior": anterior,
        "actual_periodo": (actual_from, actual_to),
        "anterior_periodo": (prev_from, prev_to),
        "var_ventas_pct": var_ventas,
        "var_facturas_pct": var_fact,
        "var_ticket_pct": var_ticket,
        "delta_ventas_abs": actual.ventas_netas - anterior.ventas_netas,
    }


# ---------------------------------------------------------------------------
# Por dimensión: vendedor
# ---------------------------------------------------------------------------

def _resolve_vendedor_id(invoices: pd.DataFrame) -> pd.Series:
    """
    Resuelve el ID del vendedor preferiendo `invoice_user_id` (vendedor de
    la factura) sobre `user_id` (responsable). Devuelve NaN si ninguno.
    """
    if "invoice_user_id" in invoices.columns:
        v = pd.to_numeric(invoices["invoice_user_id"], errors="coerce")
    else:
        v = pd.Series([np.nan] * len(invoices), index=invoices.index)
    if "user_id" in invoices.columns:
        fallback = pd.to_numeric(invoices["user_id"], errors="coerce")
        v = v.fillna(fallback)
    return v


def compute_sales_by_vendedor(
    invoices: pd.DataFrame,
    date_from: date | pd.Timestamp | None = None,
    date_to: date | pd.Timestamp | None = None,
    company_ids: Iterable[int] | None = None,
    vendedor_names: dict[int, str] | None = None,
) -> pd.DataFrame:
    """
    Ventas agrupadas por vendedor (preferimos `invoice_user_id`).

    Columnas:
      - vendedor_id, vendedor_nombre
      - ventas_netas, ventas_brutas, notas_credito
      - n_facturas, ticket_promedio
      - n_clientes
      - participacion_pct (sobre ventas_netas totales del período)
    """
    df = filter_sales_invoices(invoices, date_from, date_to, company_ids)
    if df.empty:
        return pd.DataFrame(columns=[
            "vendedor_id", "vendedor_nombre", "ventas_netas", "ventas_brutas",
            "notas_credito", "n_facturas", "ticket_promedio", "n_clientes",
            "participacion_pct",
        ])

    df = df.copy()
    df["vendedor_id"] = _resolve_vendedor_id(df)
    df["vendedor_id"] = df["vendedor_id"].fillna(-1).astype(int)

    is_fac = df["move_type"] == "out_invoice"
    is_nc = df["move_type"] == "out_refund"

    # Agregaciones
    grp = df.groupby("vendedor_id")
    res = pd.DataFrame({
        "ventas_netas": grp["amount_total_signed"].sum(),
        "ventas_brutas": df.loc[is_fac].groupby("vendedor_id")["amount_total_signed"].sum(),
        "nc_signed": df.loc[is_nc].groupby("vendedor_id")["amount_total_signed"].sum(),
        "n_facturas": df.loc[is_fac].groupby("vendedor_id").size(),
        "n_clientes": grp["partner_id"].nunique(),
    }).fillna(0.0)
    res["notas_credito"] = res["nc_signed"].abs()
    res["n_facturas"] = res["n_facturas"].astype(int)
    res["n_clientes"] = res["n_clientes"].astype(int)
    res["ticket_promedio"] = np.where(
        res["n_facturas"] > 0,
        res["ventas_brutas"] / res["n_facturas"].replace(0, np.nan),
        0.0,
    )

    total_neto = float(res["ventas_netas"].sum())
    res["participacion_pct"] = (
        res["ventas_netas"] / total_neto * 100 if total_neto else 0.0
    )

    res = res.reset_index()
    # Nombres
    if vendedor_names:
        res["vendedor_nombre"] = res["vendedor_id"].map(vendedor_names).fillna("Sin vendedor")
    else:
        res["vendedor_nombre"] = res["vendedor_id"].astype(str)
    res.loc[res["vendedor_id"] == -1, "vendedor_nombre"] = "Sin vendedor"

    res = res.drop(columns=["nc_signed"])
    return res.sort_values("ventas_netas", ascending=False).reset_index(drop=True)[[
        "vendedor_id", "vendedor_nombre", "ventas_netas", "ventas_brutas",
        "notas_credito", "n_facturas", "ticket_promedio", "n_clientes",
        "participacion_pct",
    ]]


# ---------------------------------------------------------------------------
# Por dimensión: cliente (Pareto)
# ---------------------------------------------------------------------------

def compute_sales_by_partner(
    invoices: pd.DataFrame,
    date_from: date | pd.Timestamp | None = None,
    date_to: date | pd.Timestamp | None = None,
    company_ids: Iterable[int] | None = None,
    partner_names: dict[int, str] | None = None,
    top_n: int | None = None,
) -> pd.DataFrame:
    """
    Ventas por cliente con Pareto acumulado.

    Columnas:
      - partner_id, partner_nombre
      - ventas_netas, ventas_brutas, notas_credito
      - n_facturas, ticket_promedio
      - participacion_pct, participacion_acum_pct
      - es_pareto_80 (True si entra en el 80% de las ventas)

    Si `top_n` se especifica, recorta al top N clientes por ventas_netas.
    """
    df = filter_sales_invoices(invoices, date_from, date_to, company_ids)
    if df.empty:
        return pd.DataFrame(columns=[
            "partner_id", "partner_nombre", "ventas_netas", "ventas_brutas",
            "notas_credito", "n_facturas", "ticket_promedio",
            "participacion_pct", "participacion_acum_pct", "es_pareto_80",
        ])

    df = df.copy()
    df["partner_id"] = pd.to_numeric(df["partner_id"], errors="coerce").fillna(-1).astype(int)
    is_fac = df["move_type"] == "out_invoice"
    is_nc = df["move_type"] == "out_refund"

    grp = df.groupby("partner_id")
    res = pd.DataFrame({
        "ventas_netas": grp["amount_total_signed"].sum(),
        "ventas_brutas": df.loc[is_fac].groupby("partner_id")["amount_total_signed"].sum(),
        "nc_signed": df.loc[is_nc].groupby("partner_id")["amount_total_signed"].sum(),
        "n_facturas": df.loc[is_fac].groupby("partner_id").size(),
    }).fillna(0.0)
    res["notas_credito"] = res["nc_signed"].abs()
    res["n_facturas"] = res["n_facturas"].astype(int)
    res["ticket_promedio"] = np.where(
        res["n_facturas"] > 0,
        res["ventas_brutas"] / res["n_facturas"].replace(0, np.nan),
        0.0,
    )

    res = res.reset_index().sort_values("ventas_netas", ascending=False).reset_index(drop=True)

    # Nombres
    if partner_names is None and "partner_name" in df.columns:
        partner_names = (
            df.dropna(subset=["partner_name"])
              .drop_duplicates("partner_id")
              .set_index("partner_id")["partner_name"]
              .to_dict()
        )
    if partner_names:
        res["partner_nombre"] = res["partner_id"].map(partner_names).fillna("Sin nombre")
    else:
        res["partner_nombre"] = res["partner_id"].astype(str)

    total = float(res["ventas_netas"].sum())
    res["participacion_pct"] = (res["ventas_netas"] / total * 100) if total else 0.0
    res["participacion_acum_pct"] = res["participacion_pct"].cumsum()
    res["es_pareto_80"] = res["participacion_acum_pct"] <= 80.0

    res = res.drop(columns=["nc_signed"])

    if top_n is not None:
        res = res.head(top_n)

    return res[[
        "partner_id", "partner_nombre", "ventas_netas", "ventas_brutas",
        "notas_credito", "n_facturas", "ticket_promedio",
        "participacion_pct", "participacion_acum_pct", "es_pareto_80",
    ]].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Por dimensión: producto / categoría  (requiere account.move.line)
# ---------------------------------------------------------------------------

def compute_sales_by_product(
    invoice_lines: pd.DataFrame,
    invoices: pd.DataFrame | None = None,
    date_from: date | pd.Timestamp | None = None,
    date_to: date | pd.Timestamp | None = None,
    company_ids: Iterable[int] | None = None,
    group_by: str = "product",   # "product" o "category"
    top_n: int | None = None,
) -> pd.DataFrame:
    """
    Ventas agrupadas por producto o por categoría a partir de las líneas
    de factura (account.move.line).

    `invoice_lines` debe traer al menos:
      - move_id, partner_id, product_id, product_name
      - product_categ_id, product_categ_name
      - quantity
      - price_subtotal_signed   (con signo, las NC restan)
      - move_type (heredado)
      - state, invoice_date (heredado del move)

    Si `invoices` se pasa, se usa para anclar el filtro al período sobre
    `invoice_date` real del move (para evitar inconsistencias).

    `group_by`:
      - "product"  → product_id, product_name
      - "category" → product_categ_id, product_categ_name

    Devuelve columnas:
      - <id>, <nombre>
      - cantidad
      - ventas_netas (sumatoria con signo)
      - n_facturas (move_id distintos)
      - participacion_pct
    """
    if invoice_lines is None or invoice_lines.empty:
        cols = (
            ["product_id", "product_nombre"]
            if group_by == "product"
            else ["product_categ_id", "categoria_nombre"]
        )
        return pd.DataFrame(columns=cols + [
            "cantidad", "ventas_netas", "n_facturas", "participacion_pct"
        ])

    df = invoice_lines.copy()

    # Filtro por move_type / state (deben venir heredados del move)
    if "move_type" in df.columns:
        df = df[df["move_type"].isin(SALES_MOVE_TYPES)]
    if "state" in df.columns:
        df = df[df["state"].isin(SALES_VALID_STATES)]

    # Filtro temporal: si las líneas traen invoice_date, lo usamos.
    # Si NO, hacemos lookup contra `invoices`.
    if "invoice_date" in df.columns:
        df["invoice_date_dt"] = _ensure_datetime(df["invoice_date"])
    elif invoices is not None and "id" in invoices.columns and "invoice_date" in invoices.columns:
        date_map = (
            invoices.assign(_d=_ensure_datetime(invoices["invoice_date"]))
                    .drop_duplicates("id")
                    .set_index("id")["_d"]
        )
        df["invoice_date_dt"] = pd.to_numeric(df["move_id"], errors="coerce").map(date_map)
    else:
        df["invoice_date_dt"] = pd.NaT

    df = df.dropna(subset=["invoice_date_dt"])
    if date_from is not None:
        df = df[df["invoice_date_dt"] >= pd.Timestamp(date_from)]
    if date_to is not None:
        df = df[df["invoice_date_dt"] <= pd.Timestamp(date_to)]

    if company_ids is not None and "company_id" in df.columns:
        df = df[df["company_id"].isin(set(int(c) for c in company_ids))]

    if df.empty:
        cols = (
            ["product_id", "product_nombre"]
            if group_by == "product"
            else ["product_categ_id", "categoria_nombre"]
        )
        return pd.DataFrame(columns=cols + [
            "cantidad", "ventas_netas", "n_facturas", "participacion_pct"
        ])

    if group_by == "product":
        id_col = "product_id"
        name_col = "product_name"
        out_id = "product_id"
        out_name = "product_nombre"
    else:
        id_col = "product_categ_id"
        name_col = "product_categ_name"
        out_id = "product_categ_id"
        out_name = "categoria_nombre"

    df[id_col] = pd.to_numeric(df[id_col], errors="coerce").fillna(-1).astype(int)
    grp = df.groupby(id_col)
    res = pd.DataFrame({
        "cantidad": grp["quantity"].sum() if "quantity" in df.columns else 0,
        "ventas_netas": grp["price_subtotal_signed"].sum(),
        "n_facturas": grp["move_id"].nunique(),
    }).reset_index().rename(columns={id_col: out_id})

    # Nombre legible: tomamos el primer no-vacío del grupo
    if name_col in df.columns:
        names = (
            df.dropna(subset=[name_col])
              .drop_duplicates(id_col)
              .set_index(id_col)[name_col]
              .to_dict()
        )
        res[out_name] = res[out_id].map(names).fillna("Sin nombre")
    else:
        res[out_name] = res[out_id].astype(str)
    res.loc[res[out_id] == -1, out_name] = "Sin producto" if group_by == "product" else "Sin categoría"

    total = float(res["ventas_netas"].sum())
    res["participacion_pct"] = (res["ventas_netas"] / total * 100) if total else 0.0

    res = res.sort_values("ventas_netas", ascending=False).reset_index(drop=True)
    if top_n is not None:
        res = res.head(top_n)

    return res[[out_id, out_name, "cantidad", "ventas_netas",
                "n_facturas", "participacion_pct"]]
