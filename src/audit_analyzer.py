# -*- coding: utf-8 -*-
"""
Motor de auditoría de órdenes de venta y compra.

Para cada línea de orden cruza tres cantidades que, en una orden
correctamente cerrada, deben coincidir:

    ordenada  ==  entregada/recibida  ==  facturada

Detecta y clasifica las líneas (y las órdenes) donde no coinciden, para
identificar entregas sin facturar, facturas sin entrega, cantidades
pendientes y descuadres positivos o negativos.

Entrada: DataFrame con el esquema que produce
`extractor.extract_sale_order_audit` / `extract_purchase_order_audit`.
"""
from __future__ import annotations

import pandas as pd

# Tolerancia para comparar cantidades (ignora ruido de redondeo decimal).
TOL = 0.01

# Columnas que agrega `audit_order_lines`.
_AUDIT_COLS = [
    "dif_entrega", "dif_factura", "dif_entrega_factura",
    "tipo_discrepancia", "tiene_discrepancia", "severidad",
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


def _clasificar(
    tipo: str,
    dif_entrega: float,
    dif_factura: float,
    dif_ef: float,
) -> tuple[str, str]:
    """
    Clasifica una línea según sus diferencias de cantidad.

    Devuelve (tipo_discrepancia, severidad) donde severidad es uno de
    "OK", "Revisar" o "Crítica". Es "Crítica" cuando lo entregado y lo
    facturado no coinciden (descuadre real, no solo pendiente por tiempo).
    """
    es_compra = str(tipo).strip().lower() == "compra"
    inf = "recibir" if es_compra else "entregar"
    part = "Recibido" if es_compra else "Entregado"

    flags: list[str] = []
    # 1) Inconsistencia entrega vs factura (lo más grave a corregir).
    if dif_ef > TOL:
        flags.append(f"{part} sin facturar")
    elif dif_ef < -TOL:
        flags.append(f"Facturado sin {inf}")
    # 2) Facturación vs cantidad ordenada.
    if dif_factura < -TOL:
        flags.append("Falta facturar")
    elif dif_factura > TOL:
        flags.append("Facturado de más")
    # 3) Entrega/recepción vs cantidad ordenada.
    if dif_entrega < -TOL:
        flags.append(f"Falta {inf}")
    elif dif_entrega > TOL:
        flags.append(f"{part} de más")

    if not flags:
        return "OK", "OK"
    severidad = "Crítica" if abs(dif_ef) > TOL else "Revisar"
    return " · ".join(flags), severidad


def audit_order_lines(df: pd.DataFrame) -> pd.DataFrame:
    """
    Agrega las columnas de auditoría al DataFrame de líneas de orden:
      dif_entrega, dif_factura, dif_entrega_factura,
      tipo_discrepancia, tiene_discrepancia, severidad.
    """
    if df is None or df.empty:
        out = df.copy() if df is not None else pd.DataFrame()
        for c in _AUDIT_COLS:
            if c not in out.columns:
                out[c] = pd.Series(dtype="object")
        return out

    d = df.copy()
    for c in ("cant_ordenada", "cant_entregada",
              "cant_facturada", "cant_por_facturar"):
        d[c] = pd.to_numeric(d.get(c), errors="coerce").fillna(0.0)

    d["dif_entrega"] = (d["cant_entregada"] - d["cant_ordenada"]).round(3)
    d["dif_factura"] = (d["cant_facturada"] - d["cant_ordenada"]).round(3)
    d["dif_entrega_factura"] = (
        d["cant_entregada"] - d["cant_facturada"]
    ).round(3)

    clas = d.apply(
        lambda r: _clasificar(
            r.get("tipo"), r["dif_entrega"],
            r["dif_factura"], r["dif_entrega_factura"],
        ),
        axis=1, result_type="expand",
    )
    d["tipo_discrepancia"] = clas[0]
    d["severidad"] = clas[1]
    d["tiene_discrepancia"] = d["severidad"] != "OK"
    return d


def summarize_audit_by_order(df_lines: pd.DataFrame) -> pd.DataFrame:
    """
    Resume la auditoría a nivel de ORDEN: cuántas líneas tiene, cuántas con
    discrepancia, cuántas críticas, y el total de cada cantidad. Ordena
    poniendo primero las órdenes con más problemas.
    """
    cols = [
        "order_id", "orden", "tipo", "fecha", "socio", "empresa",
        "estado_orden", "invoice_status", "n_lineas", "n_discrepancias",
        "n_criticas", "cant_ordenada", "cant_entregada", "cant_facturada",
        "cant_por_facturar", "dif_entrega", "dif_factura",
        "dif_entrega_factura", "severidad",
    ]
    if df_lines is None or df_lines.empty:
        return pd.DataFrame(columns=cols)

    d = df_lines.copy()
    d["_critica"] = (d["severidad"] == "Crítica").astype(int)
    # Agrupamos SOLO por order_id (identifica la orden de forma única); el
    # resto de campos son atributos de la orden y se toman con `first`.
    g = d.groupby("order_id", dropna=False, as_index=False).agg(
        orden=("orden", "first"),
        tipo=("tipo", "first"),
        fecha=("fecha", "first"),
        socio=("socio", "first"),
        empresa=("empresa", "first"),
        estado_orden=("estado_orden", "first"),
        invoice_status=("invoice_status", "first"),
        n_lineas=("linea_id", "count"),
        n_discrepancias=("tiene_discrepancia", "sum"),
        n_criticas=("_critica", "sum"),
        cant_ordenada=("cant_ordenada", "sum"),
        cant_entregada=("cant_entregada", "sum"),
        cant_facturada=("cant_facturada", "sum"),
        cant_por_facturar=("cant_por_facturar", "sum"),
        dif_entrega=("dif_entrega", "sum"),
        dif_factura=("dif_factura", "sum"),
        dif_entrega_factura=("dif_entrega_factura", "sum"),
    )
    g["n_discrepancias"] = g["n_discrepancias"].astype(int)
    g["n_criticas"] = g["n_criticas"].astype(int)

    def _sev(r):
        if r["n_criticas"] > 0:
            return "Crítica"
        if r["n_discrepancias"] > 0:
            return "Revisar"
        return "OK"

    g["severidad"] = g.apply(_sev, axis=1)
    g = g.sort_values(
        ["n_criticas", "n_discrepancias", "fecha"],
        ascending=[False, False, False],
    ).reset_index(drop=True)
    return g[cols]


def compute_audit_kpis(df_lines: pd.DataFrame) -> dict:
    """KPIs de la auditoría a partir de las líneas ya auditadas."""
    base = {
        "n_ordenes": 0, "n_lineas": 0,
        "n_ordenes_discrepancia": 0, "n_lineas_discrepancia": 0,
        "n_criticas": 0, "pct_ordenes_ok": 100.0,
    }
    if df_lines is None or df_lines.empty:
        return base

    d = df_lines
    n_ordenes = int(d["order_id"].nunique())
    n_lineas = int(len(d))
    con_disc = d[d["tiene_discrepancia"]]
    n_ordenes_disc = int(con_disc["order_id"].nunique())
    n_lineas_disc = int(len(con_disc))
    n_criticas = int((d["severidad"] == "Crítica").sum())
    pct_ok = (
        (n_ordenes - n_ordenes_disc) / n_ordenes * 100
        if n_ordenes else 100.0
    )
    return {
        "n_ordenes": n_ordenes,
        "n_lineas": n_lineas,
        "n_ordenes_discrepancia": n_ordenes_disc,
        "n_lineas_discrepancia": n_lineas_disc,
        "n_criticas": n_criticas,
        "pct_ordenes_ok": round(pct_ok, 1),
    }


def audit_by_month(df_lines: pd.DataFrame) -> pd.DataFrame:
    """
    Evolución mensual de la auditoría: por cada mes (según fecha de la
    orden) el total de líneas, las que tienen discrepancia y las críticas.
    """
    cols = ["mes", "mes_label", "lineas",
            "lineas_discrepancia", "lineas_criticas"]
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
    d["_disc"] = d["tiene_discrepancia"].astype(int)
    d["_crit"] = (d["severidad"] == "Crítica").astype(int)
    g = d.groupby("_mes", as_index=False).agg(
        lineas=("linea_id", "count"),
        lineas_discrepancia=("_disc", "sum"),
        lineas_criticas=("_crit", "sum"),
    ).rename(columns={"_mes": "mes"}).sort_values("mes")
    g["mes_label"] = g["mes"].dt.strftime("%Y-%m")
    return g[cols].reset_index(drop=True)


def explode_problem_types(df_lines: pd.DataFrame) -> pd.DataFrame:
    """
    Devuelve un DataFrame con una fila por (línea × tipo de problema
    individual). Solo considera líneas con discrepancia. Agrega la
    columna `problema` con cada etiqueta suelta (una línea con dos
    problemas aparece en dos filas).
    """
    if df_lines is None or df_lines.empty:
        return pd.DataFrame()
    d = df_lines[df_lines["tiene_discrepancia"]].copy()
    if d.empty:
        return d
    d["problema"] = d["tipo_discrepancia"].str.split(" · ")
    return d.explode("problema").reset_index(drop=True)
