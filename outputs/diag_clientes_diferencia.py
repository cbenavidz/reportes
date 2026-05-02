# -*- coding: utf-8 -*-
"""
Identifica los clientes que aparecen en el conteo "81" de la app pero NO
en el "76" del reporte de Odoo (clientes asignados a Yarley en su ficha
pero cuya factura de abril la emitió otro vendedor).

Uso:
    ./venv/bin/python3 outputs/diag_clientes_diferencia.py
"""
import sys
from datetime import date
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.data_loader import get_odoo_client  # noqa: E402
from src.extractor import extract_invoice_lines, extract_invoices, extract_partners  # noqa: E402


def main() -> None:
    client = get_odoo_client()

    # 1) Líneas y facturas en abril, solo Casa de los Mineros
    df = extract_invoice_lines(
        client, date_from=date(2026, 4, 1), date_to=date(2026, 4, 30),
    )
    if "company_name" in df.columns:
        df = df[df["company_name"].str.contains("Casa de los Mineros", case=False, na=False)]

    invoices = extract_invoices(
        client, date_from=date(2026, 4, 1), date_to=date(2026, 4, 30),
    )
    if "company_id_name" in invoices.columns:
        invoices = invoices[
            invoices["company_id_name"].str.contains("Casa de los Mineros", case=False, na=False)
        ]

    # 2) Identificar user_id de Yarley
    inv_user_col = "invoice_user_id_name"
    user_id_col = "invoice_user_id"
    mask_yarley = invoices[inv_user_col].astype(str).str.lower().str.contains("yarley", na=False)
    yarley_user_ids = invoices.loc[mask_yarley, user_id_col].dropna().astype(int).unique().tolist()
    print(f"User IDs de Yarley: {yarley_user_ids}")

    # 3) Partners cuyo user_id es Yarley (asignación en res.partner)
    partners = extract_partners(client)
    asignados_yarley = set(
        partners.loc[
            pd.to_numeric(partners["user_id"], errors="coerce").isin(yarley_user_ids),
            "id",
        ].dropna().astype(int).tolist()
    )
    print(f"Clientes asignados a Yarley en su ficha: {len(asignados_yarley)}")

    # 4) Filtrar líneas de Lubricantes en abril
    CATS = [
        "CMIN / LUBRICANTES EDUARDOÑO",
        "CMIN / LUBRICANTES INCOLMOTOS",
        "CMIN / LUBRICANTES CASTROL",
    ]
    sub = df[df["product_categ_name"].isin(CATS)]

    # 5) Partners en los 81 = unión (asignados a Yarley) + (Yarley facturó)
    moves_yarley = set(invoices.loc[mask_yarley, "id"].dropna().astype(int).tolist())

    partners_facturados_por_yarley = set(
        sub[sub["move_id"].astype(int).isin(moves_yarley)]["partner_id"]
        .dropna().astype(int).unique().tolist()
    )
    print(f"Clientes que Yarley facturó (Lubricantes abril): {len(partners_facturados_por_yarley)}")

    partners_asignados_con_compra = set(
        sub[sub["partner_id"].isin(asignados_yarley)]["partner_id"]
        .dropna().astype(int).unique().tolist()
    )
    print(f"Clientes asignados a Yarley con compra Lubricantes abril: {len(partners_asignados_con_compra)}")

    # Unión = los 81 que mostraba la app (vieja lógica)
    union = partners_facturados_por_yarley | partners_asignados_con_compra
    print(f"Unión (los 81 de la app vieja): {len(union)}")

    # Diferencia = los que estaban asignados pero NO los facturó Yarley
    diferencia = partners_asignados_con_compra - partners_facturados_por_yarley
    print(f"\n=== Los {len(diferencia)} clientes que SOBRABAN (asignados a Yarley pero ella NO los facturó) ===")
    print()

    # Para cada uno, mostrar quién SÍ los facturó
    for pid in diferencia:
        nombre = sub[sub["partner_id"] == pid]["partner_name"].iloc[0] if not sub[sub["partner_id"] == pid].empty else "—"
        moves_de_este = sub[sub["partner_id"] == pid]["move_id"].astype(int).unique().tolist()
        emisores = invoices[invoices["id"].isin(moves_de_este)][inv_user_col].dropna().unique().tolist()
        venta = sub[sub["partner_id"] == pid]["price_subtotal_signed"].sum()
        print(f"  • {nombre}")
        print(f"    Venta abril: ${venta:,.0f}")
        print(f"    Quien le facturó: {', '.join(emisores) if emisores else '—'}")
        print()


if __name__ == "__main__":
    main()
