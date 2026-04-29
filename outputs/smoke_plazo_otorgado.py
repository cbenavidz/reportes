"""
Smoke test: el "plazo otorgado promedio" debe reflejar el plazo NOMINAL
del contrato, NO el due efectivo post-reclasificación.

Caso reportado por el negocio (Emiro Parra):
  Sus facturas tienen plazo 30d nominal en la tabla de Detalle Cliente,
  pero el KPI "Plazo otorgado prom." y el campo "Plazo otorg." de Scoring
  mostraban 16d. La diferencia: muchas facturas se pagaban en ≤3d, así
  que `compute_effective_due_date` las reclasificaba a CONTADO y el plazo
  efectivo se desplomaba a 0d, jalando el promedio hacia abajo.

  El usuario espera ver el plazo que LE OTORGÓ al cliente (30d), no el
  que terminó "siendo" después de reclasificar por comportamiento.

Después del fix:
  - `due_otorgado` (sin payments) → solo respeta payment_term_name. Se usa
    para "plazo otorgado".
  - `due_efectivo` (con payments) → además override por settlement ≤3d.
    Se usa para mora y % a tiempo.

Construimos un cliente con 5 facturas plazo 30d, donde 3 se pagaron rápido
(2-3 días) y 2 se pagaron a 30d. Verificamos:
  - plazo_promedio_dias = 30 (no 12 = (3*0 + 2*30)/5)
  - dias_pago_promedio (DSO) sigue reflejando el comportamiento real.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.analyzer import compute_partner_metrics  # noqa: E402


def main() -> None:
    cutoff = date(2026, 4, 27)

    # 5 facturas todas a plazo nominal 30d, payment_term="30 días".
    invoices = []
    for i, (inv_d, due_d) in enumerate([
        ("2026-01-15", "2026-02-14"),
        ("2026-01-20", "2026-02-19"),
        ("2026-02-01", "2026-03-03"),
        ("2026-02-15", "2026-03-17"),
        ("2026-03-01", "2026-03-31"),
    ], start=1):
        invoices.append({
            "id": 200 + i,
            "name": f"FEV-{i:03d}",
            "partner_id": 777,
            "partner_name": "Emiro Parra",
            "invoice_date": pd.Timestamp(inv_d),
            "invoice_date_due": pd.Timestamp(due_d),
            "amount_total_signed": 100_000.0 * i,
            "amount_residual_signed": 0.0,
            "move_type": "out_invoice",
            "state": "posted",
            "payment_state": "paid",
            "company_id": 1,
            "payment_term_name": "30 días",
        })
    invoices_df = pd.DataFrame(invoices)
    open_inv = invoices_df.iloc[0:0]

    # 3 facturas pagadas RÁPIDO (2-3d), 2 pagadas a 30d.
    payments = pd.DataFrame([
        # Pagadas rápido — settlement-override las reclasificaba a CONTADO
        {"id": 1, "partner_id": 777, "date": pd.Timestamp("2026-01-17"),
         "amount": 100_000.0, "amount_signed": 100_000.0,
         "payment_type": "inbound", "state": "posted",
         "reconciled_invoice_ids": [201]},
        {"id": 2, "partner_id": 777, "date": pd.Timestamp("2026-01-22"),
         "amount": 200_000.0, "amount_signed": 200_000.0,
         "payment_type": "inbound", "state": "posted",
         "reconciled_invoice_ids": [202]},
        {"id": 3, "partner_id": 777, "date": pd.Timestamp("2026-02-04"),
         "amount": 300_000.0, "amount_signed": 300_000.0,
         "payment_type": "inbound", "state": "posted",
         "reconciled_invoice_ids": [203]},
        # Pagadas a 30d
        {"id": 4, "partner_id": 777, "date": pd.Timestamp("2026-03-17"),
         "amount": 400_000.0, "amount_signed": 400_000.0,
         "payment_type": "inbound", "state": "posted",
         "reconciled_invoice_ids": [204]},
        {"id": 5, "partner_id": 777, "date": pd.Timestamp("2026-03-31"),
         "amount": 500_000.0, "amount_signed": 500_000.0,
         "payment_type": "inbound", "state": "posted",
         "reconciled_invoice_ids": [205]},
    ])

    by_partner = compute_partner_metrics(
        invoices=invoices_df,
        open_invoices=open_inv,
        payments=payments,
        cutoff_date=cutoff,
        exclude_cash_sales=False,  # incluimos contado para que entren TODAS
    )
    emiro = by_partner[by_partner["partner_id"] == 777].iloc[0]

    plazo = float(emiro["plazo_promedio_dias"])
    dso = float(emiro["dso_cliente"])
    nfact = int(emiro["num_facturas_pagadas"])

    print(f"# facturas en pool: {nfact}")
    print(f"plazo_promedio_dias: {plazo:.1f}  (nominal era 30 en todas)")
    print(f"dso_cliente: {dso:.1f}  (real: 3 a 2-3d, 2 a 30d → promedio ~13)")

    # El plazo otorgado debe ser ~30 (NOMINAL), aunque pagaran rápido
    assert abs(plazo - 30.0) < 0.5, (
        f"plazo_promedio_dias debe ser ~30 (nominal), fue {plazo}. "
        "Si salió ~12 → el override por settlement sigue contaminando el "
        "plazo otorgado (debe usarse due_otorgado, no due_efectivo)."
    )

    # El DSO sí refleja el comportamiento real (mezcla rápidos y lentos)
    # 3 a ~2d, 2 a ~30d → promedio ≈ (3*2 + 2*30) / 5 = 13.2
    assert 10 < dso < 16, (
        f"DSO debe reflejar el comportamiento real (~13d), fue {dso}"
    )

    print(f"\n✅ Plazo otorgado = {plazo:.1f}d (nominal, sin afectarse por pagos rápidos)")
    print(f"✅ DSO = {dso:.1f}d (sí refleja que pagan rápido)")
    print("\n🎉 Smoke test plazo_otorgado pasó.")


if __name__ == "__main__":
    main()
