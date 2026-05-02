# -*- coding: utf-8 -*-
"""
Diagnóstico fino: ¿por qué mi app dice $187M para Yarley + Lubricantes en
abril cuando Odoo dice $183.5M para Yarley en TODAS categorías?
Es imposible que solo Lubricantes sea más que el total → hay un filtro mal.

Compara contra el reporte oficial de Odoo (Análisis de facturas):
    Yarley abril 2026 = $183,518,478.54
    Luis Felipe abril 2026 = $194,720,356.95
"""
import sys
from datetime import date
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.data_loader import get_odoo_client  # noqa: E402
from src.extractor import extract_invoice_lines, extract_invoices  # noqa: E402


def main() -> None:
    client = get_odoo_client()

    # 1) Líneas + facturas en abril 2026
    df = extract_invoice_lines(
        client, date_from=date(2026, 4, 1), date_to=date(2026, 4, 30),
    )
    inv = extract_invoices(
        client, date_from=date(2026, 4, 1), date_to=date(2026, 4, 30),
    )

    print(f"Líneas crudas: {len(df)} | Facturas crudas: {len(inv)}")
    print()

    # 2) Filtros
    if "company_name" in df.columns:
        print("=== Distribución líneas por empresa ===")
        print(df["company_name"].value_counts())
        print()
    if "company_id_name" in inv.columns:
        print("=== Distribución facturas por empresa ===")
        print(inv["company_id_name"].value_counts())
        print()

    # 3) Filtrar Yarley solo (invoice_user_id)
    mask_yarley = inv["invoice_user_id_name"].astype(str).str.lower().str.contains("yarley", na=False)
    inv_yarley = inv[mask_yarley]
    print(f"Facturas de Yarley (todas empresas): {len(inv_yarley)}")
    if "company_id_name" in inv_yarley.columns:
        print("  Por empresa:")
        print(inv_yarley["company_id_name"].value_counts())
    print()

    # 4) Filtrar también por Casa de los Mineros
    if "company_id_name" in inv_yarley.columns:
        inv_yarley_casa = inv_yarley[
            inv_yarley["company_id_name"].str.contains("Casa de los Mineros", case=False, na=False)
        ]
    else:
        inv_yarley_casa = inv_yarley
    print(f"Facturas Yarley + Casa de los Mineros: {len(inv_yarley_casa)}")

    # 5) Suma total Yarley + Casa (TODAS categorías) vs Odoo $183.5M
    total_amount_untaxed = inv_yarley_casa["amount_untaxed_signed"].sum()
    print(f"  amount_untaxed_signed total: ${total_amount_untaxed:,.2f}")
    print(f"  Odoo dice:                   $183,518,478.54")
    print(f"  Diferencia:                  ${total_amount_untaxed - 183518478.54:,.2f}")
    print()

    # 6) Líneas de las facturas de Yarley + Casa
    moves_yarley_casa = set(inv_yarley_casa["id"].astype(int).tolist())
    lines_yarley_casa = df[df["move_id"].astype(int).isin(moves_yarley_casa)]
    print(f"Líneas de Yarley + Casa: {len(lines_yarley_casa)}")
    total_lineas = lines_yarley_casa["price_subtotal_signed"].sum()
    print(f"  Suma price_subtotal_signed: ${total_lineas:,.2f}")
    print()

    # 7) Por categoría
    print("=== Por categoría (Yarley + Casa de los Mineros, abril 2026) ===")
    by_cat = (
        lines_yarley_casa.groupby("product_categ_name", dropna=False)
        ["price_subtotal_signed"].agg(["sum", "count"])
        .sort_values("sum", ascending=False)
    )
    print(by_cat.to_string())
    print()

    # 8) Lubricantes solamente
    CATS = [
        "CMIN / LUBRICANTES EDUARDOÑO",
        "CMIN / LUBRICANTES INCOLMOTOS",
        "CMIN / LUBRICANTES CASTROL",
    ]
    sub_lubri = lines_yarley_casa[lines_yarley_casa["product_categ_name"].isin(CATS)]
    print(f"Líneas SOLO Lubricantes (3 cat): {len(sub_lubri)}")
    print(f"  Suma: ${sub_lubri['price_subtotal_signed'].sum():,.2f}")
    print()

    # 9) Después de excluir SOAT/ANTCL
    sub_no_soat = lines_yarley_casa[
        ~lines_yarley_casa["product_default_code"].astype(str).str.upper().isin(["SOAT1", "ANTCL"])
    ]
    print(f"Líneas excluyendo SOAT/ANTCL: {len(sub_no_soat)}")
    print(f"  Suma: ${sub_no_soat['price_subtotal_signed'].sum():,.2f}")


if __name__ == "__main__":
    main()
