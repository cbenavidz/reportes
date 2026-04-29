"""
Smoke test de `classify_invoices_credit_vs_cash`.

Cubre los 3 escenarios clave:

  1) **FEV25583**: factura con plazo nominal +30d pero pagada el mismo día
     (Odoo dejó el default mal). Heurística → "crédito"; clasificador real
     debe corregir a CONTADO.

  2) **FEV25772**: factura con plazo +30d, pagada en 20 días (parcial 31-mar
     + final 7-abr). Heurística → "crédito"; clasificador real → CRÉDITO.

  3) **Factura sin pagar todavía**: sólo aplica heurística nominal → CRÉDITO
     si el plazo > 0.

Además verificamos que `compute_rotation` y `compute_monthly_history` no
incluyan FEV25583 en el pool de crédito (no debe inflar denominadores).
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.analyzer import (  # noqa: E402
    CASH_SALE_THRESHOLD_DAYS,
    classify_invoices_credit_vs_cash,
    compute_effective_due_date,
    compute_monthly_history,
    compute_rotation,
)


def build_estacion_fluvial_mixed() -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Estación Fluvial — casos reales + variantes del payment_term:
      - FEV25583: 200,000 facturada y pagada 12-mar-2026 con payment_term
        "Contado" pero invoice_date_due quedó en 11-abr-2026 (default +30d
        mal calculado). Debe ser CONTADO por payment_term explícito.
      - FEV25772: 689,784 facturada 18-mar-2026, payment_term "30 días",
        pagada 31-mar (300K) + 07-abr (389,784) → 20 días (CRÉDITO real).
      - FEV25800: 100,000 facturada 20-mar-2026 con payment_term "30 días"
        pero pagada el mismo día (cliente quiso pagar inmediato aunque le
        dimos crédito). Debe ser CONTADO por settlement override.
      - FEV25900: 500,000 facturada 15-abr-2026, payment_term "30 días",
        sin pagar todavía (CRÉDITO por payment_term explícito).
      - FEV25950: 50,000 facturada 20-abr-2026 SIN payment_term y due_date
        = invoice_date (cae a heurística → CONTADO).
    """
    invoices = pd.DataFrame([
        # Caso 1: contado por payment_term explícito (invoice_date_due mal)
        {
            "id": 25583, "name": "FEV25583", "partner_id": 999,
            "invoice_date": pd.Timestamp("2026-03-12"),
            "invoice_date_due": pd.Timestamp("2026-04-11"),  # default +30d ¡mal!
            "date": pd.Timestamp("2026-03-12"),
            "amount_total_signed": 200_000.0,
            "amount_residual_signed": 0.0,
            "move_type": "out_invoice", "state": "posted",
            "payment_state": "paid", "company_id": 1,
            "payment_term_name": "Contado",
        },
        # Caso 2: crédito real con payment_term "30 días"
        {
            "id": 25772, "name": "FEV25772", "partner_id": 999,
            "invoice_date": pd.Timestamp("2026-03-18"),
            "invoice_date_due": pd.Timestamp("2026-04-17"),
            "date": pd.Timestamp("2026-03-18"),
            "amount_total_signed": 689_784.0,
            "amount_residual_signed": 0.0,
            "move_type": "out_invoice", "state": "posted",
            "payment_state": "paid", "company_id": 1,
            "payment_term_name": "30 días",
        },
        # Caso 3: payment_term "30 días" PERO pagó mismo día → contado real
        {
            "id": 25800, "name": "FEV25800", "partner_id": 999,
            "invoice_date": pd.Timestamp("2026-03-20"),
            "invoice_date_due": pd.Timestamp("2026-04-19"),
            "date": pd.Timestamp("2026-03-20"),
            "amount_total_signed": 100_000.0,
            "amount_residual_signed": 0.0,
            "move_type": "out_invoice", "state": "posted",
            "payment_state": "paid", "company_id": 1,
            "payment_term_name": "30 días",
        },
        # Caso 4: crédito sin pagar
        {
            "id": 25900, "name": "FEV25900", "partner_id": 999,
            "invoice_date": pd.Timestamp("2026-04-15"),
            "invoice_date_due": pd.Timestamp("2026-05-15"),
            "date": pd.Timestamp("2026-04-15"),
            "amount_total_signed": 500_000.0,
            "amount_residual_signed": 500_000.0,
            "move_type": "out_invoice", "state": "posted",
            "payment_state": "not_paid", "company_id": 1,
            "payment_term_name": "30 días",
        },
        # Caso 5: SIN payment_term, due == invoice → heurística → CONTADO
        {
            "id": 25950, "name": "FEV25950", "partner_id": 999,
            "invoice_date": pd.Timestamp("2026-04-20"),
            "invoice_date_due": pd.Timestamp("2026-04-20"),
            "date": pd.Timestamp("2026-04-20"),
            "amount_total_signed": 50_000.0,
            "amount_residual_signed": 50_000.0,
            "move_type": "out_invoice", "state": "posted",
            "payment_state": "not_paid", "company_id": 1,
            "payment_term_name": None,
        },
    ])

    payments = pd.DataFrame([
        # Pago 1: liquida FEV25583 mismo día → contado real
        {
            "id": 5001, "partner_id": 999,
            "date": pd.Timestamp("2026-03-12"),
            "amount": 200_000.0, "amount_signed": 200_000.0,
            "payment_type": "inbound", "state": "posted",
            "reconciled_invoice_ids": [25583],
        },
        # Pago 2: parcial sobre FEV25772
        {
            "id": 5002, "partner_id": 999,
            "date": pd.Timestamp("2026-03-31"),
            "amount": 300_000.0, "amount_signed": 300_000.0,
            "payment_type": "inbound", "state": "posted",
            "reconciled_invoice_ids": [25772],
        },
        # Pago 3: final sobre FEV25772 → settlement = 7-abr, dias_pago = 20
        {
            "id": 5003, "partner_id": 999,
            "date": pd.Timestamp("2026-04-07"),
            "amount": 389_784.0, "amount_signed": 389_784.0,
            "payment_type": "inbound", "state": "posted",
            "reconciled_invoice_ids": [25772],
        },
        # Pago 4: liquida FEV25800 mismo día (con payment_term "30 días"!)
        # → settlement override debe llevarla a CONTADO
        {
            "id": 5004, "partner_id": 999,
            "date": pd.Timestamp("2026-03-20"),
            "amount": 100_000.0, "amount_signed": 100_000.0,
            "payment_type": "inbound", "state": "posted",
            "reconciled_invoice_ids": [25800],
        },
    ])
    return invoices, payments


def main() -> None:
    invoices, payments = build_estacion_fluvial_mixed()
    print(f"Threshold de contado real: {CASH_SALE_THRESHOLD_DAYS} días\n")

    # ------------------------------------------------------------------
    # Test 1: clasificador
    # ------------------------------------------------------------------
    is_credit = classify_invoices_credit_vs_cash(invoices, payments=payments)
    invoices_dbg = invoices.assign(es_credito_real=is_credit.values)
    print("=== classify_invoices_credit_vs_cash ===")
    print(
        invoices_dbg[["name", "invoice_date", "invoice_date_due",
                      "payment_term_name", "payment_state",
                      "es_credito_real"]].to_string(index=False)
    )

    by_id = dict(zip(invoices["id"], is_credit))
    assert bool(by_id[25583]) is False, (
        f"FEV25583 (payment_term='Contado') debe ser CONTADO, fue {by_id[25583]}"
    )
    assert bool(by_id[25772]) is True, (
        f"FEV25772 (crédito real, 20d) debe ser CRÉDITO, fue {by_id[25772]}"
    )
    assert bool(by_id[25800]) is False, (
        f"FEV25800 (payment_term='30 días' pero pagó día 0) debe ser CONTADO "
        f"por settlement override, fue {by_id[25800]}"
    )
    assert bool(by_id[25900]) is True, (
        f"FEV25900 (payment_term='30 días', sin pagar) debe ser CRÉDITO, "
        f"fue {by_id[25900]}"
    )
    assert bool(by_id[25950]) is False, (
        f"FEV25950 (sin payment_term, due==invoice) debe ser CONTADO por "
        f"heurística, fue {by_id[25950]}"
    )
    print("\n✅ Clasificación correcta con jerarquía de 3 señales:")
    print("   - FEV25583 → CONTADO (payment_term='Contado' override invoice_due mal)")
    print("   - FEV25772 → CRÉDITO (term=30d + gap real = 20d)")
    print("   - FEV25800 → CONTADO (term=30d PERO settlement override gap=0d)")
    print("   - FEV25900 → CRÉDITO (term=30d explícito, no liquidada)")
    print("   - FEV25950 → CONTADO (sin term, heurística due==invoice)\n")

    # ------------------------------------------------------------------
    # Test 2: compute_rotation excluye FEV25583 del pool
    # ------------------------------------------------------------------
    rot = compute_rotation(
        invoices=invoices,
        payments=payments,
        cutoff_date=date(2026, 4, 27),
        period_days=180,
        exclude_cash_sales=True,
    )
    print("=== compute_rotation (exclude_cash=True) ===")
    print(f"  ventas_credito          = ${rot['ventas_credito']:,.0f}")
    print(f"  facturas_credito        = {rot['facturas_credito']}")
    print(f"  facturas_contado_excl.  = {rot['facturas_contado_excluidas']}")

    # Solo deben quedar como crédito: FEV25772 + FEV25900 = 689,784 + 500,000.
    # FEV25583 (term=Contado), FEV25800 (override settlement) y FEV25950 (sin term)
    # son contado y se excluyen.
    expected_credit = 689_784.0 + 500_000.0
    assert abs(rot["ventas_credito"] - expected_credit) < 1.0, (
        f"ventas_credito esperado ~${expected_credit:,.0f}, fue ${rot['ventas_credito']:,.0f}"
    )
    assert rot["facturas_credito"] == 2, (
        f"Deben quedar 2 facturas de crédito (25772 + 25900), fueron {rot['facturas_credito']}"
    )
    assert rot["facturas_contado_excluidas"] == 3, (
        f"Deben excluirse 3 facturas contado (25583, 25800, 25950), "
        f"fueron {rot['facturas_contado_excluidas']}"
    )
    print("\n✅ compute_rotation excluye correctamente las 3 facturas de contado real.\n")

    # ------------------------------------------------------------------
    # Test 3: compute_monthly_history no infla facturado_credito de marzo
    # ------------------------------------------------------------------
    hist = compute_monthly_history(
        invoices=invoices,
        payments=payments,
        months=4,
        cutoff_date=date(2026, 4, 27),
        exclude_cash_sales=True,
        dso_method="payment_days",
    )
    print("=== compute_monthly_history (exclude_cash=True) ===")
    print(hist[["mes_label", "facturado_credito", "cobrado", "dso_rolling"]].to_string(index=False))

    # En marzo: facturado_credito debe ser SOLO 689,784 (FEV25772), NO 889,784 (con FEV25583).
    marzo = hist[hist["mes_label"] == "2026-03"]
    assert not marzo.empty, "Debe existir fila para 2026-03"
    fact_marzo = float(marzo.iloc[0]["facturado_credito"])
    assert abs(fact_marzo - 689_784.0) < 1.0, (
        f"facturado_credito de marzo esperado $689,784, fue ${fact_marzo:,.0f} "
        "— FEV25583 está colándose como crédito."
    )
    print("\n✅ compute_monthly_history no incluye FEV25583 como crédito en marzo.\n")

    # ------------------------------------------------------------------
    # Test 4: sin pagos, payment_term sigue siendo señal confiable
    # ------------------------------------------------------------------
    is_credit_sin_pagos = classify_invoices_credit_vs_cash(invoices, payments=None)
    by_id_sp = dict(zip(invoices["id"], is_credit_sin_pagos))
    # Sin pagos: confiamos en payment_term_name si existe, sino heurística.
    assert bool(by_id_sp[25583]) is False, (
        f"Sin pagos, FEV25583 (term='Contado') debe ser CONTADO por el "
        f"nombre del término. Esperado False, fue {by_id_sp[25583]}"
    )
    assert bool(by_id_sp[25772]) is True  # term '30 días'
    assert bool(by_id_sp[25800]) is True, (
        "Sin pagos NO podemos overridear FEV25800 (term='30 días') a contado. "
        "El override por settlement requiere los pagos."
    )
    assert bool(by_id_sp[25900]) is True
    assert bool(by_id_sp[25950]) is False, (
        "FEV25950 (sin term, due==invoice) → CONTADO por heurística"
    )
    print("✅ Sin pagos:")
    print("   - payment_term explícito sigue siendo confiable (FEV25583, 25772, 25900)")
    print("   - heurística aplica cuando no hay term (FEV25950)")
    print("   - settlement override no aplica (FEV25800 cae al term nominal)\n")

    # ------------------------------------------------------------------
    # Test 5: due efectivo — la mora ya NO se calcula contra el due nominal
    # ------------------------------------------------------------------
    due_eff = compute_effective_due_date(invoices, payments=payments)
    by_id_due = dict(zip(invoices["id"], due_eff))
    invoices_dbg2 = invoices.assign(due_efectivo=due_eff.values)
    print("=== compute_effective_due_date ===")
    print(
        invoices_dbg2[["name", "invoice_date", "invoice_date_due",
                       "due_efectivo"]].to_string(index=False)
    )
    # FEV25583: contado real → due efectivo = invoice_date (12-mar), no 11-abr
    assert by_id_due[25583] == pd.Timestamp("2026-03-12"), (
        f"FEV25583 (contado) → due efectivo debe ser 12-mar, fue {by_id_due[25583]}"
    )
    # FEV25800: contado por settlement override → due efectivo = invoice_date
    assert by_id_due[25800] == pd.Timestamp("2026-03-20"), (
        f"FEV25800 (contado override) → due efectivo debe ser 20-mar, fue {by_id_due[25800]}"
    )
    # FEV25772: crédito real → due efectivo = invoice_date_due (17-abr)
    assert by_id_due[25772] == pd.Timestamp("2026-04-17"), (
        f"FEV25772 (crédito) → due efectivo debe ser 17-abr, fue {by_id_due[25772]}"
    )
    print("\n✅ Due efectivo correcto:")
    print("   - FEV25583/25800/25950 (contado) → due_efectivo = invoice_date (sin plazo)")
    print("   - FEV25772/25900 (crédito) → due_efectivo = invoice_date_due (plazo nominal)")

    # Mora: para FEV25583 (pagada el mismo día → mora = 0, NO −30)
    mora_25583 = (pd.Timestamp("2026-03-12") - by_id_due[25583]).days
    assert mora_25583 == 0, (
        f"FEV25583 (contado pagado mismo día) → mora debe ser 0, fue {mora_25583}"
    )
    print(f"\n✅ Mora FEV25583 (contado pagado el día) = 0 (antes salía −30 días).\n")

    print("🎉 Todos los smoke tests pasaron.")


if __name__ == "__main__":
    main()
