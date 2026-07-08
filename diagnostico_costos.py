# -*- coding: utf-8 -*-
"""
Diagnóstico de costos en el Informe de Ventas.

Conecta a Odoo, trae las líneas de factura de una fecha (o rango) y muestra
de dónde sale el costo y por qué puede estar quedando en cero.

USO (con el venv activado):
    python diagnostico_costos.py                 # hoy
    python diagnostico_costos.py 2026-07-04       # una fecha
    python diagnostico_costos.py 2026-06-01 2026-06-30   # un rango
"""
from __future__ import annotations

import os
import sys
from datetime import date

# Cargar .env si existe (para ODOO_*)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import pandas as pd

from src.odoo_client import OdooClient
from src.extractor import (
    _resolve_invoice_line_fields,
    extract_invoice_lines,
)

pd.set_option("display.max_columns", None)
pd.set_option("display.width", 200)


def main():
    args = sys.argv[1:]
    if len(args) >= 2:
        d_from = date.fromisoformat(args[0])
        d_to = date.fromisoformat(args[1])
    elif len(args) == 1:
        d_from = d_to = date.fromisoformat(args[0])
    else:
        d_from = d_to = date.today()

    print(f"=== Diagnóstico de costos — {d_from} a {d_to} ===\n")

    client = OdooClient.from_env()
    client.authenticate()
    print(f"✓ Conectado a Odoo: {client.credentials.url}\n")

    # 0) Seleccionar Casa de los Mineros (mismo criterio que el informe).
    from src.extractor import extract_companies
    companies_df = extract_companies(client)
    company_ids = None
    if not companies_df.empty:
        mask = companies_df["name"].str.lower().str.contains(
            "casa de los mineros", na=False,
        )
        match = companies_df[mask]
        row = match.iloc[0] if not match.empty else companies_df.iloc[0]
        company_ids = [int(row["id"])]
        print(f"0) Empresa: {row['name']} (id={company_ids[0]})\n")

    # 1) ¿Qué campos de margen existen en account.move.line?
    fields, margin_fields = _resolve_invoice_line_fields(client)
    print("1) Campos de margen disponibles en account.move.line:")
    print(f"   sale_margin instalado?  ->  {'SÍ' if margin_fields else 'NO'}")
    print(f"   campos detectados: {margin_fields or '(ninguno)'}\n")

    # 2) Traer líneas del período (solo Casa de los Mineros)
    df = extract_invoice_lines(
        client, date_from=d_from, date_to=d_to, company_ids=company_ids,
    )
    if df is None or df.empty:
        print("2) No hay líneas de factura en ese período. Prueba otra fecha.")
        return

    print(f"2) Líneas traídas: {len(df)}\n")

    # 3) De dónde salió el costo
    if "cost_source" in df.columns:
        print("3) Fuente del costo (cost_source):")
        print(df["cost_source"].value_counts().to_string(), "\n")
    if "margin_source" in df.columns:
        print("   Fuente del margen (margin_source):")
        print(df["margin_source"].value_counts().to_string(), "\n")

    # 4) Sumas clave
    def _sum(col):
        return float(pd.to_numeric(df[col], errors="coerce").sum()) if col in df.columns else None

    print("4) Totales del período:")
    for col in ["price_subtotal_signed", "purchase_price",
                "product_standard_price", "line_cost", "line_margin"]:
        val = _sum(col)
        if val is None:
            print(f"   {col:26s}: (columna no existe)")
        else:
            print(f"   {col:26s}: {val:,.2f}")
    print()

    # 5) ¿Cuántas líneas tienen costo 0?
    if "line_cost" in df.columns:
        n0 = int((pd.to_numeric(df["line_cost"], errors="coerce").fillna(0) == 0).sum())
        print(f"5) Líneas con line_cost == 0:  {n0} de {len(df)}")
    if "purchase_price" in df.columns:
        n0pp = int((pd.to_numeric(df["purchase_price"], errors="coerce").fillna(0) == 0).sum())
        print(f"   Líneas con purchase_price == 0:  {n0pp} de {len(df)}")
    if "product_standard_price" in df.columns:
        n0sp = int((pd.to_numeric(df["product_standard_price"], errors="coerce").fillna(0) == 0).sum())
        print(f"   Líneas con product_standard_price == 0:  {n0sp} de {len(df)}")
    print()

    # 6) Muestra de las primeras líneas
    cols = [c for c in [
        "product_default_code", "product_uom_name", "quantity",
        "uom_factor", "quantity_base", "product_standard_price",
        "line_cost", "price_subtotal_signed", "line_margin",
    ] if c in df.columns]
    print("6) Muestra (primeras 15 líneas) — revisa que line_cost = "
          "standard_price × quantity_base:")
    print(df[cols].head(15).to_string(index=False))


if __name__ == "__main__":
    main()
