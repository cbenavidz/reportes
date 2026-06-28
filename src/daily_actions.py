# -*- coding: utf-8 -*-
"""
Helpers compartidos para las acciones de los informes diarios:
- Cargar los datos necesarios para una fecha (caja + ventas).
- Generar el PDF combinado.
- Enviar por correo SMTP.
- Pintar los botones "Descargar PDF" y "Enviar por correo" en una página.

Credenciales SMTP esperadas en `st.secrets["smtp"]`:
    host = "smtp.gmail.com"
    port = 587
    user = "reportes@xxx.com"
    password = "..."   # Gmail App Password
    from_addr = "Casa de los Mineros <reportes@xxx.com>"
    recipients = "a@b.com,c@d.com"   # opcional, default es DEFAULT_RECIPIENTS
"""
from __future__ import annotations

import smtplib
from datetime import date as _date, timedelta
from email.message import EmailMessage

import pandas as pd
import streamlit as st

from src.cash_report import (
    build_saldo_inicial_dict,
    compute_estado_caja,
    get_cash_accounts,
)
from src.daily_pdf import build_daily_pdf
from src.data_loader import (
    load_account_balances_aggregated,
    load_cash_movements_only,
    load_chart_of_accounts,
    load_companies,
    load_invoice_lines,
)
from src.sales_analyzer import (
    compute_sales_by_product,
    compute_sales_kpis_from_lines,
)

DEFAULT_RECIPIENTS = (
    "carlos@casadelosmineros.com.co,mlzorag@gmail.com"
)


# ── Carga consolidada de datos del día ──
def load_daily_dataset(
    fecha: _date, company_ids: tuple[int, ...] | None,
) -> dict:
    """
    Carga todo lo necesario para generar el PDF del día.
    Devuelve dict con: empresa, nit, estado_caja, ventas_kpis, por_cat,
    por_prod.
    """
    fecha_str = fecha.isoformat()
    ant_str = (fecha - timedelta(days=1)).isoformat()

    # ── Caja ──
    chart = load_chart_of_accounts(company_ids=company_ids)
    cash_accs = get_cash_accounts(chart)
    cash_ids = tuple(int(x) for x in cash_accs["id"].tolist()) if not cash_accs.empty else ()
    moves_dia = load_cash_movements_only(
        date_from=fecha_str, date_to=fecha_str,
        company_ids=company_ids, cash_account_ids=cash_ids,
    ) if cash_ids else pd.DataFrame()
    balances = load_account_balances_aggregated(
        date_to=ant_str, company_ids=company_ids,
    )
    saldo_ini = build_saldo_inicial_dict(balances, set(cash_ids))
    estado_caja = compute_estado_caja(chart, moves_dia, saldo_ini, fecha)

    # ── Ventas ──
    lineas = load_invoice_lines(
        company_ids=company_ids, date_from=fecha_str, date_to=fecha_str,
    )
    ventas_kpis = None
    por_cat = pd.DataFrame()
    por_prod = pd.DataFrame()
    if lineas is not None and not lineas.empty:
        ventas_kpis = compute_sales_kpis_from_lines(
            lineas, date_from=fecha, date_to=fecha, company_ids=company_ids,
        )
        por_cat = compute_sales_by_product(
            lineas, group_by="category",
            date_from=fecha, date_to=fecha, company_ids=company_ids,
        )
        por_prod = compute_sales_by_product(
            lineas, group_by="product",
            date_from=fecha, date_to=fecha, company_ids=company_ids,
        )

    # ── Empresa + NIT (de res.company) ──
    companies = load_companies()
    empresa = "Casa de los Mineros"
    nit = ""
    if companies is not None and not companies.empty:
        co_df = companies
        if company_ids:
            co_df = companies[companies["id"].isin(list(company_ids))]
        if not co_df.empty:
            row = co_df.iloc[0]
            empresa = str(row.get("name", empresa))
            nit = str(row.get("vat", "") or row.get("company_registry", "") or "")

    return {
        "fecha": fecha,
        "empresa": empresa,
        "nit": nit,
        "estado_caja": estado_caja,
        "ventas_kpis": ventas_kpis,
        "ventas_por_categoria": por_cat,
        "ventas_por_producto": por_prod,
    }


# ── Generar PDF ──
def build_pdf_for_date(data: dict, top_productos: int = 25) -> bytes:
    return build_daily_pdf(
        fecha=data["fecha"],
        empresa=data["empresa"],
        nit=data["nit"],
        estado_caja=data["estado_caja"],
        ventas_kpis=data["ventas_kpis"],
        ventas_por_categoria=data["ventas_por_categoria"],
        ventas_por_producto=data["ventas_por_producto"],
        top_productos=top_productos,
    )


# ── Envío por correo ──
def _smtp_config() -> dict:
    """Lee la configuración SMTP de st.secrets. Lanza ValueError si falta."""
    try:
        cfg = dict(st.secrets["smtp"])
    except Exception:
        raise ValueError(
            "Falta la sección [smtp] en Streamlit Secrets. Configura "
            "host, port, user, password y from_addr."
        )
    for k in ("host", "port", "user", "password", "from_addr"):
        if not cfg.get(k):
            raise ValueError(f"Falta `{k}` en st.secrets[smtp].")
    return cfg


def send_daily_email(
    pdf_bytes: bytes, fecha: _date,
    recipients: str | list[str] | None = None,
    subject: str | None = None, body: str | None = None,
) -> dict:
    """
    Envía el PDF como adjunto al correo. Devuelve dict con `ok`, `to` y
    `error` (si aplica).
    """
    try:
        cfg = _smtp_config()
    except ValueError as exc:
        return {"ok": False, "to": [], "error": str(exc)}

    if recipients is None:
        recipients = cfg.get("recipients") or DEFAULT_RECIPIENTS
    if isinstance(recipients, str):
        to_list = [x.strip() for x in recipients.split(",") if x.strip()]
    else:
        to_list = list(recipients)

    fecha_str = fecha.strftime("%d/%m/%Y") if isinstance(fecha, _date) else str(fecha)
    subject = subject or f"Informe Diario CDM — {fecha_str}"
    body = body or (
        f"Adjunto el Informe Diario (Estado de Caja + Ventas Diarias) "
        f"correspondiente al {fecha_str}.\n\n"
        "Generado automáticamente por la plataforma de cartera CDM."
    )

    msg = EmailMessage()
    msg["From"] = cfg["from_addr"]
    msg["To"] = ", ".join(to_list)
    msg["Subject"] = subject
    msg.set_content(body)
    filename = (
        f"informe_diario_cdm_"
        f"{fecha.strftime('%Y%m%d') if isinstance(fecha, _date) else fecha}.pdf"
    )
    msg.add_attachment(
        pdf_bytes, maintype="application", subtype="pdf", filename=filename,
    )

    try:
        port = int(cfg["port"])
        with smtplib.SMTP(cfg["host"], port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(cfg["user"], cfg["password"])
            smtp.send_message(msg)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "to": to_list, "error": str(exc)}
    return {"ok": True, "to": to_list, "error": None}


# ── Botones para Streamlit ──
def render_daily_actions(
    fecha: _date, company_ids: tuple[int, ...] | None,
    key_prefix: str = "daily",
) -> None:
    """
    Pinta los botones de descarga y envío en la página. Carga los datos
    al momento de pulsar (usa caché de los loaders).
    """
    st.markdown("---")
    st.markdown("### 📤 Informe diario combinado (PDF)")
    st.caption(
        "Genera un PDF con el Estado de Caja y el Informe de Ventas "
        "Diarias del día seleccionado."
    )

    col1, col2 = st.columns(2)
    pdf_key = f"{key_prefix}_pdf_cache_{fecha.isoformat()}"

    # Precomputar / cachear el PDF en session_state al pulsar generar
    with col1:
        if st.button("📄 Generar PDF", use_container_width=True,
                     key=f"{key_prefix}_gen"):
            with st.spinner("Generando informe diario..."):
                try:
                    data = load_daily_dataset(fecha, company_ids)
                    pdf_bytes = build_pdf_for_date(data)
                    st.session_state[pdf_key] = pdf_bytes
                    st.session_state[f"{pdf_key}_data"] = data
                    st.success(
                        f"PDF generado: {len(pdf_bytes) / 1024:.1f} KB. "
                        "Usa los botones de abajo para descargar o enviar."
                    )
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Error al generar PDF: {exc}")

    pdf_bytes = st.session_state.get(pdf_key)
    if pdf_bytes:
        with col2:
            st.download_button(
                "⬇️ Descargar PDF", data=pdf_bytes,
                file_name=f"informe_diario_cdm_{fecha.strftime('%Y%m%d')}.pdf",
                mime="application/pdf",
                use_container_width=True,
                key=f"{key_prefix}_dl",
            )

        st.markdown("**Enviar por correo**")
        try:
            cfg = _smtp_config()
            default_to = (
                cfg.get("recipients") or DEFAULT_RECIPIENTS
            )
        except ValueError:
            default_to = DEFAULT_RECIPIENTS

        to_input = st.text_input(
            "Destinatarios (separados por coma)",
            value=default_to, key=f"{key_prefix}_to",
        )
        if st.button("✉️ Enviar ahora", key=f"{key_prefix}_send",
                     use_container_width=True):
            with st.spinner("Enviando correo..."):
                res = send_daily_email(
                    pdf_bytes, fecha, recipients=to_input,
                )
            if res["ok"]:
                st.success(
                    "✅ Correo enviado a: " + ", ".join(res["to"])
                )
            else:
                st.error(
                    f"❌ No se pudo enviar: {res['error']}"
                )
