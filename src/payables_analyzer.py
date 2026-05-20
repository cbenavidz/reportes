# -*- coding: utf-8 -*-
"""
src/payables_analyzer.py
========================
Motor de análisis de Cuentas por Pagar (CxP).

Funciones principales:
  - enrich_payables: une facturas de proveedor con sus términos de pago
    y calcula días para vencer, estado, y descuento por pronto pago.
  - compute_payables_kpis: KPIs globales (total por pagar, vencido, etc.).
  - compute_calendar: calendario de vencimientos próximos.
  - compute_pronto_pago_alerts: facturas con descuento por pago anticipado
    todavía vigente (las que NO se pueden dejar pasar).
  - compute_aging: antigüedad de saldos (corriente / 1-30 / 31-60 / ...).
  - compute_dpo: días promedio de pago a proveedores y rotación de CxP.
  - compute_top_proveedores: ranking por saldo pendiente.
  - compute_cash_flow_projection: cuánto dinero se necesita por semana.

El "pronto pago" se calcula desde los términos de pago de Odoo
(account.payment.term con early_discount / discount_percentage /
discount_days). La fecha límite del descuento es:
    fecha_factura + discount_days
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd


# ===========================================================================
# Enriquecimiento de facturas por pagar
# ===========================================================================


def enrich_payables(
    payables: pd.DataFrame,
    payment_terms: pd.DataFrame | None,
    today: date | None = None,
) -> pd.DataFrame:
    """
    Enriquece las facturas de proveedor abiertas con:
      - dias_para_vencer: días hasta el vencimiento (negativo = vencida)
      - estado: 'Vencida' / 'Vence hoy' / 'Por vencer'
      - bucket_aging: clasificación de antigüedad
      - pronto_pago: bool — el término ofrece descuento por pago anticipado
      - fecha_limite_dto: fecha límite para capturar el descuento
      - dias_para_dto: días hasta perder el descuento (negativo = perdido)
      - monto_descuento: valor del descuento si paga a tiempo
      - estado_dto: 'Vigente' / 'Vence hoy' / 'Perdido' / 'Sin descuento'

    Args:
        payables: DataFrame de extract_payables.
        payment_terms: DataFrame de extract_payment_terms (puede ser None).
        today: fecha de referencia (default = hoy).
    """
    if payables is None or payables.empty:
        return pd.DataFrame()

    if today is None:
        today = date.today()
    today_ts = pd.Timestamp(today)

    df = payables.copy()

    # Saldo pendiente (usar amount_residual; las NC vienen con signo)
    if "amount_residual_signed" in df.columns:
        df["saldo"] = df["amount_residual_signed"]
    elif "amount_residual" in df.columns:
        df["saldo"] = df["amount_residual"]
    else:
        df["saldo"] = 0.0
    df["saldo"] = pd.to_numeric(df["saldo"], errors="coerce").fillna(0.0)

    # Monto total de la factura
    if "amount_total_signed" in df.columns:
        df["total_factura"] = df["amount_total_signed"]
    elif "amount_total" in df.columns:
        df["total_factura"] = df["amount_total"]
    else:
        df["total_factura"] = df["saldo"]
    df["total_factura"] = pd.to_numeric(
        df["total_factura"], errors="coerce"
    ).fillna(0.0)

    # Fechas
    for col in ("invoice_date", "invoice_date_due"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")

    # Si no hay fecha de vencimiento, usar la fecha de factura
    if "invoice_date_due" not in df.columns:
        df["invoice_date_due"] = df.get("invoice_date")
    df["invoice_date_due"] = df["invoice_date_due"].fillna(
        df.get("invoice_date")
    )

    # Días para vencer
    df["dias_para_vencer"] = (
        df["invoice_date_due"] - today_ts
    ).dt.days

    def _estado(d):
        if pd.isna(d):
            return "Sin fecha"
        if d < 0:
            return "Vencida"
        if d == 0:
            return "Vence hoy"
        return "Por vencer"

    df["estado"] = df["dias_para_vencer"].apply(_estado)

    # Bucket de aging (antigüedad de la deuda)
    def _bucket(d):
        if pd.isna(d):
            return "Sin fecha"
        if d >= 0:
            return "Por vencer"
        dd = abs(d)
        if dd <= 30:
            return "1-30 días"
        if dd <= 60:
            return "31-60 días"
        if dd <= 90:
            return "61-90 días"
        return "+90 días"

    df["bucket_aging"] = df["dias_para_vencer"].apply(_bucket)

    # ── Descuento por pronto pago (desde términos de pago Odoo) ──
    terms_map: dict[int, dict] = {}
    if payment_terms is not None and not payment_terms.empty:
        for _, t in payment_terms.iterrows():
            try:
                tid = int(t["id"])
            except (TypeError, ValueError, KeyError):
                continue
            terms_map[tid] = {
                "early_discount": bool(t.get("early_discount", False)),
                "discount_percentage": float(t.get("discount_percentage", 0) or 0),
                "discount_days": int(t.get("discount_days", 0) or 0),
            }

    def _term_field(row, field, default):
        tid = row.get("payment_term_id")
        if tid is None or pd.isna(tid):
            return default
        info = terms_map.get(int(tid))
        if not info:
            return default
        return info.get(field, default)

    df["pronto_pago"] = df.apply(
        lambda r: bool(_term_field(r, "early_discount", False)), axis=1
    )
    df["dto_porcentaje"] = df.apply(
        lambda r: float(_term_field(r, "discount_percentage", 0.0)), axis=1
    )
    df["dto_dias"] = df.apply(
        lambda r: int(_term_field(r, "discount_days", 0)), axis=1
    )

    # Fecha límite del descuento = fecha factura + discount_days
    def _fecha_limite(row):
        if not row.get("pronto_pago"):
            return pd.NaT
        inv_date = row.get("invoice_date")
        if pd.isna(inv_date):
            return pd.NaT
        return inv_date + pd.Timedelta(days=int(row.get("dto_dias", 0)))

    df["fecha_limite_dto"] = df.apply(_fecha_limite, axis=1)
    df["dias_para_dto"] = (
        df["fecha_limite_dto"] - today_ts
    ).dt.days

    # Monto del descuento (sobre el saldo pendiente)
    df["monto_descuento"] = (
        df["saldo"].abs() * df["dto_porcentaje"] / 100.0
    ).where(df["pronto_pago"], 0.0)

    def _estado_dto(row):
        if not row.get("pronto_pago"):
            return "Sin descuento"
        d = row.get("dias_para_dto")
        if pd.isna(d):
            return "Sin descuento"
        if d < 0:
            return "Perdido"
        if d == 0:
            return "Vence hoy"
        return "Vigente"

    df["estado_dto"] = df.apply(_estado_dto, axis=1)

    return df


# ===========================================================================
# KPIs globales
# ===========================================================================


def compute_payables_kpis(
    enriched: pd.DataFrame,
    today: date | None = None,
    horizonte_dias: int = 30,
) -> dict:
    """
    KPIs de cuentas por pagar.

    Devuelve dict con: total_por_pagar, total_vencido, total_por_vencer_h,
    n_facturas, n_proveedores, total_descuentos_vigentes,
    n_facturas_con_descuento, descuento_mas_urgente_dias.
    """
    if enriched is None or enriched.empty:
        return {
            "total_por_pagar": 0.0, "total_vencido": 0.0,
            "total_por_vencer_h": 0.0, "n_facturas": 0,
            "n_proveedores": 0, "total_descuentos_vigentes": 0.0,
            "n_facturas_con_descuento": 0, "descuento_mas_urgente_dias": None,
            "n_vencidas": 0,
        }

    df = enriched
    total = float(df["saldo"].sum())
    vencido = float(df.loc[df["estado"] == "Vencida", "saldo"].sum())
    por_vencer_h = float(df.loc[
        (df["dias_para_vencer"] >= 0)
        & (df["dias_para_vencer"] <= horizonte_dias),
        "saldo",
    ].sum())

    dto_vigentes = df[df["estado_dto"].isin(["Vigente", "Vence hoy"])]
    total_dto = float(dto_vigentes["monto_descuento"].sum())
    n_dto = int(len(dto_vigentes))
    urgente = None
    if not dto_vigentes.empty:
        urgente = int(dto_vigentes["dias_para_dto"].min())

    return {
        "total_por_pagar": total,
        "total_vencido": vencido,
        "total_por_vencer_h": por_vencer_h,
        "n_facturas": int(len(df)),
        "n_vencidas": int((df["estado"] == "Vencida").sum()),
        "n_proveedores": int(df["partner_id"].nunique()),
        "total_descuentos_vigentes": total_dto,
        "n_facturas_con_descuento": n_dto,
        "descuento_mas_urgente_dias": urgente,
    }


# ===========================================================================
# Calendario de vencimientos
# ===========================================================================


def compute_calendar(
    enriched: pd.DataFrame,
    today: date | None = None,
    horizonte_dias: int = 30,
) -> pd.DataFrame:
    """
    Calendario de vencimientos: una fila por día con el monto a pagar,
    el # de facturas y el # de proveedores de ese día.

    Incluye sólo facturas que vencen en [today, today+horizonte].
    """
    if enriched is None or enriched.empty:
        return pd.DataFrame(columns=[
            "fecha", "monto", "n_facturas", "n_proveedores",
        ])
    if today is None:
        today = date.today()
    today_ts = pd.Timestamp(today)
    limite = today_ts + pd.Timedelta(days=horizonte_dias)

    df = enriched.copy()
    df = df[
        (df["invoice_date_due"] >= today_ts)
        & (df["invoice_date_due"] <= limite)
    ]
    if df.empty:
        return pd.DataFrame(columns=[
            "fecha", "monto", "n_facturas", "n_proveedores",
        ])

    df["fecha"] = df["invoice_date_due"].dt.normalize()
    cal = df.groupby("fecha", as_index=False).agg(
        monto=("saldo", "sum"),
        n_facturas=("id", "count"),
        n_proveedores=("partner_id", "nunique"),
    )
    return cal.sort_values("fecha").reset_index(drop=True)


def compute_calendar_semanal(
    enriched: pd.DataFrame,
    today: date | None = None,
    semanas: int = 8,
) -> pd.DataFrame:
    """
    Agrupa los vencimientos por semana (lunes a domingo) para una
    proyección de flujo de caja de pagos.

    Devuelve DataFrame con: semana_inicio, semana_label, monto,
    n_facturas, monto_vencido (lo que ya estaba vencido cae en la
    primera semana).
    """
    if enriched is None or enriched.empty:
        return pd.DataFrame(columns=[
            "semana_inicio", "semana_label", "monto", "n_facturas",
        ])
    if today is None:
        today = date.today()
    today_ts = pd.Timestamp(today)

    # Lunes de la semana actual
    lunes_actual = today_ts - pd.Timedelta(days=today_ts.weekday())
    fin = lunes_actual + pd.Timedelta(weeks=semanas)

    df = enriched.copy()
    df = df[df["invoice_date_due"].notna()]

    rows = []
    for i in range(semanas):
        ini = lunes_actual + pd.Timedelta(weeks=i)
        nxt = ini + pd.Timedelta(weeks=1)
        if i == 0:
            # La primera semana incluye TODO lo vencido + lo que vence
            mask = df["invoice_date_due"] < nxt
        else:
            mask = (df["invoice_date_due"] >= ini) & (df["invoice_date_due"] < nxt)
        sub = df[mask]
        rows.append({
            "semana_inicio": ini,
            "semana_label": (
                "Vencido + " + ini.strftime("%d/%m")
                if i == 0 else ini.strftime("%d/%m")
            ),
            "monto": float(sub["saldo"].sum()),
            "n_facturas": int(len(sub)),
        })
    return pd.DataFrame(rows)


# ===========================================================================
# Alertas de pronto pago
# ===========================================================================


def compute_pronto_pago_alerts(
    enriched: pd.DataFrame,
    incluir_perdidos: bool = False,
) -> pd.DataFrame:
    """
    Devuelve las facturas con descuento por pronto pago, ordenadas por
    urgencia (las que vencen el descuento más pronto, primero).

    Columnas: name, ref, partner_name, invoice_date, fecha_limite_dto,
    dias_para_dto, saldo, dto_porcentaje, monto_descuento, estado_dto.

    Args:
        incluir_perdidos: si True, también muestra descuentos ya perdidos
            (para análisis de oportunidades dejadas pasar).
    """
    if enriched is None or enriched.empty:
        return pd.DataFrame()

    df = enriched[enriched["pronto_pago"]].copy()
    if df.empty:
        return pd.DataFrame()

    if not incluir_perdidos:
        df = df[df["estado_dto"].isin(["Vigente", "Vence hoy"])]

    if df.empty:
        return pd.DataFrame()

    cols = [c for c in [
        "name", "ref", "partner_name", "invoice_date", "invoice_date_due",
        "fecha_limite_dto", "dias_para_dto", "saldo",
        "dto_porcentaje", "monto_descuento", "estado_dto",
    ] if c in df.columns]
    out = df[cols].sort_values("dias_para_dto").reset_index(drop=True)
    return out


# ===========================================================================
# Aging de saldos
# ===========================================================================


def compute_aging(enriched: pd.DataFrame) -> pd.DataFrame:
    """
    Antigüedad de la deuda con proveedores. Devuelve DataFrame con
    bucket, monto, n_facturas y porcentaje del total.
    """
    if enriched is None or enriched.empty:
        return pd.DataFrame(columns=["bucket", "monto", "n_facturas", "pct"])

    orden = ["Por vencer", "1-30 días", "31-60 días", "61-90 días",
             "+90 días", "Sin fecha"]
    grp = enriched.groupby("bucket_aging", as_index=False).agg(
        monto=("saldo", "sum"),
        n_facturas=("id", "count"),
    )
    total = grp["monto"].sum()
    grp["pct"] = (grp["monto"] / total * 100) if total else 0
    grp["_orden"] = grp["bucket_aging"].apply(
        lambda b: orden.index(b) if b in orden else 99
    )
    grp = grp.sort_values("_orden").drop(columns="_orden")
    grp = grp.rename(columns={"bucket_aging": "bucket"})
    return grp.reset_index(drop=True)


# ===========================================================================
# DPO y rotación de cuentas por pagar
# ===========================================================================


def compute_dpo(
    cxp_promedio: float,
    compras_periodo: float,
    dias_periodo: int = 365,
) -> dict:
    """
    Calcula DPO (Days Payable Outstanding) y rotación de CxP.

    DPO = (CxP promedio / Compras del período) × días del período
    Rotación = Compras del período / CxP promedio

    Un DPO alto significa que la empresa tarda más en pagar (más liquidez
    retenida); uno muy bajo puede indicar que no se aprovechan los plazos.

    Args:
        cxp_promedio: saldo promedio de cuentas por pagar.
        compras_periodo: total de compras a crédito del período.
        dias_periodo: días del período (365 = año).
    """
    if compras_periodo <= 0 or cxp_promedio <= 0:
        return {"dpo": 0.0, "rotacion": 0.0}
    dpo = (cxp_promedio / compras_periodo) * dias_periodo
    rotacion = compras_periodo / cxp_promedio
    return {"dpo": round(dpo, 1), "rotacion": round(rotacion, 2)}


# ===========================================================================
# Top proveedores
# ===========================================================================


def compute_top_proveedores(
    enriched: pd.DataFrame,
    top_n: int = 15,
) -> pd.DataFrame:
    """
    Ranking de proveedores por saldo pendiente. Devuelve DataFrame con
    partner_name, saldo_total, saldo_vencido, n_facturas, pct_concentracion.
    """
    if enriched is None or enriched.empty:
        return pd.DataFrame()

    df = enriched.copy()
    df["_vencido"] = df["saldo"].where(df["estado"] == "Vencida", 0.0)
    grp = df.groupby(["partner_id", "partner_name"], as_index=False).agg(
        saldo_total=("saldo", "sum"),
        saldo_vencido=("_vencido", "sum"),
        n_facturas=("id", "count"),
    )
    total = grp["saldo_total"].sum()
    grp["pct_concentracion"] = (
        grp["saldo_total"] / total * 100
    ) if total else 0
    grp = grp.sort_values("saldo_total", ascending=False).reset_index(drop=True)
    if top_n:
        grp = grp.head(top_n)
    return grp


# ===========================================================================
# Detalle ordenado para la tabla principal
# ===========================================================================


def build_tabla_detalle(
    enriched: pd.DataFrame,
) -> pd.DataFrame:
    """
    Tabla detallada de todas las facturas por pagar, ordenada por fecha
    de vencimiento. Para mostrar en la página y exportar.
    """
    if enriched is None or enriched.empty:
        return pd.DataFrame()

    cols = [c for c in [
        "name", "ref", "partner_name",
        "invoice_date", "invoice_date_due", "dias_para_vencer", "estado",
        "total_factura", "saldo", "bucket_aging",
        "payment_term_name", "pronto_pago", "fecha_limite_dto",
        "dias_para_dto", "dto_porcentaje", "monto_descuento", "estado_dto",
        "company_id_name",
    ] if c in enriched.columns]
    out = enriched[cols].copy()
    out = out.sort_values("invoice_date_due", na_position="last")
    return out.reset_index(drop=True)
