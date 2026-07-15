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
    cat_filtro = None
    if "--cat" in args:
        i = args.index("--cat")
        cat_filtro = args[i + 1].lower()
        args = args[:i] + args[i + 2:]
    if len(args) >= 2:
        d_from = date.fromisoformat(args[0])
        d_to = date.fromisoformat(args[1])
    elif len(args) == 1:
        d_from = d_to = date.fromisoformat(args[0])
    else:
        d_from = d_to = date.today()

    print(f"=== Diagnóstico de costos — {d_from} a {d_to} ===")
    if cat_filtro:
        print(f"    Filtrando categoría que contenga: '{cat_filtro}'")
    print()

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

    if cat_filtro and "product_categ_name" in df.columns:
        antes = len(df)
        df = df[df["product_categ_name"].fillna("").str.lower()
                .str.contains(cat_filtro)]
        print(f"2) Líneas traídas: {antes} → {len(df)} tras filtrar categoría\n")
        if df.empty:
            print("   No hay líneas de esa categoría en el período.")
            return
    else:
        print(f"2) Líneas traídas: {len(df)}\n")

    # 2b) Unidades de medida en juego (clave para el tema de las cajas)
    if "product_uom_name" in df.columns:
        print("2b) Unidad de medida de las líneas (product_uom_id):")
        print(df["product_uom_name"].fillna("(vacía)").value_counts().to_string())
        if "uom_factor" in df.columns:
            print("\n    Factor de conversión aplicado (unidades por UoM):")
            print(df.groupby(df["product_uom_name"].fillna("(vacía)"))["uom_factor"]
                  .agg(["min", "max"]).to_string())
            n1 = int((pd.to_numeric(df["uom_factor"], errors="coerce") == 1).sum())
            print(f"\n    Líneas con factor = 1 (sin caja): {n1} de {len(df)}")
        print()

    # 2c) Productos con costo cero (inflan el margen)
    if "product_standard_price" in df.columns:
        sp = pd.to_numeric(df["product_standard_price"], errors="coerce").fillna(0)
        cero = df[sp == 0]
        print(f"2c) Líneas con product_standard_price = 0: {len(cero)} de {len(df)}")
        if not cero.empty and "product_name" in cero.columns:
            print("    Productos sin costo configurado:")
            print(cero["product_name"].value_counts().head(15).to_string())
        print()

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

    # 6) Detalle por línea con el margen implícito (los inflados arriba)
    d = df.copy()
    vta = pd.to_numeric(d.get("price_subtotal_signed", 0), errors="coerce").fillna(0)
    cst = pd.to_numeric(d.get("line_cost", 0), errors="coerce").fillna(0)
    qty = pd.to_numeric(d.get("quantity", 0), errors="coerce").replace(0, pd.NA)
    d["margen_%"] = ((vta - cst) / vta.replace(0, pd.NA) * 100).round(1)
    d["precio_unit"] = (vta / qty).round(0)
    # Cuántas veces el precio unitario supera al costo unitario del producto:
    # si sale ~24 o ~36, la cantidad está en CAJAS y el costo en UNIDADES.
    sp = pd.to_numeric(d.get("product_standard_price", 0), errors="coerce")
    d["precio/costo"] = (d["precio_unit"] / sp.replace(0, pd.NA)).round(1)
    cols = [c for c in [
        "product_default_code", "product_uom_name", "quantity",
        "uom_factor", "product_uom_factor", "quantity_base",
        "product_standard_price", "precio_unit", "precio/costo",
        "line_cost", "price_subtotal_signed", "margen_%",
    ] if c in d.columns]
    print("6) Detalle por línea (ordenado por margen; los inflados arriba):\n")
    print("   Cómo leerlo:")
    print("   · uom_factor = 1  → la caja NO está en la unidad de medida.")
    print("   · precio/costo ≈ 24 o 36 → la cantidad viene en CAJAS pero el")
    print("     costo es por UNIDAD: ahí está el margen inflado.")
    print("   · product_standard_price = 0 → producto sin costo en Odoo.\n")
    print(d.sort_values("margen_%", ascending=False)[cols]
          .head(20).to_string(index=False))


if __name__ == "__main__":
    main()
