# -*- coding: utf-8 -*-
"""
Motor de la TABLA DINÁMICA de Cartera / Cuentas por Cobrar.

Toma las facturas por cobrar ya enriquecidas (`enrich_receivables`) y las
prepara para cruzarlas libremente: el usuario elige filas, columnas, métrica
y filtros, igual que una tabla dinámica de Excel.

Pandas puro (sin Streamlit ni Odoo) para poder probarlo aislado.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Dimensiones disponibles: etiqueta visible -> columna del DataFrame
DIMENSIONES = {
    "Cliente": "cliente",
    "Vendedor": "vendedor",
    "Ciudad": "ciudad",
    "Departamento": "departamento",
    "Antigüedad (aging)": "bucket_aging",
    "Estado": "estado",
    "Mes de vencimiento": "mes_vencimiento",
    "Mes de facturación": "mes_factura",
    "Empresa": "empresa",
    "Diario": "diario",
}

# Métricas: etiqueta -> (columna, función de agregación)
METRICAS = {
    "Saldo por cobrar": ("saldo", "sum"),
    "Saldo vencido": ("saldo_vencido", "sum"),
    "Total facturado": ("total_factura", "sum"),
    "# Facturas": ("id", "count"),
    "# Clientes": ("partner_id", "nunique"),
    "Días de mora (prom.)": ("dias_mora", "mean"),
}

ORDEN_AGING = ["Corriente", "1-30", "31-60", "61-90", "91-180", "+180"]


def preparar(
    enriched: pd.DataFrame,
    partners: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    Añade a la cartera enriquecida las dimensiones que faltan: vendedor,
    ciudad, departamento, mes de vencimiento/facturación y saldo vencido.
    """
    if enriched is None or enriched.empty:
        return pd.DataFrame()
    df = enriched.copy()

    # Nombre del cliente
    if "partner_name" in df.columns:
        df["cliente"] = df["partner_name"].fillna("—")
    else:
        df["cliente"] = "—"

    # Vendedor / ciudad / departamento desde los clientes
    if partners is not None and not partners.empty and "id" in partners.columns:
        cols = [c for c in ["id", "user_name", "city", "state_name"]
                if c in partners.columns]
        geo = partners[cols].drop_duplicates("id").rename(columns={"id": "partner_id"})
        df["partner_id"] = pd.to_numeric(df["partner_id"], errors="coerce")
        geo["partner_id"] = pd.to_numeric(geo["partner_id"], errors="coerce")
        df = df.merge(geo, on="partner_id", how="left")
    df["vendedor"] = df.get("user_name", pd.Series(index=df.index)).fillna("Sin vendedor")
    df["ciudad"] = df.get("city", pd.Series(index=df.index)).fillna("Sin ciudad")
    df["ciudad"] = df["ciudad"].replace("", "Sin ciudad")
    df["departamento"] = df.get("state_name", pd.Series(index=df.index)).fillna("—")

    # Empresa / diario (si vienen)
    df["empresa"] = df.get("company_name", pd.Series(index=df.index)).fillna("—")
    df["diario"] = df.get("journal_id_name", pd.Series(index=df.index)).fillna("—")

    # Meses
    for col, out in [("invoice_date_due", "mes_vencimiento"),
                     ("invoice_date", "mes_factura")]:
        if col in df.columns:
            d = pd.to_datetime(df[col], errors="coerce")
            df[out] = d.dt.to_period("M").astype(str).replace("NaT", "—")
        else:
            df[out] = "—"

    # Saldo vencido (solo el de facturas vencidas)
    dias_mora = pd.to_numeric(df.get("dias_mora", 0), errors="coerce").fillna(0)
    df["saldo_vencido"] = np.where(dias_mora > 0, df["saldo"], 0.0)

    for c in DIMENSIONES.values():
        if c not in df.columns:
            df[c] = "—"
        df[c] = df[c].astype(str).replace({"nan": "—", "None": "—", "": "—"})
    return df


def construir(
    df: pd.DataFrame,
    filas: list[str],
    columnas: list[str],
    metrica: str,
    totales: bool = True,
) -> pd.DataFrame:
    """
    Arma la tabla dinámica.

    `filas` / `columnas`: etiquetas de DIMENSIONES.
    `metrica`: etiqueta de METRICAS.
    """
    if df is None or df.empty or metrica not in METRICAS:
        return pd.DataFrame()
    col_val, agg = METRICAS[metrica]
    if col_val not in df.columns:
        return pd.DataFrame()

    idx = [DIMENSIONES[f] for f in filas if f in DIMENSIONES]
    cols = [DIMENSIONES[c] for c in columnas if c in DIMENSIONES]
    if not idx and not cols:
        return pd.DataFrame()

    piv = pd.pivot_table(
        df, index=idx or None, columns=cols or None, values=col_val,
        aggfunc=agg, fill_value=0,
        margins=totales, margins_name="TOTAL",
        observed=True,
    )
    # Ordenar buckets de aging con sentido de negocio, no alfabético
    piv = _ordenar_aging(piv, idx, cols)
    return piv


def _ordenar_aging(piv: pd.DataFrame, idx: list[str], cols: list[str]) -> pd.DataFrame:
    """Reordena los buckets de antigüedad en orden lógico."""
    try:
        if idx == ["bucket_aging"]:
            orden = [b for b in ORDEN_AGING if b in piv.index]
            resto = [b for b in piv.index if b not in orden and b != "TOTAL"]
            fin = orden + resto + (["TOTAL"] if "TOTAL" in piv.index else [])
            piv = piv.reindex(fin)
        if cols == ["bucket_aging"]:
            orden = [b for b in ORDEN_AGING if b in piv.columns]
            resto = [c for c in piv.columns if c not in orden and c != "TOTAL"]
            fin = orden + resto + (["TOTAL"] if "TOTAL" in piv.columns else [])
            piv = piv[fin]
    except Exception:  # noqa: BLE001 — el orden es cosmético, nunca debe romper
        pass
    return piv


def tabla_plana(df: pd.DataFrame) -> pd.DataFrame:
    """Datos planos (una fila por factura) listos para pivotear en Excel."""
    if df is None or df.empty:
        return pd.DataFrame()
    cols = [
        ("name", "Factura"), ("cliente", "Cliente"), ("vendedor", "Vendedor"),
        ("ciudad", "Ciudad"), ("departamento", "Departamento"),
        ("empresa", "Empresa"), ("diario", "Diario"),
        ("invoice_date", "Fecha factura"), ("invoice_date_due", "Vencimiento"),
        ("mes_factura", "Mes factura"), ("mes_vencimiento", "Mes vencimiento"),
        ("bucket_aging", "Antigüedad"), ("estado", "Estado"),
        ("dias_mora", "Días mora"), ("dias_para_vencer", "Días para vencer"),
        ("total_factura", "Total factura"), ("saldo", "Saldo"),
        ("saldo_vencido", "Saldo vencido"),
    ]
    out = pd.DataFrame()
    for src, dst in cols:
        if src in df.columns:
            out[dst] = df[src]
    return out
