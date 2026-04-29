# -*- coding: utf-8 -*-
"""
Diagnóstico: ¿por qué DSO rolling y saldo mensual están en 0
en los meses recientes?

Imprime, mes a mes, cuántas facturas existen, cuántas son a crédito
(según `_is_credit_sale`), cuántas son contado, y el monto total.

Uso:
    source venv/bin/activate
    python diagnostico_historico.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))


def load_env(env_path: Path) -> None:
    if not env_path.exists():
        print(f"❌ No .env en {env_path}")
        sys.exit(1)
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


load_env(ROOT / ".env")

import pandas as pd  # noqa: E402

from src.analyzer import _is_credit_sale  # noqa: E402
from src.extractor import extract_all_for_cartera, extract_companies  # noqa: E402
from src.odoo_client import OdooClient  # noqa: E402


def main() -> None:
    client = OdooClient.from_env()
    client.authenticate()

    comps = extract_companies(client)
    print("Compañías visibles:")
    print(comps[["id", "name"]].to_string(index=False))

    casa_row = comps[comps["name"].str.contains("Mineros", case=False, na=False)]
    if casa_row.empty:
        print("⚠ No encontré 'Casa de los Mineros'. Uso primera compañía.")
        company_id = int(comps.iloc[0]["id"])
    else:
        company_id = int(casa_row.iloc[0]["id"])
    print(f"\n→ Filtrando por company_id={company_id}\n")

    print("Descargando 12 meses…")
    bundle = extract_all_for_cartera(
        client, months_back=12, company_ids=[company_id]
    )
    inv = bundle["invoices"].copy()
    pay = bundle["payments"]
    cutoff = bundle["cutoff_date"]

    print(f"\nFecha de corte: {cutoff}")
    print(f"Facturas extraídas (out_invoice + out_refund): {len(inv)}")
    print(f"Pagos extraídos: {len(pay)}")

    if inv.empty:
        print("❌ No hay facturas. Revisa el filtro de empresa.")
        return

    inv["invoice_date"] = pd.to_datetime(inv["invoice_date"], errors="coerce")
    inv["mes"] = inv["invoice_date"].dt.to_period("M").astype(str)

    is_credit = _is_credit_sale(inv)
    inv["es_credito"] = is_credit
    inv["es_contado"] = ~is_credit & (inv["move_type"] == "out_invoice")

    # Pivote por mes
    by_month = inv.groupby("mes").agg(
        n_total=("id", "count"),
        n_credito=("es_credito", "sum"),
        n_contado=("es_contado", "sum"),
        monto_total=("amount_total_signed", lambda s: s.abs().sum()),
        monto_credito=(
            "amount_total_signed",
            lambda s: s.where(inv.loc[s.index, "es_credito"]).abs().sum(),
        ),
        monto_contado=(
            "amount_total_signed",
            lambda s: s.where(inv.loc[s.index, "es_contado"]).abs().sum(),
        ),
    ).reset_index()

    print("\n" + "=" * 100)
    print("FACTURAS POR MES (selected company)")
    print("=" * 100)
    print(by_month.to_string(index=False))

    # Diagnóstico
    print("\n" + "=" * 100)
    print("DIAGNÓSTICO")
    print("=" * 100)
    meses_recientes = by_month.tail(7)
    print("\nÚltimos 7 meses:")
    print(meses_recientes.to_string(index=False))

    sin_facturas = meses_recientes[meses_recientes["n_total"] == 0]
    sin_credito = meses_recientes[
        (meses_recientes["n_total"] > 0) & (meses_recientes["n_credito"] == 0)
    ]

    if not sin_facturas.empty:
        print(f"\n⚠ Meses recientes SIN facturas: {sin_facturas['mes'].tolist()}")
        print("   → Causa probable: el filtro de empresa o el rango de fechas")
        print("     no está trayendo facturas recientes para esta company.")

    if not sin_credito.empty:
        print(f"\n⚠ Meses con facturas pero TODAS son CONTADO: {sin_credito['mes'].tolist()}")
        print("   → Causa probable: las facturas recientes tienen invoice_date_due")
        print("     == invoice_date (o nulo). El _is_credit_sale las descarta.")
        # Mostrar 5 ejemplos
        for mes in sin_credito["mes"].head(2):
            ej = inv[(inv["mes"] == mes) & (inv["move_type"] == "out_invoice")].head(5)
            print(f"\n  Ejemplos del mes {mes}:")
            print(
                ej[[
                    "name", "invoice_date", "invoice_date_due",
                    "amount_total_signed", "payment_term_name",
                ]].to_string(index=False)
            )

    if sin_facturas.empty and sin_credito.empty:
        print("✓ Hay facturas a crédito en los meses recientes. El problema")
        print("  podría estar en otro lado (cache de Streamlit, normalización).")


if __name__ == "__main__":
    main()
