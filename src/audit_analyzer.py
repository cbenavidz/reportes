# -*- coding: utf-8 -*-
"""
Motor de auditoría de órdenes de venta y compra.

Se enfoca en DOS saldos que en una orden bien cerrada deben ser cero
(las mismas columnas del Análisis de compra/venta de Odoo):

  1. Cantidad a facturar  = cantidad ordenada − cantidad facturada.
  2. Cantidad por recibir = cantidad facturada − cantidad recibida
     (en ventas: cantidad facturada − cantidad entregada).

Marca las líneas (y las órdenes) donde alguno de los dos saldos ≠ 0.
"""
from __future__ import annotations

import pandas as pd

# Tolerancia para comparar cantidades (ignora ruido de redondeo decimal).
TOL = 0.01

# Columnas que agrega `audit_order_lines`.
_AUDIT_COLS = [
    "cant_a_facturar", "cant_por_recibir",
    "tipo_discrepancia", "tiene_discrepancia",
]

# Etiquetas legibles para mostrar en la interfaz.
INVOICE_STATUS_LABELS = {
    "no": "Nada que facturar",
    "to invoice": "Por facturar",
    "invoiced": "Facturada",
    "upselling": "Venta adicional",
}
ESTADO_ORDEN_LABELS = {
    "draft": "Borrador",
    "sent": "Enviada",
    "sale": "Venta confirmada",
    "purchase": "Compra confirmada",
    "done": "Bloqueada / Hecha",
    "cancel": "Cancelada",
}


def _clasificar(tipo: str, a_facturar: float, por_recibir: float) -> str:
    """Devuelve la etiqueta de discrepancia según los dos saldos."""
    es_compra = str(tipo).strip().lower() == "compra"
    verbo = "recibir" if es_compra else "entregar"
    flags: list[str] = []
    if abs(a_facturar) > TOL:
        flags.append("Por facturar")
    if abs(por_recibir) > TOL:
        flags.append(f"Por {verbo}")
    return " · ".join(flags) if flags else "OK"


def audit_order_lines(df: pd.DataFrame) -> pd.DataFrame:
    """
    Agrega las columnas de auditoría al DataFrame de líneas de orden:
      cant_a_facturar   = cant_ordenada  − cant_facturada
      cant_por_recibir  = cant_facturada − cant_entregada
      tipo_discrepancia = "Por facturar" / "Por recibir" / combinación / "OK"
      tiene_discrepancia
    """
    if df is None or df.empty:
        out = df.copy() if df is not None else pd.DataFrame()
        for c in _AUDIT_COLS:
            if c not in out.columns:
                out[c] = pd.Series(dtype="object")
        return out

    d = df.copy()
    for c in ("cant_ordenada", "cant_entregada", "cant_facturada"):
        d[c] = pd.to_numeric(d.get(c), errors="coerce").fillna(0.0)

    # Saldo 1: lo que falta facturar respecto a lo ordenado.
    d["cant_a_facturar"] = (
        d["cant_ordenada"] - d["cant_facturada"]
    ).round(3)
    # Saldo 2 (calculado): descuadre entre lo facturado y lo movido.
    d["cant_por_recibir"] = (
        d["cant_facturada"] - d["cant_entregada"]
    ).round(3)

    d["tipo_discrepancia"] = d.apply(
        lambda r: _clasificar(
            r.get("tipo"), r["cant_a_facturar"], r["cant_por_recibir"],
        ),
        axis=1,
    )
    d["tiene_discrepancia"] = d["tipo_discrepancia"] != "OK"
    return d


def summarize_audit_by_order(df_lines: pd.DataFrame) -> pd.DataFrame:
    """Resume la auditoría a nivel de ORDEN (agrupa solo por order_id)."""
    cols = [
        "order_id", "orden", "tipo", "fecha", "socio", "empresa",
        "estado_orden", "invoice_status", "n_lineas", "n_discrepancias",
        "cant_ordenada", "cant_entregada", "cant_facturada",
        "cant_a_facturar", "cant_por_recibir",
    ]
    if df_lines is None or df_lines.empty:
        return pd.DataFrame(columns=cols)

    g = df_lines.groupby("order_id", dropna=False, as_index=False).agg(
        orden=("orden", "first"),
        tipo=("tipo", "first"),
        fecha=("fecha", "first"),
        socio=("socio", "first"),
        empresa=("empresa", "first"),
        estado_orden=("estado_orden", "first"),
        invoice_status=("invoice_status", "first"),
        n_lineas=("linea_id", "count"),
        n_discrepancias=("tiene_discrepancia", "sum"),
        cant_ordenada=("cant_ordenada", "sum"),
        cant_entregada=("cant_entregada", "sum"),
        cant_facturada=("cant_facturada", "sum"),
        cant_a_facturar=("cant_a_facturar", "sum"),
        cant_por_recibir=("cant_por_recibir", "sum"),
    )
    g["n_discrepancias"] = g["n_discrepancias"].astype(int)
    # Orden: fecha más reciente primero; en empates, orden con más discrepancias.
    g = g.sort_values(
        ["fecha", "n_discrepancias"], ascending=[False, False],
    ).reset_index(drop=True)
    return g[cols]


def compute_audit_kpis(df_lines: pd.DataFrame) -> dict:
    """KPIs de la auditoría a partir de las líneas ya auditadas."""
    base = {
        "n_ordenes": 0, "n_lineas": 0,
        "n_ordenes_discrepancia": 0,
        "n_lineas_por_facturar": 0, "n_lineas_por_recibir": 0,
        "pct_ordenes_ok": 100.0,
    }
    if df_lines is None or df_lines.empty:
        return base

    d = df_lines
    n_ordenes = int(d["order_id"].nunique())
    n_lineas = int(len(d))
    con_disc = d[d["tiene_discrepancia"]]
    n_ordenes_disc = int(con_disc["order_id"].nunique())
    n_por_facturar = int((d["cant_a_facturar"].abs() > TOL).sum())
    n_por_recibir = int((d["cant_por_recibir"].abs() > TOL).sum())
    pct_ok = (
        (n_ordenes - n_ordenes_disc) / n_ordenes * 100
        if n_ordenes else 100.0
    )
    return {
        "n_ordenes": n_ordenes,
        "n_lineas": n_lineas,
        "n_ordenes_discrepancia": n_ordenes_disc,
        "n_lineas_por_facturar": n_por_facturar,
        "n_lineas_por_recibir": n_por_recibir,
        "pct_ordenes_ok": round(pct_ok, 1),
    }


def audit_by_month(df_lines: pd.DataFrame) -> pd.DataFrame:
    """
    Evolución mensual: por cada mes (según fecha de la orden), cuántas
    líneas tienen cantidad a facturar y cuántas tienen cantidad por
    recibir/entregar.
    """
    cols = ["mes", "mes_label", "lineas_por_facturar", "lineas_por_recibir"]
    if df_lines is None or df_lines.empty:
        return pd.DataFrame(columns=cols)
    d = df_lines.copy()
    d["_mes"] = (
        pd.to_datetime(d["fecha"], errors="coerce")
        .dt.to_period("M").dt.to_timestamp()
    )
    d = d.dropna(subset=["_mes"])
    if d.empty:
        return pd.DataFrame(columns=cols)
    d["_fact"] = (d["cant_a_facturar"].abs() > TOL).astype(int)
    d["_recib"] = (d["cant_por_recibir"].abs() > TOL).astype(int)
    g = d.groupby("_mes", as_index=False).agg(
        lineas_por_facturar=("_fact", "sum"),
        lineas_por_recibir=("_recib", "sum"),
    ).rename(columns={"_mes": "mes"}).sort_values("mes")
    g["mes_label"] = g["mes"].dt.strftime("%Y-%m")
    return g[cols].reset_index(drop=True)


def explode_problem_types(df_lines: pd.DataFrame) -> pd.DataFrame:
    """
    Una fila por (línea × tipo de problema individual). Solo líneas con
    discrepancia. Agrega la columna `problema` con cada etiqueta suelta.
    """
    if df_lines is None or df_lines.empty:
        return pd.DataFrame()
    d = df_lines[df_lines["tiene_discrepancia"]].copy()
    if d.empty:
        return d
    d["problema"] = d["tipo_discrepancia"].str.split(" · ")
    return d.explode("problema").reset_index(drop=True)
