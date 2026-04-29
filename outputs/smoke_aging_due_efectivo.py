"""
Smoke test: aging y dias_vencido respetan el due EFECTIVO (no nominal).

Caso problemático histórico:
  FEV25583 — facturada 12-mar-2026, payment_term="Contado", pagada el
  mismo día. Pero en Odoo el campo invoice_date_due quedó en 11-abr-2026
  (default +30d aplicado por error).

Antes del fix:
  - dias_vencido = cutoff (27-abr) − 11-abr = +16  → "vencida moderada"
  - aging la metía en rango 1-30
  - monto_vencido la sumaba como deuda de 16 días
  - PERO la factura ya está PAGADA y NO ESTÁ en open_invoices, así que
    el escenario crítico es: ¿qué pasa con FEV25583 si NO se hubiera
    pagado todavía?

Caso del test (más severo):
  Una factura abierta con payment_term="Contado" pero invoice_date_due
  mal puesto a +30d. La nueva metodología:
    - due_efectivo = invoice_date (es contado por payment_term_name)
    - dias_vencido = cutoff − invoice_date (es decir, ya está MUY vencida)
    - cae en el rango correcto del aging
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.analyzer import build_aging_report, compute_days_overdue  # noqa: E402


def main() -> None:
    cutoff = date(2026, 4, 27)

    open_invoices = pd.DataFrame([
        # 1) Contado mal etiquetado: payment_term="Contado" pero due=+30d
        #    Facturada 1-mar, debería estar vencida desde 1-mar (57 días)
        {
            "id": 1, "name": "FEV001", "partner_id": 10, "partner_name": "A",
            "invoice_date": pd.Timestamp("2026-03-01"),
            "invoice_date_due": pd.Timestamp("2026-03-31"),  # +30d falso
            "amount_total_signed": 100_000.0,
            "amount_residual_signed": 100_000.0,
            "move_type": "out_invoice", "state": "posted",
            "payment_state": "not_paid", "company_id": 1,
            "payment_term_name": "Contado",
        },
        # 2) Crédito real: term="30 días", facturada 1-abr, vence 1-may.
        #    Hoy (27-abr) NO debería estar vencida.
        {
            "id": 2, "name": "FEV002", "partner_id": 10, "partner_name": "A",
            "invoice_date": pd.Timestamp("2026-04-01"),
            "invoice_date_due": pd.Timestamp("2026-05-01"),
            "amount_total_signed": 500_000.0,
            "amount_residual_signed": 500_000.0,
            "move_type": "out_invoice", "state": "posted",
            "payment_state": "not_paid", "company_id": 1,
            "payment_term_name": "30 días",
        },
        # 3) Crédito real vencido: term="30 días", facturada 15-feb, due=17-mar.
        #    Hoy (27-abr) → 41 días vencida.
        {
            "id": 3, "name": "FEV003", "partner_id": 20, "partner_name": "B",
            "invoice_date": pd.Timestamp("2026-02-15"),
            "invoice_date_due": pd.Timestamp("2026-03-17"),
            "amount_total_signed": 300_000.0,
            "amount_residual_signed": 300_000.0,
            "move_type": "out_invoice", "state": "posted",
            "payment_state": "not_paid", "company_id": 1,
            "payment_term_name": "30 días",
        },
    ])

    # ------------------------------------------------------------------
    # Test 1: compute_days_overdue
    # ------------------------------------------------------------------
    res = compute_days_overdue(open_invoices, cutoff_date=cutoff)
    print("=== compute_days_overdue (con due efectivo) ===")
    print(res[["name", "invoice_date", "invoice_date_due",
               "fecha_vencimiento_efectiva", "dias_vencido", "esta_vencida"]]
          .to_string(index=False))

    by_id = dict(zip(res["id"], res["dias_vencido"]))
    by_due = dict(zip(res["id"], res["fecha_vencimiento_efectiva"]))

    # FEV001 (contado mal etiquetado): due efectivo = invoice_date (1-mar)
    # → dias_vencido = (27-abr − 1-mar) = 57
    assert by_due[1] == pd.Timestamp("2026-03-01"), (
        f"FEV001 due efectivo debe ser 1-mar (invoice_date), fue {by_due[1]}. "
        "Si fue 31-mar → la nueva metodología NO se aplicó al aging."
    )
    assert by_id[1] == 57, (
        f"FEV001 dias_vencido esperado 57, fue {by_id[1]}. "
        "El aging sigue usando el debe nominal +30d."
    )

    # FEV002 (crédito real, no vencida): due = 1-may, dias = -4 (corriente)
    assert by_due[2] == pd.Timestamp("2026-05-01")
    assert by_id[2] == -4, f"FEV002 esperado -4 días, fue {by_id[2]}"
    assert not res.loc[res["id"] == 2, "esta_vencida"].iloc[0]

    # FEV003 (crédito real, vencida): due = 17-mar, dias = 41
    assert by_due[3] == pd.Timestamp("2026-03-17")
    assert by_id[3] == 41, f"FEV003 esperado 41 días, fue {by_id[3]}"
    assert res.loc[res["id"] == 3, "esta_vencida"].iloc[0]

    print("\n✅ compute_days_overdue usa due efectivo correctamente:")
    print("   - FEV001 (contado mal): 57 días vencida (era −4 con due nominal)")
    print("   - FEV002 (crédito): -4 días (corriente)")
    print("   - FEV003 (crédito vencido): 41 días vencida\n")

    # ------------------------------------------------------------------
    # Test 2: build_aging_report
    # ------------------------------------------------------------------
    aging = build_aging_report(open_invoices, cutoff_date=cutoff)
    print("=== build_aging_report ===")
    print(aging.to_string(index=False))

    # Saldo total: 100K + 500K + 300K = 900K
    # FEV001 (57 días) → rango 31-60
    # FEV002 (-4 días) → rango "Corriente" (no vencida)
    # FEV003 (41 días) → rango 31-60
    # → rango 31-60 debe tener 2 facturas y 400K (100K + 300K)
    rango_31_60 = aging[aging["rango"].astype(str).str.contains("31-60", regex=False)]
    if not rango_31_60.empty:
        n = int(rango_31_60.iloc[0]["num_facturas"])
        m = float(rango_31_60.iloc[0]["monto"])
        print(f"\n  Rango 31-60: {n} facturas, ${m:,.0f}")
        assert n == 2, f"31-60 debe tener 2 facturas, tuvo {n}"
        assert abs(m - 400_000) < 1, f"31-60 debe sumar 400K, sumó {m}"

    print("\n✅ Aging report distribuye correctamente con due efectivo.\n")

    # ------------------------------------------------------------------
    # Test 3: pasamos payments → no rompe nada
    # ------------------------------------------------------------------
    payments_empty = pd.DataFrame(columns=[
        "id", "partner_id", "date", "amount", "amount_signed",
        "payment_type", "state", "reconciled_invoice_ids",
    ])
    res2 = compute_days_overdue(
        open_invoices, cutoff_date=cutoff, payments=payments_empty,
    )
    assert (res2["dias_vencido"] == res["dias_vencido"]).all(), (
        "compute_days_overdue debe ser idempotente con payments vacíos"
    )
    aging2 = build_aging_report(
        open_invoices, cutoff_date=cutoff, payments=payments_empty,
    )
    assert aging.equals(aging2)
    print("✅ Idempotente con payments vacíos.\n")

    print("🎉 Todos los smoke tests de aging+due_efectivo pasaron.")


if __name__ == "__main__":
    main()
