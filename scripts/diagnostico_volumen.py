#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Diagnóstico del cálculo de VOLUMEN físico (galones) en Ventas en Ruta /
Línea Lubricantes.

Compara la fórmula anterior con la corregida:

  ANTES:  volumen = quantity_base × product.volume
  AHORA:  volumen = (quantity_base / product_uom_factor) × product.volume

`product.volume` en Odoo se expresa por 1 unidad de la UoM PROPIA del
producto. Si esa UoM ya es el embalaje ("Caja * 24"), el volume es POR
CAJA y la fórmula anterior lo multiplicaba además por las 24 unidades
sueltas → volumen inflado 24×.

Uso (en el Mac, dentro del venv del proyecto):
  python scripts/diagnostico_volumen.py            # último mes completo
  python scripts/diagnostico_volumen.py 2026-02    # un mes específico
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
    if len(sys.argv) > 1:
        period = pd.Period(sys.argv[1], freq="M")
    else:
        period = pd.Period(date.today(), freq="M") - 1  # último mes completo
    d_from = period.to_timestamp(how="start").date()
    d_to = period.to_timestamp(how="end").date()

    client = OdooClient.from_env()
    client.authenticate()
    print(f"✓ Conectado a {client.credentials.url}")
    print(f"Período analizado: {period} ({d_from} → {d_to})\n")

    df = extract_invoice_lines(client, date_from=d_from, date_to=d_to)
    if df.empty:
        print("Sin líneas de factura en el período.")
        return

    qty = pd.to_numeric(df.get("quantity_base"), errors="coerce").fillna(0)
    vol = pd.to_numeric(df.get("product_volume"), errors="coerce").fillna(0)
    pf = pd.to_numeric(df.get("product_uom_factor"), errors="coerce").fillna(1.0)
    pf = pf.where(pf > 0, 1.0)
    sign = df["move_type"].map({"out_invoice": 1, "out_refund": -1}).fillna(1)

    df["_vol_antes"] = qty * vol * sign
    df["_vol_ahora"] = (qty / pf) * vol * sign

    tot_a, tot_n = df["_vol_antes"].sum(), df["_vol_ahora"].sum()
    print(f"Volumen TOTAL del mes:")
    print(f"  Fórmula anterior : {tot_a:,.1f}")
    print(f"  Fórmula corregida: {tot_n:,.1f}")
    print(f"  Diferencia       : {tot_a - tot_n:,.1f} "
          f"({(tot_a / tot_n - 1) * 100:+.1f}% de inflación)"
          if tot_n else "")

    # Productos cuya UoM propia es un embalaje (factor > 1) — los afectados
    afectados = df[pf > 1].copy()
    print(f"\nLíneas afectadas (UoM propia con factor > 1): "
          f"{len(afectados):,} de {len(df):,}")
    if afectados.empty:
        print("→ Ningún producto tiene UoM propia tipo 'Caja x N'. "
              "Si el volumen sigue mal, el problema está en otro lado "
              "(p.ej. `volume` = 0 o mal capturado en la ficha del producto).")
    else:
        name_col = "product_name" if "product_name" in df.columns else "name"
        top = (
            afectados.groupby(
                afectados[name_col].fillna("—") if name_col in afectados.columns
                else afectados["product_id"]
            )
            .agg(
                factor_uom=("product_uom_factor", "max"),
                vol_producto=("product_volume", "max"),
                qty_base=("quantity_base", "sum"),
                vol_antes=("_vol_antes", "sum"),
                vol_ahora=("_vol_ahora", "sum"),
            )
            .assign(diferencia=lambda t: t["vol_antes"] - t["vol_ahora"])
            .sort_values("diferencia", ascending=False)
            .head(15)
        )
        print("\nTop 15 productos por diferencia de volumen:")
        with pd.option_context("display.width", 160,
                               "display.max_colwidth", 45,
                               "display.float_format", "{:,.1f}".format):
            print(top)

    # Chequeo extra: productos vendidos SIN volumen configurado
    sin_vol = df[(vol == 0) & (qty != 0)]
    if not sin_vol.empty:
        n_prod = sin_vol["product_id"].nunique()
        print(f"\n⚠️ {len(sin_vol):,} líneas ({n_prod} productos) tienen "
              f"`volume` = 0 en la ficha del producto: no suman volumen "
              f"aunque sí venden. Revisar en Odoo si aplica.")


if __name__ == "__main__":
    main()
