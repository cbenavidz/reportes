import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import sys
sys.path.insert(0, '/sessions/blissful-adoring-clarke/mnt/Cartera casa de los mineros')

from src.analyzer import compute_monthly_history, compute_partner_metrics
from src.scoring import compute_partner_scores

np.random.seed(42)
base_date = datetime(2025, 5, 1)
clients = ['Cliente_A', 'Cliente_B', 'Cliente_C', 'Cliente_D', 'Cliente_E']
invoices_list = []
payments_list = []

for client in clients:
    for month_offset in range(12):
        invoice_date = base_date - timedelta(days=30*month_offset)
        amount = np.random.uniform(50000, 150000)
        due_date = invoice_date + timedelta(days=30)
        invoices_list.append({
            'id': f"{client}_inv_{month_offset}",
            'partner_id': client,
            'partner_name': client,
            'invoice_date': invoice_date,
            'invoice_date_due': due_date,
            'amount_total': amount,
            'amount_total_signed': amount,
            'amount_residual_signed': amount,
            'move_type': 'out_invoice',
            'payment_state': 'posted' if month_offset > 0 else 'draft',
            'payment_terms': 30,
            'date': invoice_date
        })
    
    for month_offset in range(12):
        payment_date = base_date - timedelta(days=30*month_offset + np.random.randint(-15, 45))
        amount = np.random.uniform(40000, 140000)
        payments_list.append({
            'id': f"{client}_pay_{month_offset}",
            'partner_id': client,
            'date': payment_date,
            'amount': amount
        })

invoices = pd.DataFrame(invoices_list)
payments = pd.DataFrame(payments_list)

# Convertir payment_state a 'paid' para algunas facturas
invoices.loc[invoices['payment_state'] != 'draft', 'payment_state'] = 'paid'
invoices['date'] = invoices['invoice_date'] + timedelta(days=np.random.randint(0, 60))

print("✓ DataFrames sintéticos creados")
print(f"  - Invoices: {len(invoices)}, Payments: {len(payments)}")

# TEST 1: compute_monthly_history
try:
    history = compute_monthly_history(invoices, payments, months=12)
    assert len(history) == 12, f"Esperaba 12 filas, obtuve {len(history)}"
    required_cols = ['mes', 'mes_label', 'facturado_credito', 'cobrado', 'saldo_acumulado', 'dso_rolling']
    missing = [c for c in required_cols if c not in history.columns]
    assert not missing, f"Columnas faltantes: {missing}"
    print(f"✓ compute_monthly_history: 12 filas, columnas correctas")
except Exception as e:
    print(f"✗ compute_monthly_history FALLÓ: {e}")
    sys.exit(1)

# TEST 2: compute_partner_metrics
try:
    open_invs = invoices[invoices['payment_state'] != 'paid'].copy()
    metrics = compute_partner_metrics(invoices, open_invs, payments)
    
    required_metric_cols = ['plazo_promedio_dias', 'dso_cliente', 'dias_sobre_plazo', 'cumplimiento_plazo']
    missing_metrics = [c for c in required_metric_cols if c not in metrics.columns]
    assert not missing_metrics, f"Columnas de métricas faltantes: {missing_metrics}"
    print(f"✓ compute_partner_metrics: columnas nuevas presentes")
except Exception as e:
    print(f"✗ compute_partner_metrics FALLÓ: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# TEST 3: compute_partner_scores
try:
    scores = compute_partner_scores(metrics)
    assert 'habito_pago' in scores.columns, "Columna 'habito_pago' no existe"
    print(f"✓ compute_partner_scores: columna 'habito_pago' generada")
except Exception as e:
    print(f"✗ compute_partner_scores FALLÓ: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print("\n✓✓✓ SMOKE TEST COMPLETO - TODO BIEN ✓✓✓")
