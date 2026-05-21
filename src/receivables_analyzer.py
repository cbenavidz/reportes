# -*- coding: utf-8 -*-
"""
src/receivables_analyzer.py
===========================
Motor de análisis de Cuentas por Cobrar (CxC) — vista operativa.

Funciones principales:
  - enrich_receivables: normaliza facturas de cliente abiertas y calcula
    días para vencer, estado, antigüedad y días de mora.
  - compute_receivables_kpis: KPIs de cartera (total por cobrar, vencido…).
  - compute_calendar: calendario de cobros próximos.
  - compute_morosos: ranking de clientes en mora (priorización de gestión).
  - compute_aging: antigüedad de la cartera.
  - compute_dso: días promedio de cobro (DSO) y rotación de cartera.
  - compute_top_clientes: ranking de clientes por saldo pendiente.
  - compute_proyeccion_cobros_semanal: ingresos esperados por semana.
  - build_tabla_detalle: tabla detallada de facturas por cobrar.
"""
from __future__ import annotations

from datetime import date

import pandas as pd


# ===========================================================================
# Enriquecimiento de facturas por cobrar
# ===========================================================================


def enrich_receivables(
    receivables: pd.DataFrame,
    today: date | None = None,
) -> pd.DataFrame:
    """
    Enriquece las facturas de cliente abiertas con:
      - saldo: saldo pendiente de cobro
      - total_factura: monto total de la factura
      - dias_para_vencer: días hasta el vencimiento (negativo = vencida)
      - dias_mora: días de atraso (0 si no está vencida)
      - estado: 'Vencida' / 'Vence hoy' / 'Por cobrar'
      - bucket_aging: clasificación de antigüedad
    """
    if receivables is None or receivables.empty:
        return pd.DataFrame()

    if today is None:
        today = date.today()
    today_ts = pd.Timestamp(today)

    df = receivables.copy()

    # Saldo pendiente
    if "amount_residual_signed" in df.columns:
        df["saldo"] = df["amount_residual_signed"]
    elif "amount_residual" in df.columns:
        df["saldo"] = df["amount_residual"]
    else:
        df["saldo"] = 0.0
    df["saldo"] = pd.to_numeric(df["saldo"], errors="coerce").fillna(0.0)

    # Total de la factura
    if "amount_total_signed" in df.columns:
        df["total_factura"] = df["amount_total_signed"]
    elif "amount_total" in df.columns:
        df["total_factura"] = df["amount_total"]
    else:
        df["total_factura"] = df["saldo"]
    df["total_factura"] = pd.to_numeric(
        df["total_factura"], errors="coerce"
    ).fillna(0.0)

    # Fechas
    for col in ("invoice_date", "invoice_date_due"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    if "invoice_date_due" not in df.columns:
        df["invoice_date_due"] = df.get("invoice_date")
    df["invoice_date_due"] = df["invoice_date_due"].fillna(
        df.get("invoice_date")
    )

    # Días para vencer / días de mora
    df["dias_para_vencer"] = (df["invoice_date_due"] - today_ts).dt.days
    df["dias_mora"] = df["dias_para_vencer"].apply(
        lambda d: abs(int(d)) if pd.notna(d) and d < 0 else 0
    )

    def _estado(d):
        if pd.isna(d):
            return "Sin fecha"
        if d < 0:
            return "Vencida"
        if d == 0:
            return "Vence hoy"
        return "Por cobrar"

    df["estado"] = df["dias_para_vencer"].apply(_estado)

    def _bucket(d):
        if pd.isna(d):
            return "Sin fecha"
        if d >= 0:
            return "Por vencer"
        dd = abs(d)
        if dd <= 30:
            return "1-30 días"
        if dd <= 60:
            return "31-60 días"
        if dd <= 90:
            return "61-90 días"
        return "+90 días"

    df["bucket_aging"] = df["dias_para_vencer"].apply(_bucket)

    return df


# ===========================================================================
# KPIs de cartera
# ===========================================================================


def compute_receivables_kpis(
    enriched: pd.DataFrame,
    today: date | None = None,
    horizonte_dias: int = 30,
) -> dict:
    """
    KPIs de cuentas por cobrar.

    Devuelve dict con: total_por_cobrar, total_vencido, total_por_cobrar_h,
    n_facturas, n_vencidas, n_clientes, n_clientes_mora, pct_vencido,
    mora_promedio_dias.
    """
    if enriched is None or enriched.empty:
        return {
            "total_por_cobrar": 0.0, "total_vencido": 0.0,
            "total_por_cobrar_h": 0.0, "n_facturas": 0, "n_vencidas": 0,
            "n_clientes": 0, "n_clientes_mora": 0, "pct_vencido": 0.0,
            "mora_promedio_dias": 0.0,
        }

    df = enriched
    total = float(df["saldo"].sum())
    vencidas = df[df["estado"] == "Vencida"]
    vencido = float(vencidas["saldo"].sum())
    por_cobrar_h = float(df.loc[
        (df["dias_para_vencer"] >= 0)
        & (df["dias_para_vencer"] <= horizonte_dias),
        "saldo",
    ].sum())

    mora_prom = 0.0
    if not vencidas.empty:
        # Mora ponderada por saldo
        peso = vencidas["saldo"].clip(lower=0)
        if peso.sum() > 0:
            mora_prom = float(
                (vencidas["dias_mora"] * peso).sum() / peso.sum()
            )

    return {
        "total_por_cobrar": total,
        "total_vencido": vencido,
        "total_por_cobrar_h": por_cobrar_h,
        "n_facturas": int(len(df)),
        "n_vencidas": int(len(vencidas)),
        "n_clientes": int(df["partner_id"].nunique()),
        "n_clientes_mora": int(vencidas["partner_id"].nunique()),
        "pct_vencido": (vencido / total * 100) if total else 0.0,
        "mora_promedio_dias": mora_prom,
    }


# ===========================================================================
# Calendario de cobros
# ===========================================================================


def compute_calendar(
    enriched: pd.DataFrame,
    today: date | None = None,
    horizonte_dias: int = 30,
) -> pd.DataFrame:
    """
    Calendario de cobros: una fila por día con el monto a cobrar, el #
    de facturas y el # de clientes de ese día.
    """
    if enriched is None or enriched.empty:
        return pd.DataFrame(columns=[
            "fecha", "monto", "n_facturas", "n_clientes",
        ])
    if today is None:
        today = date.today()
    today_ts = pd.Timestamp(today)
    limite = today_ts + pd.Timedelta(days=horizonte_dias)

    df = enriched.copy()
    df = df[
        (df["invoice_date_due"] >= today_ts)
        & (df["invoice_date_due"] <= limite)
    ]
    if df.empty:
        return pd.DataFrame(columns=[
            "fecha", "monto", "n_facturas", "n_clientes",
        ])

    df["fecha"] = df["invoice_date_due"].dt.normalize()
    cal = df.groupby("fecha", as_index=False).agg(
        monto=("saldo", "sum"),
        n_facturas=("id", "count"),
        n_clientes=("partner_id", "nunique"),
    )
    return cal.sort_values("fecha").reset_index(drop=True)


def compute_proyeccion_cobros_semanal(
    enriched: pd.DataFrame,
    today: date | None = None,
    semanas: int = 8,
) -> pd.DataFrame:
    """
    Proyección de ingresos (cobros esperados) por semana. La primera
    semana incluye todo lo ya vencido.

    Devuelve DataFrame con: semana_inicio, semana_label, monto,
    n_facturas, monto_acumulado.
    """
    if enriched is None or enriched.empty:
        return pd.DataFrame(columns=[
            "semana_inicio", "semana_label", "monto", "n_facturas",
            "monto_acumulado",
        ])
    if today is None:
        today = date.today()
    today_ts = pd.Timestamp(today)
    lunes_actual = today_ts - pd.Timedelta(days=today_ts.weekday())

    df = enriched.copy()
    df = df[df["invoice_date_due"].notna()]

    rows = []
    acumulado = 0.0
    for i in range(semanas):
        ini = lunes_actual + pd.Timedelta(weeks=i)
        nxt = ini + pd.Timedelta(weeks=1)
        if i == 0:
            mask = df["invoice_date_due"] < nxt
        else:
            mask = (df["invoice_date_due"] >= ini) & (df["invoice_date_due"] < nxt)
        sub = df[mask]
        monto = float(sub["saldo"].sum())
        acumulado += monto
        rows.append({
            "semana_inicio": ini,
            "semana_label": (
                "Vencido + " + ini.strftime("%d/%m")
                if i == 0 else ini.strftime("%d/%m")
            ),
            "monto": monto,
            "n_facturas": int(len(sub)),
            "monto_acumulado": acumulado,
        })
    return pd.DataFrame(rows)


# ===========================================================================
# Clientes en mora
# ===========================================================================


def compute_morosos(
    enriched: pd.DataFrame,
    top_n: int = 20,
) -> pd.DataFrame:
    """
    Ranking de clientes en mora para priorizar la gestión de cobro.

    Devuelve DataFrame con: partner_name, saldo_vencido, saldo_total,
    n_facturas_vencidas, dias_mora_max, dias_mora_prom, pct_cartera.
    Ordenado por saldo vencido descendente.
    """
    if enriched is None or enriched.empty:
        return pd.DataFrame()

    vencidas = enriched[enriched["estado"] == "Vencida"].copy()
    if vencidas.empty:
        return pd.DataFrame()

    # Saldo total por cliente (todas las facturas, no solo vencidas)
    saldo_total_cli = enriched.groupby("partner_id")["saldo"].sum()

    grp = vencidas.groupby(
        ["partner_id", "partner_name"], as_index=False,
    ).agg(
        saldo_vencido=("saldo", "sum"),
        n_facturas_vencidas=("id", "count"),
        dias_mora_max=("dias_mora", "max"),
        dias_mora_prom=("dias_mora", "mean"),
    )
    grp["saldo_total"] = grp["partner_id"].map(saldo_total_cli).fillna(0.0)

    total_vencido = grp["saldo_vencido"].sum()
    grp["pct_cartera_vencida"] = (
        grp["saldo_vencido"] / total_vencido * 100
    ) if total_vencido else 0.0

    grp["dias_mora_prom"] = grp["dias_mora_prom"].round(0)
    grp = grp.sort_values("saldo_vencido", ascending=False).reset_index(drop=True)
    if top_n:
        grp = grp.head(top_n)
    return grp


# ===========================================================================
# Aging de cartera
# ===========================================================================


def compute_aging(enriched: pd.DataFrame) -> pd.DataFrame:
    """
    Antigüedad de la cartera. Devuelve DataFrame con bucket, monto,
    n_facturas y porcentaje del total.
    """
    if enriched is None or enriched.empty:
        return pd.DataFrame(columns=["bucket", "monto", "n_facturas", "pct"])

    orden = ["Por vencer", "1-30 días", "31-60 días", "61-90 días",
             "+90 días", "Sin fecha"]
    grp = enriched.groupby("bucket_aging", as_index=False).agg(
        monto=("saldo", "sum"),
        n_facturas=("id", "count"),
    )
    total = grp["monto"].sum()
    grp["pct"] = (grp["monto"] / total * 100) if total else 0
    grp["_orden"] = grp["bucket_aging"].apply(
        lambda b: orden.index(b) if b in orden else 99
    )
    grp = grp.sort_values("_orden").drop(columns="_orden")
    grp = grp.rename(columns={"bucket_aging": "bucket"})
    return grp.reset_index(drop=True)


# ===========================================================================
# DSO y rotación de cartera
# ===========================================================================


def compute_dso(
    cartera_promedio: float,
    ventas_periodo: float,
    dias_periodo: int = 365,
) -> dict:
    """
    Calcula DSO (Days Sales Outstanding) y rotación de cartera.

    DSO = (Cartera promedio / Ventas a crédito) × días del período
    Rotación = Ventas del período / Cartera promedio

    Un DSO bajo significa que cobras rápido; uno alto indica que el
    dinero queda mucho tiempo en manos de los clientes.

    Args:
        cartera_promedio: saldo promedio de cuentas por cobrar.
        ventas_periodo: total de ventas a crédito del período.
        dias_periodo: días del período (365 = año).
    """
    if ventas_periodo <= 0 or cartera_promedio <= 0:
        return {"dso": 0.0, "rotacion": 0.0}
    dso = (cartera_promedio / ventas_periodo) * dias_periodo
    rotacion = ventas_periodo / cartera_promedio
    return {"dso": round(dso, 1), "rotacion": round(rotacion, 2)}


# ===========================================================================
# Top clientes
# ===========================================================================


def compute_top_clientes(
    enriched: pd.DataFrame,
    top_n: int = 15,
) -> pd.DataFrame:
    """
    Ranking de clientes por saldo pendiente. Devuelve DataFrame con
    partner_name, saldo_total, saldo_vencido, n_facturas, pct_concentracion.
    """
    if enriched is None or enriched.empty:
        return pd.DataFrame()

    df = enriched.copy()
    df["_vencido"] = df["saldo"].where(df["estado"] == "Vencida", 0.0)
    grp = df.groupby(["partner_id", "partner_name"], as_index=False).agg(
        saldo_total=("saldo", "sum"),
        saldo_vencido=("_vencido", "sum"),
        n_facturas=("id", "count"),
    )
    total = grp["saldo_total"].sum()
    grp["pct_concentracion"] = (
        grp["saldo_total"] / total * 100
    ) if total else 0
    grp = grp.sort_values("saldo_total", ascending=False).reset_index(drop=True)
    if top_n:
        grp = grp.head(top_n)
    return grp


# ===========================================================================
# Detalle ordenado para la tabla principal
# ===========================================================================


def build_tabla_detalle(enriched: pd.DataFrame) -> pd.DataFrame:
    """
    Tabla detallada de todas las facturas por cobrar, ordenada por fecha
    de vencimiento.
    """
    if enriched is None or enriched.empty:
        return pd.DataFrame()

    cols = [c for c in [
        "name", "ref", "partner_name",
        "invoice_date", "invoice_date_due", "dias_para_vencer", "dias_mora",
        "estado", "total_factura", "saldo", "bucket_aging",
        "payment_term_name", "company_id_name",
    ] if c in enriched.columns]
    out = enriched[cols].copy()
    out = out.sort_values("invoice_date_due", na_position="last")
    return out.reset_index(drop=True)
