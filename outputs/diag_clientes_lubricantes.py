# -*- coding: utf-8 -*-
"""
Diagnóstico: cuenta de clientes con compras de Lubricantes en abril 2026.

Compara contra el número que da el reporte oficial de Odoo (76 según
captura del usuario). Lista los clientes con NETA <= 0 que NO deberían
contar pero que mi app cuenta.

Uso:
    ./venv/bin/python3 outputs/diag_clientes_lubricantes.py
"""
import sys
from datetime import date
from pathlib import Path

# Agregar el repo al path para que encuentre `src`
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.data_loader import get_odoo_client  # noqa: E402
from src.extractor import extract_invoice_lines  # noqa: E402


def main() -> None:
    client = get_odoo_client()

    # 1) Líneas de factura — solo Casa de los Mineros (igual que la app)
    df = extract_invoice_lines(
        client,
        date_from=date(2026, 4, 1),
        date_to=date(2026, 4, 30),
    )
    print(f"Líneas crudas en abril (todas las empresas): {len(df)}")
    if "company_name" in df.columns:
        df = df[df["company_name"].str.contains("Casa de los Mineros", case=False, na=False)]
        print(f"Líneas en abril SOLO Casa de los Mineros: {len(df)}")

    # 2) Cargar facturas (account.move) para mapear move_id → invoice_user_id
    from src.extractor import extract_invoices
    invoices = extract_invoices(
        client,
        date_from=date(2026, 4, 1),
        date_to=date(2026, 4, 30),
    )
    print(f"Facturas crudas en abril (todas las empresas): {len(invoices)}")
    if "company_id_name" in invoices.columns:
        invoices = invoices[
            invoices["company_id_name"].str.contains("Casa de los Mineros", case=False, na=False)
        ]
        print(f"Facturas en abril SOLO Casa de los Mineros: {len(invoices)}")

    # 3) Filtrar SOLO por Yarley Vanessa (la tabla de Odoo era de ella)
    vendedores_target = ["yarley"]
    inv_user_col = "invoice_user_id_name" if "invoice_user_id_name" in invoices.columns else "user_id_name"
    user_id_col = "invoice_user_id" if "invoice_user_id" in invoices.columns else "user_id"

    if inv_user_col in invoices.columns:
        mask_vend = invoices[inv_user_col].astype(str).str.lower().apply(
            lambda n: any(t in n for t in vendedores_target)
        )
        ids_vendedores = (
            invoices.loc[mask_vend, user_id_col].dropna().astype(int).unique().tolist()
        )
        print(f"User IDs detectados (Yarley/Luis Felipe): {ids_vendedores}")
        moves_vendedores = (
            invoices.loc[mask_vend, "id"].dropna().astype(int).tolist()
        )
        print(f"Facturas de esos vendedores: {len(moves_vendedores)}")

        # Filtrar líneas por move_id en moves_vendedores
        df = df[df["move_id"].astype(int).isin(moves_vendedores)]
        print(f"Líneas después de filtrar vendedor: {len(df)}")

    CATS = [
        "CMIN / LUBRICANTES EDUARDOÑO",
        "CMIN / LUBRICANTES INCOLMOTOS",
        "CMIN / LUBRICANTES CASTROL",
    ]
    sub = df[df["product_categ_name"].isin(CATS)]
    print(f"Líneas finales (vendedor + 3 cat. Lubricantes): {len(sub)}")

    agg = (
        sub.groupby(["partner_id", "partner_name"])
        .agg(
            venta_neta=("price_subtotal_signed", "sum"),
            n_lineas_invoice=("move_type", lambda s: (s == "out_invoice").sum()),
            n_lineas_nc=("move_type", lambda s: (s == "out_refund").sum()),
        )
        .reset_index()
        .sort_values("partner_name")
    )

    print()
    print(f"Total clientes con CUALQUIER movimiento: {len(agg)}")
    print(f"Solo con venta neta > 0:                  {(agg['venta_neta'] > 0).sum()}")
    print(f"Solo con venta neta == 0 (devolvió todo): {(agg['venta_neta'] == 0).sum()}")
    print(f"Solo con venta neta < 0 (NC > factura):   {(agg['venta_neta'] < 0).sum()}")
    print()

    # Verificar estados (sospecha de drafts)
    if "parent_state" in sub.columns:
        print("=== Distribución por parent_state ===")
        print(sub["parent_state"].value_counts())
        print()

    print("=== LISTA COMPLETA de clientes (compara con tu Excel de Odoo) ===")
    print("(Los nombres ordenados alfabéticamente, igual que tu tabla de Odoo)")
    print()
    for i, row in enumerate(agg.itertuples(index=False), start=1):
        print(f"{i:3d}. {row.partner_name}")
    print()
    print(f"TOTAL: {len(agg)} clientes")
    print()
    print("=== CLIENTES con NETA <= 0 (deberían NO contar) ===")
    print(agg[agg["venta_neta"] <= 0].to_string(index=False))
    print()
    print("=== CLIENTES con NETA > 0 que SOLO tienen NC (sin invoice) ===")
    print(
        agg[(agg["venta_neta"] > 0) & (agg["n_lineas_invoice"] == 0)]
        .to_string(index=False)
    )


if __name__ == "__main__":
    main()
