#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script standalone para generar el informe diario combinado (Estado de
Caja + Ventas Diarias) y enviarlo por correo.

Pensado para correr en GitHub Actions con cron a las 6:05 PM Bogotá
(23:05 UTC).

Variables de entorno requeridas:
  ODOO_URL, ODOO_DB, ODOO_USERNAME, ODOO_API_KEY
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM
  DAILY_REPORT_TO  (opcional, separado por comas)

Uso:
  python scripts/send_daily_report.py            # informe de hoy
  python scripts/send_daily_report.py 2025-11-15 # informe de fecha exacta
"""
from __future__ import annotations

import os
import smtplib
import sys
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage

# Agregar el root del repo al path para importar src.*
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402

from src.cash_report import (  # noqa: E402
    build_saldo_inicial_dict,
    compute_estado_caja,
    get_cash_accounts,
)
from src.daily_pdf import build_daily_pdf  # noqa: E402
from src.extractor import (  # noqa: E402
    extract_account_movements,
    extract_chart_of_accounts,
    extract_companies,
    extract_invoice_lines,
)
from src.odoo_client import OdooClient  # noqa: E402
from src.sales_analyzer import (  # noqa: E402
    compute_sales_by_product,
    compute_sales_kpis_from_lines,
)

DEFAULT_RECIPIENTS = "carlos@casadelosmineros.com.co,mlzorag@gmail.com"


def _saldos_iniciales(
    client: OdooClient, ant_iso: str, cash_ids: list[int],
    company_ids: list[int] | None = None,
) -> dict[int, float]:
    """Calcula saldo a `ant_iso` para cada cuenta de caja con read_group."""
    if not cash_ids:
        return {}
    domain = [
        ("parent_state", "=", "posted"),
        ("date", "<=", ant_iso),
        ("account_id", "in", cash_ids),
    ]
    if company_ids:
        domain.append(("company_id", "in", list(company_ids)))
    try:
        groups = client.execute_kw(
            "account.move.line", "read_group",
            [domain, ["account_id", "debit:sum", "credit:sum"],
             ["account_id"]],
            {"lazy": False},
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️ read_group para saldos iniciales falló: {exc}")
        return {}
    rows = []
    for g in groups:
        acc = g.get("account_id")
        acc_id = acc[0] if isinstance(acc, list) and acc else acc
        rows.append({
            "account_id": acc_id,
            "debit": float(g.get("debit") or 0),
            "credit": float(g.get("credit") or 0),
        })
    df = pd.DataFrame(rows)
    return build_saldo_inicial_dict(df, set(cash_ids))


def generar_pdf_dia(fecha: date) -> tuple[bytes, str, str]:
    """
    Genera el PDF del día. Devuelve (pdf_bytes, empresa, nit).
    """
    client = OdooClient.from_env()
    client.authenticate()
    print(f"✓ Conectado a Odoo: {client.credentials.url}")

    companies_df = extract_companies(client)
    empresa = "Casa de los Mineros"
    nit = ""
    company_ids: list[int] | None = None
    if not companies_df.empty:
        # El informe diario es SOLO para Casa de los Mineros. Seleccionamos esa
        # empresa por nombre (mismo criterio que el filtro de la app Streamlit).
        # Esto es crítico para el COSTO: `standard_price` depende de la empresa,
        # así que hay que leerlo en el contexto de Casa de los Mineros. Si se
        # usara otra empresa (p.ej. la aseguradora), el costo llegaría en 0.
        mask = companies_df["name"].str.lower().str.contains(
            "casa de los mineros", na=False,
        )
        match = companies_df[mask]
        row = match.iloc[0] if not match.empty else companies_df.iloc[0]
        empresa = str(row.get("name", empresa))
        nit = str(row.get("vat", "") or "")
        if "id" in companies_df.columns:
            company_ids = [int(row["id"])]

    # --- Caja (solo cuentas de Casa de los Mineros) ---
    print("✓ Cargando plan de cuentas y movimientos del día...")
    chart = extract_chart_of_accounts(client, company_ids=company_ids)
    cash_accs = get_cash_accounts(chart)
    cash_ids = [int(x) for x in cash_accs["id"].tolist()] if not cash_accs.empty else []

    moves_dia = extract_account_movements(
        client, date_from=fecha, date_to=fecha, company_ids=company_ids,
    )
    if not moves_dia.empty and cash_ids and "account_id" in moves_dia.columns:
        moves_dia["account_id"] = pd.to_numeric(
            moves_dia["account_id"], errors="coerce",
        )
        moves_dia = moves_dia[moves_dia["account_id"].isin(cash_ids)]

    ant_iso = (fecha - timedelta(days=1)).isoformat()
    saldo_ini = _saldos_iniciales(client, ant_iso, cash_ids, company_ids=company_ids)
    estado_caja = compute_estado_caja(chart, moves_dia, saldo_ini, fecha)
    print(f"  · {len(estado_caja.get('cuentas') or [])} cuentas de caja con saldo/movimientos")

    # --- Ventas ---
    print("✓ Cargando facturas del día...")
    lineas = extract_invoice_lines(
        client, date_from=fecha, date_to=fecha,
        company_ids=company_ids,
    )
    ventas_kpis = None
    por_cat = pd.DataFrame()
    por_prod = pd.DataFrame()
    if lineas is not None and not lineas.empty:
        ventas_kpis = compute_sales_kpis_from_lines(
            lineas, date_from=fecha, date_to=fecha,
            company_ids=company_ids,
        )
        por_cat = compute_sales_by_product(
            lineas, group_by="category",
            date_from=fecha, date_to=fecha,
            company_ids=company_ids,
        )
        por_prod = compute_sales_by_product(
            lineas, group_by="product",
            date_from=fecha, date_to=fecha,
            company_ids=company_ids,
        )
        print(f"  · {len(lineas)} líneas / {ventas_kpis.n_facturas} facturas")

    pdf_bytes = build_daily_pdf(
        fecha=fecha, empresa=empresa, nit=nit,
        estado_caja=estado_caja, ventas_kpis=ventas_kpis,
        ventas_por_categoria=por_cat, ventas_por_producto=por_prod,
    )
    print(f"✓ PDF: {len(pdf_bytes) / 1024:.1f} KB")
    return pdf_bytes, empresa, nit


def enviar_pdf_correo(
    pdf_bytes: bytes, fecha: date, empresa: str,
) -> None:
    """Envía el PDF como adjunto usando SMTP."""
    smtp_host = os.environ["SMTP_HOST"]
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ["SMTP_USER"]
    smtp_pass = os.environ["SMTP_PASSWORD"]
    smtp_from = os.environ.get("SMTP_FROM", smtp_user)
    recipients = os.environ.get("DAILY_REPORT_TO", DEFAULT_RECIPIENTS)
    to_list = [x.strip() for x in recipients.split(",") if x.strip()]

    fecha_str = fecha.strftime("%d/%m/%Y")
    msg = EmailMessage()
    msg["From"] = smtp_from
    msg["To"] = ", ".join(to_list)
    msg["Subject"] = f"Informe Diario {empresa} — {fecha_str}"
    msg.set_content(
        f"Adjunto el Informe Diario (Estado de Caja + Ventas Diarias) "
        f"correspondiente al {fecha_str}.\n\n"
        f"Generado automáticamente al cierre del día (6:05 PM Bogotá)."
    )
    fname = f"informe_diario_cdm_{fecha.strftime('%Y%m%d')}.pdf"
    msg.add_attachment(
        pdf_bytes, maintype="application", subtype="pdf", filename=fname,
    )

    print(f"✓ Enviando a: {', '.join(to_list)}")
    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(smtp_user, smtp_pass)
        smtp.send_message(msg)
    print("✓ Correo enviado correctamente.")


def main():
    # GitHub Actions corre en UTC. Colombia es UTC-5 (sin horario de verano),
    # así que la fecha del informe se calcula en hora de Bogotá. De lo
    # contrario, al correr al final de la tarde (ya de madrugada en UTC) el
    # informe saldría con la fecha del día siguiente.
    bogota = timezone(timedelta(hours=-5))
    fecha = datetime.now(bogota).date()
    if len(sys.argv) > 1:
        fecha = date.fromisoformat(sys.argv[1])
    print(f"=== Informe Diario CDM — {fecha.isoformat()} ===")
    pdf_bytes, empresa, nit = generar_pdf_dia(fecha)
    enviar_pdf_correo(pdf_bytes, fecha, empresa)


if __name__ == "__main__":
    main()
