"""
Smoke test del módulo `sales_analyzer`.

Cubre los puntos críticos:

  1) Filtro usa `invoice_date`, NO `date_order`. Para verificarlo,
     agregamos una columna espuria `date_order` muy distinta y nos
     aseguramos de que el agregado mensual no la consulte.

  2) Notas crédito (`out_refund`) restan de las ventas netas vía
     `amount_total_signed` (que ya viene con signo negativo).

  3) Estados `draft` y `cancel` se excluyen.

  4) `compute_sales_growth` compara correctamente vs el período anterior
     (mismo largo, justo antes).

  5) Pareto por cliente: la suma de `participacion_pct` da 100% y
     `participacion_acum_pct` es monotónica.

  6) Por vendedor: usa `invoice_user_id` como prioridad, fallback a
     `user_id`, "-1" para sin vendedor.

  7) `compute_sales_by_product`: agrega por producto y por categoría
     desde líneas de factura.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd

# Detectar ruta del repo de manera portable: dos niveles arriba de este archivo.
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.sales_analyzer import (  # noqa: E402
    compute_sales_by_partner,
    compute_sales_by_product,
    compute_sales_by_vendedor,
    compute_sales_growth,
    compute_sales_kpis,
    compute_sales_monthly,
    filter_sales_invoices,
)


def build_invoices_fixture() -> pd.DataFrame:
    """
    Universo:
      - 2026-02: 2 facturas (cliente A: 1.000.000, cliente B: 500.000)
      - 2026-03: 3 facturas (A: 800.000, B: 400.000, C: 300.000) +
                 1 NC sobre A: -200.000 (queda neto A=600.000)
      - 2026-04: 2 facturas (A: 700.000, C: 350.000)
      - 1 factura `draft` (no debe contar)
      - 1 factura cancelada (no debe contar)
      - 1 factura con `date_order=2025-01-01` pero `invoice_date=2026-04-15`
        → debe contar para abril 2026 (no enero 2025)
    """
    rows = [
        # Febrero
        dict(id=1, name="FEV001", partner_id=10, partner_name="Cliente A",
             invoice_date=pd.Timestamp("2026-02-05"), invoice_date_due=pd.Timestamp("2026-03-07"),
             date=pd.Timestamp("2026-02-05"), amount_total_signed=1_000_000.0,
             move_type="out_invoice", state="posted", payment_state="paid",
             company_id=1, invoice_user_id=100, user_id=100,
             date_order=pd.Timestamp("2026-01-01"),  # debe ignorarse
             ),
        dict(id=2, name="FEV002", partner_id=20, partner_name="Cliente B",
             invoice_date=pd.Timestamp("2026-02-20"), invoice_date_due=pd.Timestamp("2026-03-22"),
             date=pd.Timestamp("2026-02-20"), amount_total_signed=500_000.0,
             move_type="out_invoice", state="posted", payment_state="paid",
             company_id=1, invoice_user_id=101, user_id=101,
             date_order=pd.Timestamp("2026-01-15"),
             ),
        # Marzo
        dict(id=3, name="FEV003", partner_id=10, partner_name="Cliente A",
             invoice_date=pd.Timestamp("2026-03-10"), invoice_date_due=pd.Timestamp("2026-04-09"),
             date=pd.Timestamp("2026-03-10"), amount_total_signed=800_000.0,
             move_type="out_invoice", state="posted", payment_state="paid",
             company_id=1, invoice_user_id=100, user_id=100,
             date_order=pd.Timestamp("2026-02-01"),
             ),
        dict(id=4, name="FEV004", partner_id=20, partner_name="Cliente B",
             invoice_date=pd.Timestamp("2026-03-15"), invoice_date_due=pd.Timestamp("2026-04-14"),
             date=pd.Timestamp("2026-03-15"), amount_total_signed=400_000.0,
             move_type="out_invoice", state="posted", payment_state="paid",
             company_id=1, invoice_user_id=101, user_id=101,
             date_order=pd.Timestamp("2026-02-15"),
             ),
        dict(id=5, name="FEV005", partner_id=30, partner_name="Cliente C",
             invoice_date=pd.Timestamp("2026-03-25"), invoice_date_due=pd.Timestamp("2026-04-24"),
             date=pd.Timestamp("2026-03-25"), amount_total_signed=300_000.0,
             move_type="out_invoice", state="posted", payment_state="paid",
             company_id=1, invoice_user_id=None, user_id=None,  # sin vendedor
             date_order=pd.Timestamp("2026-03-01"),
             ),
        # NC sobre A en marzo (resta 200.000)
        dict(id=6, name="NC001", partner_id=10, partner_name="Cliente A",
             invoice_date=pd.Timestamp("2026-03-28"), invoice_date_due=pd.Timestamp("2026-03-28"),
             date=pd.Timestamp("2026-03-28"), amount_total_signed=-200_000.0,
             move_type="out_refund", state="posted", payment_state="paid",
             company_id=1, invoice_user_id=100, user_id=100,
             date_order=None,
             ),
        # Abril
        dict(id=7, name="FEV006", partner_id=10, partner_name="Cliente A",
             invoice_date=pd.Timestamp("2026-04-08"), invoice_date_due=pd.Timestamp("2026-05-08"),
             date=pd.Timestamp("2026-04-08"), amount_total_signed=700_000.0,
             move_type="out_invoice", state="posted", payment_state="not_paid",
             company_id=1, invoice_user_id=100, user_id=100,
             date_order=pd.Timestamp("2026-03-01"),
             ),
        dict(id=8, name="FEV007", partner_id=30, partner_name="Cliente C",
             invoice_date=pd.Timestamp("2026-04-15"), invoice_date_due=pd.Timestamp("2026-05-15"),
             date=pd.Timestamp("2026-04-15"), amount_total_signed=350_000.0,
             move_type="out_invoice", state="posted", payment_state="not_paid",
             company_id=1, invoice_user_id=101, user_id=101,
             # date_order extremo: si por error usamos date_order esta venta
             # se iría a 2025-01-01 y abril daría números muy distintos.
             date_order=pd.Timestamp("2025-01-01"),
             ),
        # Draft (no cuenta)
        dict(id=9, name="DRAFT001", partner_id=10, partner_name="Cliente A",
             invoice_date=pd.Timestamp("2026-04-20"), invoice_date_due=pd.Timestamp("2026-05-20"),
             date=pd.Timestamp("2026-04-20"), amount_total_signed=999_999.0,
             move_type="out_invoice", state="draft", payment_state="not_paid",
             company_id=1, invoice_user_id=100, user_id=100,
             date_order=pd.Timestamp("2026-04-01"),
             ),
        # Cancelada (no cuenta)
        dict(id=10, name="CAN001", partner_id=20, partner_name="Cliente B",
             invoice_date=pd.Timestamp("2026-04-22"), invoice_date_due=pd.Timestamp("2026-05-22"),
             date=pd.Timestamp("2026-04-22"), amount_total_signed=999_999.0,
             move_type="out_invoice", state="cancel", payment_state="not_paid",
             company_id=1, invoice_user_id=101, user_id=101,
             date_order=pd.Timestamp("2026-04-15"),
             ),
    ]
    return pd.DataFrame(rows)


def main() -> None:
    invoices = build_invoices_fixture()
    print(f"Universo total: {len(invoices)} registros (8 ventas + 1 draft + 1 cancel)\n")

    # ------------------------------------------------------------------
    # Test 1: filter usa invoice_date, NO date_order
    # ------------------------------------------------------------------
    f_abril = filter_sales_invoices(invoices,
                                    date_from=pd.Timestamp("2026-04-01"),
                                    date_to=pd.Timestamp("2026-04-30"))
    print("=== Test 1: filtro abril 2026 (debe usar invoice_date) ===")
    print(f_abril[["name", "invoice_date", "amount_total_signed", "state"]].to_string(index=False))
    # Deben estar FEV006 y FEV007 (FEV007 tiene date_order=2025-01-01 pero
    # invoice_date=2026-04-15 → cuenta para abril). NO draft ni cancel.
    nombres_abril = set(f_abril["name"])
    assert nombres_abril == {"FEV006", "FEV007"}, (
        f"Abril debe tener exactamente FEV006+FEV007, fueron {nombres_abril}. "
        "Si aparece draft/cancel → estados no filtrados. Si falta FEV007 → "
        "se está usando date_order en lugar de invoice_date."
    )
    print("\n✅ Filtro usa invoice_date (FEV007 con date_order=2025 cae en abril 2026).\n")

    # ------------------------------------------------------------------
    # Test 2: KPIs marzo netean NC
    # ------------------------------------------------------------------
    kpis_mar = compute_sales_kpis(invoices,
                                  date_from=pd.Timestamp("2026-03-01"),
                                  date_to=pd.Timestamp("2026-03-31"))
    print("=== Test 2: KPIs marzo 2026 (NC -200K debe restar) ===")
    print(f"  ventas_brutas    = ${kpis_mar.ventas_brutas:,.0f}  (esperado 1.500.000)")
    print(f"  notas_credito    = ${kpis_mar.notas_credito:,.0f}  (esperado 200.000)")
    print(f"  ventas_netas     = ${kpis_mar.ventas_netas:,.0f}  (esperado 1.300.000)")
    print(f"  n_facturas       = {kpis_mar.n_facturas}            (esperado 3)")
    print(f"  n_notas_credito  = {kpis_mar.n_notas_credito}            (esperado 1)")
    print(f"  ticket_promedio  = ${kpis_mar.ticket_promedio:,.0f}    (esperado 500.000)")
    assert kpis_mar.ventas_brutas == 1_500_000.0
    assert kpis_mar.notas_credito == 200_000.0
    assert kpis_mar.ventas_netas == 1_300_000.0
    assert kpis_mar.n_facturas == 3
    assert kpis_mar.n_notas_credito == 1
    assert kpis_mar.ticket_promedio == 500_000.0
    print("\n✅ KPIs marzo correctos: NC netea, draft/cancel excluidos.\n")

    # ------------------------------------------------------------------
    # Test 3: Mensual
    # ------------------------------------------------------------------
    monthly = compute_sales_monthly(invoices, months=3,
                                    cutoff_date=date(2026, 4, 30))
    print("=== Test 3: tendencia mensual (feb, mar, abr 2026) ===")
    print(monthly[["mes_label", "ventas_brutas", "notas_credito",
                   "ventas_netas", "n_facturas", "var_mom"]].to_string(index=False))
    by_mes = dict(zip(monthly["mes_label"], monthly["ventas_netas"]))
    assert by_mes["2026-02"] == 1_500_000.0
    assert by_mes["2026-03"] == 1_300_000.0  # 1.5M − 200K NC
    assert by_mes["2026-04"] == 1_050_000.0  # 700K + 350K
    # var_mom de marzo vs feb: (1.3M / 1.5M − 1) * 100 = -13.33%
    var_mar = float(monthly[monthly["mes_label"] == "2026-03"]["var_mom"].iloc[0])
    assert abs(var_mar - (-13.333333)) < 0.01, f"var_mom marzo = {var_mar}"
    print("\n✅ Tendencia mensual y var_mom correctos.\n")

    # ------------------------------------------------------------------
    # Test 4: Comparativo período actual vs anterior
    # ------------------------------------------------------------------
    growth = compute_sales_growth(
        invoices,
        date_from=pd.Timestamp("2026-04-01"),
        date_to=pd.Timestamp("2026-04-30"),
    )
    print("=== Test 4: abril 2026 vs marzo 2026 ===")
    print(f"  actual    : ${growth['actual'].ventas_netas:,.0f}   (esperado 1.050.000)")
    print(f"  anterior  : ${growth['anterior'].ventas_netas:,.0f}   (esperado 1.300.000)")
    print(f"  var_pct   : {growth['var_ventas_pct']:.2f}%       (esperado -19.23%)")
    assert growth["actual"].ventas_netas == 1_050_000.0
    assert growth["anterior"].ventas_netas == 1_300_000.0
    assert abs(growth["var_ventas_pct"] - (-19.230769)) < 0.01
    print("\n✅ Comparativo correcto.\n")

    # ------------------------------------------------------------------
    # Test 5: Por cliente (Pareto)
    # ------------------------------------------------------------------
    by_partner = compute_sales_by_partner(
        invoices,
        date_from=pd.Timestamp("2026-02-01"),
        date_to=pd.Timestamp("2026-04-30"),
    )
    print("=== Test 5: ventas por cliente (feb-abr 2026) ===")
    print(by_partner.to_string(index=False))
    # Cliente A: 1.000.000 + 800.000 − 200.000 + 700.000 = 2.300.000
    # Cliente B: 500.000 + 400.000 = 900.000
    # Cliente C: 300.000 + 350.000 = 650.000
    by_id = dict(zip(by_partner["partner_id"], by_partner["ventas_netas"]))
    assert by_id[10] == 2_300_000.0, f"A esperado 2.3M, fue {by_id[10]}"
    assert by_id[20] == 900_000.0
    assert by_id[30] == 650_000.0
    # Suma participaciones = 100
    assert abs(by_partner["participacion_pct"].sum() - 100.0) < 0.001
    # Acumulado monotónico
    assert by_partner["participacion_acum_pct"].is_monotonic_increasing
    # A solo (59.7%) ya entra en Pareto 80
    assert by_partner.iloc[0]["es_pareto_80"]
    print("\n✅ Pareto correcto: participaciones suman 100%, acum monotónico.\n")

    # ------------------------------------------------------------------
    # Test 6: Por vendedor (con fallback)
    # ------------------------------------------------------------------
    nombres = {100: "Juan Pérez", 101: "María Gómez"}
    by_vend = compute_sales_by_vendedor(
        invoices,
        date_from=pd.Timestamp("2026-02-01"),
        date_to=pd.Timestamp("2026-04-30"),
        vendedor_names=nombres,
    )
    print("=== Test 6: ventas por vendedor ===")
    print(by_vend.to_string(index=False))
    # Juan (100): FEV001(1M) + FEV003(800K) + NC001(-200K) + FEV006(700K) = 2.300.000
    # María (101): FEV002(500K) + FEV004(400K) + FEV007(350K) = 1.250.000
    # Sin vendedor (-1): FEV005 = 300.000
    by_vid = dict(zip(by_vend["vendedor_id"], by_vend["ventas_netas"]))
    assert by_vid[100] == 2_300_000.0, f"Juan esperado 2.3M, fue {by_vid[100]}"
    assert by_vid[101] == 1_250_000.0, f"María esperado 1.25M, fue {by_vid[101]}"
    assert by_vid[-1] == 300_000.0, f"Sin vendedor esperado 300K, fue {by_vid[-1]}"
    # Nombre "Sin vendedor" para el id = -1
    sin = by_vend[by_vend["vendedor_id"] == -1].iloc[0]
    assert sin["vendedor_nombre"] == "Sin vendedor"
    print("\n✅ Vendedores correctos (incluyendo 'Sin vendedor').\n")

    # ------------------------------------------------------------------
    # Test 7: Por producto desde líneas
    # ------------------------------------------------------------------
    lines = pd.DataFrame([
        # FEV001 (Cliente A, feb): 2 productos
        dict(id=1, move_id=1, partner_id=10,
             product_id=200, product_name="Café Premium",
             product_categ_id=50, product_categ_name="Bebidas",
             quantity=10, price_subtotal_signed=600_000.0,
             move_type="out_invoice", state="posted",
             invoice_date=pd.Timestamp("2026-02-05"), company_id=1),
        dict(id=2, move_id=1, partner_id=10,
             product_id=201, product_name="Galletas",
             product_categ_id=51, product_categ_name="Snacks",
             quantity=20, price_subtotal_signed=400_000.0,
             move_type="out_invoice", state="posted",
             invoice_date=pd.Timestamp("2026-02-05"), company_id=1),
        # FEV003 (marzo): solo café
        dict(id=3, move_id=3, partner_id=10,
             product_id=200, product_name="Café Premium",
             product_categ_id=50, product_categ_name="Bebidas",
             quantity=15, price_subtotal_signed=800_000.0,
             move_type="out_invoice", state="posted",
             invoice_date=pd.Timestamp("2026-03-10"), company_id=1),
        # NC001 (marzo): nota crédito de café (resta)
        dict(id=4, move_id=6, partner_id=10,
             product_id=200, product_name="Café Premium",
             product_categ_id=50, product_categ_name="Bebidas",
             quantity=-3, price_subtotal_signed=-200_000.0,
             move_type="out_refund", state="posted",
             invoice_date=pd.Timestamp("2026-03-28"), company_id=1),
    ])
    by_prod = compute_sales_by_product(
        lines,
        date_from=pd.Timestamp("2026-02-01"),
        date_to=pd.Timestamp("2026-03-31"),
        group_by="product",
    )
    print("=== Test 7a: ventas por producto (feb-mar) ===")
    print(by_prod.to_string(index=False))
    # Café: 600K + 800K − 200K = 1.200.000 (cant 10+15-3 = 22)
    # Galletas: 400.000 (cant 20)
    by_pid = dict(zip(by_prod["product_id"], by_prod["ventas_netas"]))
    assert by_pid[200] == 1_200_000.0, f"Café neto = {by_pid[200]}"
    assert by_pid[201] == 400_000.0
    cant_cafe = float(by_prod[by_prod["product_id"] == 200]["cantidad"].iloc[0])
    assert cant_cafe == 22.0, f"Cantidad café = {cant_cafe}"

    by_cat = compute_sales_by_product(
        lines,
        date_from=pd.Timestamp("2026-02-01"),
        date_to=pd.Timestamp("2026-03-31"),
        group_by="category",
    )
    print("\n=== Test 7b: ventas por categoría (feb-mar) ===")
    print(by_cat.to_string(index=False))
    by_cid = dict(zip(by_cat["product_categ_id"], by_cat["ventas_netas"]))
    assert by_cid[50] == 1_200_000.0  # Bebidas
    assert by_cid[51] == 400_000.0    # Snacks
    print("\n✅ Productos y categorías correctos.\n")

    print("🎉 Todos los smoke tests de sales_analyzer pasaron.")


if __name__ == "__main__":
    main()
