# -*- coding: utf-8 -*-
"""
Smoke test: validar que credit_limit y days_sales_outstanding
se resuelven correctamente al pasar context con allowed_company_ids,
y que partners no listados como customer_rank>0 también se traen
cuando aparecen en facturas.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Cargar .env
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

from src.odoo_client import OdooClient  # noqa: E402
from src.extractor import (  # noqa: E402
    extract_companies,
    extract_invoices,
    extract_partners,
    extract_all_for_cartera,
)


def main() -> None:
    client = OdooClient.from_env()
    client.authenticate()

    print("=" * 70)
    print("Compañías visibles para el usuario:")
    print("=" * 70)
    comps = extract_companies(client)
    print(comps[["id", "name"]].to_string(index=False))

    # Buscar Casa de los Mineros
    casa_row = comps[comps["name"].str.contains("Mineros", case=False, na=False)]
    if casa_row.empty:
        print("⚠ No encontré 'Casa de los Mineros' por nombre, uso primera compañía.")
        company_id = int(comps.iloc[0]["id"])
    else:
        company_id = int(casa_row.iloc[0]["id"])
    company_ids = [company_id]
    print(f"\n→ Usando company_id={company_id} ({comps[comps['id']==company_id]['name'].iloc[0]})")

    # ------------------------------------------------------------------
    # Buscar 'Estacion Fluvial del Atrato' por nombre
    # ------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("Buscando partner 'Estacion Fluvial del Atrato'…")
    print("=" * 70)
    matches = client.search_read(
        "res.partner",
        domain=[("name", "ilike", "estacion fluvial")],
        fields=["id", "name", "customer_rank", "company_id"],
        limit=10,
    )
    if not matches:
        print("⚠ Partner no encontrado por nombre. Pruebo con 'atrato'…")
        matches = client.search_read(
            "res.partner",
            domain=[("name", "ilike", "atrato")],
            fields=["id", "name", "customer_rank", "company_id"],
            limit=10,
        )
    for m in matches:
        print(f"  id={m['id']:>6}  rank={m.get('customer_rank',0):>3}  "
              f"company={m.get('company_id')}  name={m['name']}")

    if not matches:
        print("❌ No pude localizar el partner. Salgo.")
        sys.exit(1)

    target = matches[0]
    target_id = int(target["id"])
    print(f"\n→ target_id={target_id} ({target['name']})")

    # ------------------------------------------------------------------
    # PASO 1: leer SIN context (comportamiento previo). Esperamos 0.
    # ------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("PASO 1: read SIN context (comportamiento anterior)")
    print("=" * 70)
    rec_no_ctx = client.read(
        "res.partner",
        [target_id],
        fields=["id", "name", "credit_limit", "days_sales_outstanding",
                "use_partner_credit_limit", "credit"],
    )
    if rec_no_ctx:
        r = rec_no_ctx[0]
        print(f"  credit_limit              = {r.get('credit_limit')}")
        print(f"  days_sales_outstanding    = {r.get('days_sales_outstanding')}")
        print(f"  use_partner_credit_limit  = {r.get('use_partner_credit_limit')}")
        print(f"  credit (saldo actual)     = {r.get('credit')}")

    # ------------------------------------------------------------------
    # PASO 2: leer CON context allowed_company_ids
    # ------------------------------------------------------------------
    print("\n" + "=" * 70)
    print(f"PASO 2: read CON context allowed_company_ids={company_ids}")
    print("=" * 70)
    rec_ctx = client.read(
        "res.partner",
        [target_id],
        fields=["id", "name", "credit_limit", "days_sales_outstanding",
                "use_partner_credit_limit", "credit"],
        context={"allowed_company_ids": company_ids},
    )
    if rec_ctx:
        r = rec_ctx[0]
        print(f"  credit_limit              = {r.get('credit_limit')}")
        print(f"  days_sales_outstanding    = {r.get('days_sales_outstanding')}")
        print(f"  use_partner_credit_limit  = {r.get('use_partner_credit_limit')}")
        print(f"  credit (saldo actual)     = {r.get('credit')}")

    # ------------------------------------------------------------------
    # PASO 3: probar extract_all_for_cartera completo (flujo real)
    # ------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("PASO 3: extract_all_for_cartera con company_ids")
    print("=" * 70)
    bundle = extract_all_for_cartera(
        client, months_back=6, company_ids=company_ids
    )
    partners = bundle["partners"]
    print(f"  Partners traídos: {len(partners)}")
    print(f"  Facturas: {len(bundle['invoices'])}, "
          f"Abiertas: {len(bundle['open_invoices'])}, "
          f"Pagos: {len(bundle['payments'])}")

    row = partners[partners["id"] == target_id]
    if row.empty:
        print(f"  ❌ El partner {target_id} NO está en partners (no aparecía en invoices/payments del rango)")
    else:
        r = row.iloc[0]
        print(f"  ✓ Partner encontrado en bundle:")
        print(f"    name                      = {r.get('name')}")
        print(f"    credit_limit              = {r.get('credit_limit')}")
        print(f"    days_sales_outstanding    = {r.get('days_sales_outstanding')}")
        print(f"    use_partner_credit_limit  = {r.get('use_partner_credit_limit')}")
        print(f"    credit                    = {r.get('credit')}")

    print("\n" + "=" * 70)
    print("✅ Smoke test completado")
    print("=" * 70)


if __name__ == "__main__":
    main()
