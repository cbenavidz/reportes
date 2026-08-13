#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Diagnóstico MENSUAL del volumen físico (galones) por categoría.

Compara mes a mes la fórmula anterior con la corregida:

  ANTES:  volumen = quantity_base × product.volume
  AHORA:  volumen = (quantity_base / product_uom_factor) × product.volume

Pensado para validar la expectativa del negocio de que el volumen de
"LUBRICANTES EDUARDOÑO" sea homogéneo entre meses: si la serie "AHORA"
es estable y la "ANTES" es errática, la corrección es la buena.

Uso (en el Mac, dentro del venv del proyecto):
  python scripts/diagnostico_volumen_mensual.py               # eduard, 8 meses
  python scripts/diagnostico_volumen_mensual.py eduard 12     # filtro y meses
  python scripts/diagnostico_volumen_mensual.py lubricantes 6 # toda la línea
"""
from __future__ import annotations

import os
import sys
from datetime import date

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, ".env"))
except ImportError:
    pass

import pandas as pd  # noqa: E402

from src.extractor import extract_invoice_lines  # noqa: E402
from src.odoo_client import OdooClient  # noqa: E402


def main() -> None:
    filtro = sys.argv[1].lower() if len(sys.argv) > 1 else "eduard"
    n_meses = int(sys.argv[2]) if len(sys.argv) > 2 else 8

    fin = pd.Period(date.today(), freq="M")
    inicio = fin - (n_meses - 1)
    d_from = inicio.to_timestamp(how="start").date()
    d_to = fin.to_timestamp(how="end").date()

    client = OdooClient.from_env()
    client.authenticate()
    print(f"✓ Conectado a {client.credentials.url}")
    print(f"Categoría (contiene): '{filtro}' | Meses: {inicio} → {fin}\n")

    df = extract_invoice_lines(client, date_from=d_from, date_to=d_to)
    if df.empty:
        print("Sin líneas de factura en el período.")
        return

    cat = df.get("product_categ_name")
    if cat is None:
        print("Las líneas no traen product_categ_name — revisar extractor.")
        return
    df = df[cat.fillna("").astype(str).str.lower().str.contains(filtro)].copy()
    if df.empty:
        print(f"Ninguna línea con categoría que contenga '{filtro}'.")
        return

    qty = pd.to_numeric(df.get("quantity_base"), errors="coerce").fillna(0)
    vol = pd.to_numeric(df.get("product_volume"), errors="coerce").fillna(0)
    pf = pd.to_numeric(df.get("product_uom_factor"), errors="coerce").fillna(1.0)
    pf = pf.where(pf > 0, 1.0)
    sign = df["move_type"].map({"out_invoice": 1, "out_refund": -1}).fillna(1)

    fecha_col = "invoice_date" if "invoice_date" in df.columns else "date"
    df["_mes"] = pd.to_datetime(df[fecha_col], errors="coerce").dt.to_period("M")
    df["_vol_antes"] = qty * vol * sign
    df["_vol_ahora"] = (qty / pf) * vol * sign
    df["_afectada"] = pf > 1

    tabla = (
        df.groupby("_mes")
        .agg(
            vol_antes=("_vol_antes", "sum"),
            vol_ahora=("_vol_ahora", "sum"),
            lineas=("_vol_antes", "size"),
            lineas_embalaje=("_afectada", "sum"),
        )
        .assign(inflacion_pct=lambda t: (
            (t["vol_antes"] / t["vol_ahora"] - 1) * 100
        ).where(t["vol_ahora"] != 0))
    )
    print("Volumen mensual — fórmula ANTES vs AHORA (corregida):")
    with pd.option_context("display.width", 140,
                           "display.float_format", "{:,.1f}".format):
        print(tabla)

    for col, nombre in (("vol_antes", "ANTES"), ("vol_ahora", "AHORA")):
        s = tabla[col]
        cv = s.std() / s.mean() * 100 if s.mean() else float("nan")
        print(f"\nCoef. de variación {nombre}: {cv:,.1f}% "
              f"(menor = más homogéneo entre meses)")

    sin_vol = df[(vol == 0) & (qty != 0)]
    if not sin_vol.empty:
        n_prod = sin_vol["product_id"].nunique()
        print(f"\n⚠️ {len(sin_vol):,} líneas ({n_prod} productos) con "
              f"`volume` = 0 en la ficha: venden pero no suman volumen.")
        name_col = "product_name" if "product_name" in sin_vol.columns else "product_id"
        top = sin_vol.groupby(name_col)["quantity_base"] if "quantity_base" in sin_vol.columns else None
        ejemplos = sin_vol[name_col].dropna().astype(str).value_counts().head(10)
        print("Top productos sin volumen configurado:")
        for nombre_p, n in ejemplos.items():
            print(f"  - {nombre_p} ({n} líneas)")


if __name__ == "__main__":
    main()
