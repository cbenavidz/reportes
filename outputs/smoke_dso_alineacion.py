"""
Smoke test: el DSO de `compute_partner_metrics` (página de Scoring)
debe ser idéntico al DSO que muestra Detalle Cliente, que es el promedio
real `mean(settlement_date - invoice_date)` sobre las facturas pagadas
del cliente, EXCLUYENDO contado.

Caso reportado por el negocio:
  Taller Germotos en Detalle Cliente → DSO 43-45 días.
  Taller Germotos en Scoring → DSO 30 días (fórmula contable saldo/ventas).
  Esto confundía al usuario porque los dos números deberían ser el mismo.

Después del fix:
  compute_partner_metrics → dso_cliente = dias_pago_promedio
                          = mean(settlement - invoice_date)
                          = lo mismo que Detalle Cliente.

Construimos un cliente sintético con dos facturas pagadas, una a 30d
y otra a 60d, y verificamos que el DSO promedio sea 45 (no 30 ni 60).
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.analyzer import (  # noqa: E402
    _compute_invoice_settlement_dates,
    compute_partner_metrics,
)


def main() -> None:
    cutoff = date(2026, 4, 27)

    # Cliente Taller Germotos: 2 facturas pagadas a crédito.
    # Una pagada en 30 días, otra en 60 días → DSO real promedio = 45 días.
    invoices = pd.DataFrame([
        {
            "id": 101, "name": "FEV-A", "partner_id": 999, "partner_name": "Taller Germotos",
            "invoice_date": pd.Timestamp("2026-01-15"),
            "invoice_date_due": pd.Timestamp("2026-02-14"),
            "amount_total_signed": 1_000_000.0,
            "amount_residual_signed": 0.0,
            "move_type": "out_invoice", "state": "posted",
            "payment_state": "paid", "company_id": 1,
            "payment_term_name": "30 días",
        },
        {
            "id": 102, "name": "FEV-B", "partner_id": 999, "partner_name": "Taller Germotos",
            "invoice_date": pd.Timestamp("2026-02-01"),
            "invoice_date_due": pd.Timestamp("2026-03-03"),
            "amount_total_signed": 2_000_000.0,
            "amount_residual_signed": 0.0,
            "move_type": "out_invoice", "state": "posted",
            "payment_state": "paid", "company_id": 1,
            "payment_term_name": "30 días",
        },
        # Factura abierta para que tenga saldo HOY (que la fórmula vieja
        # saldo/ventas usaba para calcular DSO 30d).
        {
            "id": 103, "name": "FEV-C", "partner_id": 999, "partner_name": "Taller Germotos",
            "invoice_date": pd.Timestamp("2026-04-01"),
            "invoice_date_due": pd.Timestamp("2026-05-01"),
            "amount_total_signed": 250_000.0,
            "amount_residual_signed": 250_000.0,
            "move_type": "out_invoice", "state": "posted",
            "payment_state": "not_paid", "company_id": 1,
            "payment_term_name": "30 días",
        },
    ])

    # Pagos: A pagada 30d después, B pagada 60d después.
    payments = pd.DataFrame([
        {
            "id": 1, "partner_id": 999,
            "date": pd.Timestamp("2026-02-14"),  # 30d después de FEV-A
            "amount": 1_000_000.0, "amount_signed": 1_000_000.0,
            "payment_type": "inbound", "state": "posted",
            "reconciled_invoice_ids": [101],
        },
        {
            "id": 2, "partner_id": 999,
            "date": pd.Timestamp("2026-04-02"),  # 60d después de FEV-B
            "amount": 2_000_000.0, "amount_signed": 2_000_000.0,
            "payment_type": "inbound", "state": "posted",
            "reconciled_invoice_ids": [102],
        },
    ])

    open_invoices = invoices[invoices["payment_state"].isin(["not_paid", "partial"])].copy()

    # ------------------------------------------------------------------
    # 1) Calcular DSO al estilo Detalle Cliente
    # ------------------------------------------------------------------
    settlement = _compute_invoice_settlement_dates(
        invoices=invoices,
        payments=payments,
        exclude_cash_sales=True,  # mismo flag que usa Detalle Cliente
    )
    sp = settlement[settlement["partner_id"] == 999]
    dso_detalle = float(sp["dias_pago"].mean())
    print(f"DSO Detalle Cliente (mean dias_pago): {dso_detalle:.1f} días")
    print(f"  facturas en pool: {len(sp)}")
    print(f"  dias_pago individuales: {sp['dias_pago'].tolist()}")

    # ------------------------------------------------------------------
    # 2) Calcular DSO al estilo Scoring (compute_partner_metrics)
    # ------------------------------------------------------------------
    by_partner = compute_partner_metrics(
        invoices=invoices,
        open_invoices=open_invoices,
        payments=payments,
        cutoff_date=cutoff,
        exclude_cash_sales=True,
    )
    germotos = by_partner[by_partner["partner_id"] == 999].iloc[0]
    dso_scoring = float(germotos["dso_cliente"])
    print(f"\nDSO Scoring (dso_cliente): {dso_scoring:.1f} días")
    print(f"  saldo_actual: ${germotos['saldo_actual']:,.0f}")
    print(f"  num_facturas_pagadas: {germotos['num_facturas_pagadas']}")
    print(f"  plazo_promedio_dias: {germotos['plazo_promedio_dias']:.1f}")

    # ------------------------------------------------------------------
    # 3) Asserts
    # ------------------------------------------------------------------
    assert abs(dso_scoring - 45.0) < 0.5, (
        f"DSO Scoring debería ser 45 (promedio de 30d y 60d), fue {dso_scoring}. "
        "Si salió 30 → la fórmula vieja saldo/ventas sigue activa. "
        "Si salió 0 → no se está pasando settlement_date al promedio."
    )
    assert abs(dso_scoring - dso_detalle) < 0.1, (
        f"DSO Scoring ({dso_scoring}) debe ser idéntico a DSO Detalle "
        f"Cliente ({dso_detalle}). La página de Scoring y la de Detalle "
        "Cliente NO están alineadas."
    )

    print(f"\n✅ DSO Scoring = DSO Detalle Cliente = {dso_scoring:.1f} días")
    print("   (eran fórmulas distintas; ahora ambas usan mean(dias_pago))")
    print("\n🎉 Smoke test de alineación de DSO pasó.")


if __name__ == "__main__":
    main()
