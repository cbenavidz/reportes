# -*- coding: utf-8 -*-
"""
Motor de análisis de cartera.

Calcula:
- Rotación de cartera (en días y veces) por período.
- Aging report (antigüedad de saldos por rangos).
- Días vencidos por factura.
- Métricas globales y por cliente.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable

import numpy as np
import pandas as pd

from .config import get_data_floor_date

logger = logging.getLogger(__name__)


# Rangos de aging por defecto. Pueden sobreescribirse vía .env o UI.
DEFAULT_AGING_RANGES: list[tuple[str, int, int]] = [
    ("Corriente (no vencido)", -10_000, 0),
    ("1 - 30 días", 1, 30),
    ("31 - 60 días", 31, 60),
    ("61 - 90 días", 61, 90),
    ("91 - 180 días", 91, 180),
    ("Más de 180 días", 181, 100_000),
]


@dataclass
class CarteraMetrics:
    """Resultado consolidado del análisis de cartera."""

    cutoff_date: date
    saldo_cartera: float = 0.0
    saldo_cartera_promedio: float = 0.0
    ventas_credito_periodo: float = 0.0
    rotacion_veces: float = 0.0
    rotacion_dias: float = 0.0
    dso: float = 0.0  # Days Sales Outstanding (igual que rotación en días)
    facturas_abiertas: int = 0
    facturas_vencidas: int = 0
    monto_vencido: float = 0.0
    pct_vencido: float = 0.0
    clientes_con_saldo: int = 0
    facturas_credito_periodo: int = 0
    facturas_contado_excluidas: int = 0
    exclude_cash_sales: bool = True
    aging: pd.DataFrame = field(default_factory=pd.DataFrame)
    open_invoices: pd.DataFrame = field(default_factory=pd.DataFrame)
    by_partner: pd.DataFrame = field(default_factory=pd.DataFrame)


# ---------------------------------------------------------------------------
# Cálculos
# ---------------------------------------------------------------------------


def compute_days_overdue(
    open_invoices: pd.DataFrame,
    cutoff_date: date | None = None,
    payments: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    Añade columna 'dias_vencido' al DataFrame de facturas abiertas.

    dias_vencido > 0 -> factura vencida.
    dias_vencido <= 0 -> factura corriente.

    ⚠️ FECHA DE VENCIMIENTO USADA — `due_efectivo` (no el `invoice_date_due`
    nominal). Esto corrige el bug histórico de Odoo donde una factura de
    contado (pagada el mismo día) quedaba con `invoice_date_due = invoice_date
    + 30d` por el plazo por defecto del partner. Con la nueva metodología:
      - Si la factura es de contado (por payment_term o por settlement real),
        el due efectivo = invoice_date → mora se mide contra ese día.
      - Si es de crédito real, el due efectivo = invoice_date_due nominal.

    Pasa `payments` para que el override por settlement aplique en facturas
    parciales (cuando ya hay liquidación parcial registrada).
    """
    if open_invoices.empty:
        return open_invoices.assign(dias_vencido=pd.Series(dtype="float"))

    cutoff = pd.Timestamp(cutoff_date or datetime.now().date())
    df = open_invoices.copy()

    # Due efectivo según la jerarquía:
    #   1) heurística nominal (due > invoice_date)
    #   2) override por payment_term_name ("Contado" → due = invoice_date)
    #   3) override por settlement real (si hay pago liquidador)
    # Si no hay invoice_date_due ni invoice_date, queda NaT y dias_vencido=NaN.
    due = compute_effective_due_date(df, payments=payments)
    df["dias_vencido"] = (cutoff - due).dt.days
    df["fecha_vencimiento_efectiva"] = due
    df["esta_vencida"] = df["dias_vencido"] > 0
    return df


def build_aging_report(
    open_invoices: pd.DataFrame,
    cutoff_date: date | None = None,
    ranges: list[tuple[str, int, int]] | None = None,
    payments: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    Construye el reporte de antigüedad de saldos por rango.

    Devuelve DataFrame con columnas: rango, num_facturas, monto, pct_total.

    ⚠️  Usa due_efectivo (vía compute_days_overdue → compute_effective_due_date)
    para que las facturas de contado mal etiquetadas en Odoo no caigan en
    rangos artificialmente "corrientes".
    """
    ranges = ranges or DEFAULT_AGING_RANGES
    if open_invoices.empty:
        return pd.DataFrame(columns=["rango", "num_facturas", "monto", "pct_total"])

    df = compute_days_overdue(open_invoices, cutoff_date, payments=payments)
    saldo = df["amount_residual_signed"].abs()
    total = saldo.sum() or 1.0  # Evita división por cero

    rows = []
    for label, lo, hi in ranges:
        mask = (df["dias_vencido"] >= lo) & (df["dias_vencido"] <= hi)
        rows.append(
            {
                "rango": label,
                "min_dias": lo,
                "max_dias": hi,
                "num_facturas": int(mask.sum()),
                "monto": float(saldo[mask].sum()),
                "pct_total": float(saldo[mask].sum() / total * 100),
            }
        )

    return pd.DataFrame(rows)


def _is_credit_sale(invoices: pd.DataFrame) -> pd.Series:
    """
    Heurística NOMINAL: True si la factura tiene plazo > 0 (due > invoice_date).

    Esta es la clasificación basada únicamente en el plazo declarado en Odoo.
    Falla cuando el plazo por defecto del cliente está mal configurado
    (Odoo a veces pone +30d aunque la operación fue de contado). Para una
    clasificación robusta usar `classify_invoices_credit_vs_cash` que combina
    esta heurística con el comportamiento real de pago.

    Una factura es de contado/pago inmediato (False) cuando:
        - No tiene fecha de vencimiento (invoice_date_due es nula), o
        - invoice_date_due == invoice_date (mismo día).
    """
    if invoices.empty:
        return pd.Series([], dtype=bool)
    inv_date = pd.to_datetime(invoices.get("invoice_date"), errors="coerce")
    due_date = pd.to_datetime(invoices.get("invoice_date_due"), errors="coerce")
    # Es a crédito si tiene fecha de vencimiento posterior a la fecha de factura
    return due_date.notna() & (due_date > inv_date)


# Umbral por defecto para considerar una factura "pagada al instante" (contado
# real) sin importar qué diga el plazo nominal. 3 días absorben el desfase de
# transferencias bancarias / consignaciones de fin de semana.
CASH_SALE_THRESHOLD_DAYS = 3

# Patrones que identifican un `account.payment.term` como "pago inmediato /
# contado" por nombre. Cubre español (Colombia) e inglés. Se aplica con
# `str.contains(..., case=False, regex=True)`, así que solo importa el
# subtoken; nombres como "Pago al contado", "Contado / Inmediato",
# "Immediate Payment", "Cash on delivery", etc. matchean.
CASH_TERM_NAME_REGEX = r"\b(?:contado|inmediato|immediate|cash)\b|al ingreso|al recibir"


def classify_invoices_credit_vs_cash(
    invoices: pd.DataFrame,
    payments: pd.DataFrame | None = None,
    cash_threshold_days: int = CASH_SALE_THRESHOLD_DAYS,
) -> pd.Series:
    """
    Clasifica cada factura como CRÉDITO (True) o CONTADO (False) combinando
    tres señales en orden de confiabilidad creciente.

    Devuelve un boolean Series con el MISMO índice que `invoices`.

    Jerarquía de señales
    --------------------
    1) **Heurística nominal** (`_is_credit_sale`): `due > invoice_date` →
       crédito. Es la base cuando no tenemos información mejor.

    2) **Plazo del documento (`payment_term_name`)** — más confiable que la
       heurística. Es el `account.payment.term` realmente asignado a la
       factura (no el default del cliente). Si el nombre matchea
       `CASH_TERM_NAME_REGEX` (Contado / Pago inmediato / Immediate / Cash)
       se trata como CONTADO. Si el nombre existe y NO matchea contado, se
       trata como CRÉDITO. Esto corrige los casos donde Odoo derivó un
       `invoice_date_due` con desfase incorrecto del default del partner.

    3) **Comportamiento real (`settlement_date`)** — la verdad operativa.
       Si la factura está liquidada y el gap
       `(settlement_date − invoice_date).days ≤ cash_threshold_days`,
       overridea a CONTADO. Si el gap es mayor, overridea a CRÉDITO. Para
       fines de DSO y rotación pesa el flujo real, no la intención.

    Cuando una factura no tiene `payment_term_name` ni está liquidada, el
    resultado es la heurística pura del paso 1.

    Por qué importa
    ---------------
    Caso real (Estación Fluvial — FEV25583): factura y pago el mismo día,
    pero el `invoice_date_due` en Odoo quedó a +30d porque el partner tenía
    un default mal puesto. Sin este helper la factura se contaba como
    crédito e inflaba el DSO. Ahora:
      - Si su `payment_term_name` dice "Contado" → CONTADO inmediatamente.
      - Si dice "30 días" pero pagó el mismo día → settlement override → CONTADO.
      - En ambos casos sale del pool de crédito.
    """
    if invoices is None or invoices.empty:
        return pd.Series([], dtype=bool)

    # ------------------------------------------------------------------
    # Paso 1: base con la heurística nominal (due > invoice_date).
    # ------------------------------------------------------------------
    result = _is_credit_sale(invoices).copy()

    # ------------------------------------------------------------------
    # Paso 2: refinar con `payment_term_name` (intención del documento).
    # Tiene más peso que la heurística porque captura el plazo realmente
    # asignado a la factura, no el default del cliente.
    # ------------------------------------------------------------------
    if "payment_term_name" in invoices.columns:
        names = (
            invoices["payment_term_name"]
            .astype("string")
            .fillna("")
            .str.strip()
        )
        is_cash_term = names.str.contains(
            CASH_TERM_NAME_REGEX, case=False, regex=True, na=False
        )
        has_term = names != ""
        is_explicit_credit_term = has_term & ~is_cash_term

        # `mask(cond, other)` reemplaza con `other` donde `cond` es True.
        # Si el término es de contado → False (CONTADO).
        result = result.mask(is_cash_term, False)
        # Si el término es explícitamente de plazo > 0 → True (CRÉDITO).
        result = result.mask(is_explicit_credit_term, True)

    # ------------------------------------------------------------------
    # Paso 3: override por comportamiento real (si hay pagos).
    # Esta es la verdad operativa para fines de cartera/DSO.
    # ------------------------------------------------------------------
    if payments is None or payments.empty or "id" not in invoices.columns:
        return result

    try:
        settlement = _compute_invoice_settlement_dates(
            invoices=invoices,
            payments=payments,
            exclude_cash_sales=False,
        )
    except Exception:  # noqa: BLE001 — no romper el pipeline por esto
        logger.exception("classify_invoices: falló settlement, usando nominal")
        return result

    if settlement.empty:
        return result

    # Mapear invoice_id → dias_pago real.
    # Deduplicamos por invoice_id (puede haber duplicados si se concatenan
    # extracts de varias empresas o por race conditions del cargue): nos
    # quedamos con el último settlement registrado, que es el más reciente.
    settlement_unique = settlement.drop_duplicates(
        subset="invoice_id", keep="last"
    )
    real_days = pd.to_numeric(
        settlement_unique.set_index("invoice_id")["dias_pago"], errors="coerce"
    )
    if not real_days.index.is_unique:  # safety net
        real_days = real_days.groupby(level=0).max()

    inv_ids = invoices["id"]
    pago_real = pd.to_numeric(inv_ids.map(real_days), errors="coerce")

    mask_settled = pago_real.notna()
    contado_real = mask_settled & (pago_real <= cash_threshold_days)
    credito_real = mask_settled & (pago_real > cash_threshold_days)

    # Aplicamos override sólo donde tenemos comportamiento real.
    if contado_real.any():
        result.loc[contado_real] = False
    if credito_real.any():
        result.loc[credito_real] = True

    return result


def compute_effective_due_date(
    invoices: pd.DataFrame,
    payments: pd.DataFrame | None = None,
) -> pd.Series:
    """
    Devuelve la fecha de vencimiento EFECTIVA de cada factura.

    - Si la factura se reclasifica como CONTADO (por payment_term o por
      settlement real) → due_efectivo = invoice_date (sin plazo).
    - Si es CRÉDITO → due_efectivo = invoice_date_due (lo que dice Odoo).

    Esto evita que facturas de contado mal etiquetadas con `invoice_date_due`
    a +30d aparezcan con "−30 días de mora" en los reportes y desvíen el
    `% pagado a tiempo`.
    """
    if invoices is None or invoices.empty:
        return pd.Series([], dtype="datetime64[ns]")

    inv_date = pd.to_datetime(invoices.get("invoice_date"), errors="coerce")
    due_date = pd.to_datetime(invoices.get("invoice_date_due"), errors="coerce")
    is_credit = classify_invoices_credit_vs_cash(invoices, payments=payments)

    # Donde es contado, el due efectivo es la fecha de la factura (plazo 0).
    # Donde es crédito, mantenemos el due de Odoo. Si due viene NaT, fallback
    # a invoice_date para no propagar NaT al cálculo de mora.
    due_effective = due_date.where(is_credit, inv_date)
    due_effective = due_effective.fillna(inv_date)
    return due_effective


def compute_rotation(
    invoices: pd.DataFrame,
    cutoff_date: date | None = None,
    period_days: int = 365,
    exclude_cash_sales: bool = True,
    open_invoices: pd.DataFrame | None = None,
    payments: pd.DataFrame | None = None,
) -> dict[str, float]:
    """
    Calcula rotación de cartera para el período (días hacia atrás desde cutoff).

    Fórmula (estándar contable, igual que Odoo):
        rotación (veces) = ventas a crédito / cartera promedio
        rotación (días)  = 365 / rotación (veces)
                        = (cartera promedio / ventas a crédito) * 365

    Cartera promedio:
      - Si se pasa `open_invoices` (lo recomendado), `saldo_final` se ancla en
        el saldo REAL hoy, y `saldo_inicial` se reconstruye caminando hacia
        atrás con todos los movimientos del periodo:
            saldo_inicial = saldo_final + cobrado + notas_credito − ventas_credito
        Con esto el promedio refleja la cartera realmente activa en el periodo.
      - Si no se pasa, cae al método antiguo (estima a partir de las facturas
        cargadas en la ventana, lo cual SUBESTIMA fuerte cuando hay facturas
        abiertas anteriores al rango y por eso daba "DSO = 6 días").

    Args:
        exclude_cash_sales: Si True (default), excluye facturas donde
            fecha_factura == fecha_vencimiento (ventas de contado/pago inmediato)
            tanto del numerador (ventas a crédito) como del denominador
            (saldo promedio). Esto evita distorsión cuando la operación tiene
            mucho contado.
        open_invoices: snapshot de facturas abiertas HOY (sin filtrar por fecha).
            Si se pasa, se usa para anclar el saldo final real.
        payments: pagos del periodo (mismo bundle que invoices). Necesario para
            reconstruir saldo_inicial cuando se ancla con open_invoices.
    """
    if invoices.empty:
        return {
            "ventas_credito": 0.0,
            "saldo_inicial": 0.0,
            "saldo_final": 0.0,
            "saldo_promedio": 0.0,
            "rotacion_veces": 0.0,
            "rotacion_dias": 0.0,
            "facturas_credito": 0,
            "facturas_contado_excluidas": 0,
        }

    cutoff = pd.Timestamp(cutoff_date or datetime.now().date())
    period_start = cutoff - pd.Timedelta(days=period_days)

    # Anclar el inicio del período al piso de datos confiables (cargue del
    # sistema). Si el período pedido se mete antes del go-live, lo subimos al
    # piso para que el denominador (ventas a crédito del período) no incluya
    # facturas parciales de la migración inicial.
    floor_ts = pd.Timestamp(get_data_floor_date())
    if period_start < floor_ts:
        period_start = floor_ts
    # Días reales transcurridos en el período recortado: la fórmula DSO usa
    # este número como factor temporal para que rotación_días siga siendo
    # consistente con la ventana real (ej. si solo hay 8 meses de datos
    # confiables y el usuario pidió "Último año", contamos 240 días, no 365).
    effective_period_days = max((cutoff - period_start).days, 1)

    # Filtrar por crédito vs contado.
    # Usamos la clasificación robusta que combina plazo nominal con
    # comportamiento real de pago: si una factura se liquidó en ≤3 días se
    # trata como CONTADO aunque su plazo nominal diga 30d (caso típico cuando
    # el plazo por defecto del cliente está mal configurado en Odoo).
    is_credit = classify_invoices_credit_vs_cash(invoices, payments=payments)
    if exclude_cash_sales:
        rotation_pool = invoices[is_credit].copy()
    else:
        rotation_pool = invoices.copy()

    facturas_credito = int(is_credit.sum())
    facturas_contado_excluidas = int((~is_credit).sum()) if exclude_cash_sales else 0

    # Solo facturas (excluimos refunds) en el período como ventas a crédito
    sales_mask = (
        (rotation_pool["move_type"] == "out_invoice")
        & (rotation_pool["invoice_date"] >= period_start)
        & (rotation_pool["invoice_date"] <= cutoff)
    )
    ventas_credito = float(rotation_pool.loc[sales_mask, "amount_total_signed"].abs().sum())

    # ------------------------------------------------------------------
    # Saldo final + saldo inicial.
    # Preferimos anclar en el saldo real (open_invoices) y caminar hacia atrás.
    # ------------------------------------------------------------------
    use_anchor = (
        open_invoices is not None
        and not open_invoices.empty
        and "amount_residual_signed" in open_invoices.columns
    )

    if use_anchor:
        saldo_final = float(open_invoices["amount_residual_signed"].abs().sum())

        # Cobrado en el periodo
        if payments is not None and not payments.empty:
            pay_dates = pd.to_datetime(payments["date"], errors="coerce")
            mask_pay = (pay_dates >= period_start) & (pay_dates <= cutoff)
            cobrado_periodo = float(payments.loc[mask_pay, "amount"].sum())
        else:
            cobrado_periodo = 0.0

        # Notas crédito en el periodo (suman al saldo cuando caminamos atrás)
        refunds = invoices[invoices["move_type"] == "out_refund"]
        if not refunds.empty:
            ref_dates = pd.to_datetime(refunds["invoice_date"], errors="coerce")
            mask_ref = (ref_dates >= period_start) & (ref_dates <= cutoff)
            notas_periodo = float(
                refunds.loc[mask_ref, "amount_total_signed"].abs().sum()
            )
        else:
            notas_periodo = 0.0

        saldo_inicial = max(
            saldo_final + cobrado_periodo + notas_periodo - ventas_credito,
            0.0,
        )
    else:
        # Fallback: método antiguo basado sólo en `invoices` cargadas.
        saldo_final = _saldo_abierto_a_fecha(rotation_pool, cutoff)
        saldo_inicial = _saldo_abierto_a_fecha(rotation_pool, period_start)

    saldo_promedio = (saldo_inicial + saldo_final) / 2 if (saldo_inicial + saldo_final) else 0.0

    rotacion_veces = (ventas_credito / saldo_promedio) if saldo_promedio else 0.0
    rotacion_dias = (effective_period_days / rotacion_veces) if rotacion_veces else 0.0

    return {
        "ventas_credito": ventas_credito,
        "saldo_inicial": saldo_inicial,
        "saldo_final": saldo_final,
        "saldo_promedio": saldo_promedio,
        "rotacion_veces": rotacion_veces,
        "rotacion_dias": rotacion_dias,
        "facturas_credito": facturas_credito,
        "facturas_contado_excluidas": facturas_contado_excluidas,
        "period_days_efectivo": effective_period_days,
        "period_start": period_start.date() if hasattr(period_start, "date") else period_start,
    }


def _saldo_abierto_a_fecha(invoices: pd.DataFrame, fecha: pd.Timestamp) -> float:
    """
    Aproxima el saldo de cartera abierto a una fecha dada.

    Considera todas las facturas emitidas hasta esa fecha y resta los pagos
    aplicados antes de esa fecha. Como aproximación rápida, usamos el
    amount_residual_signed actual para facturas anteriores a la fecha si
    payment_state es 'not_paid' o 'partial'.

    NOTA: `in_payment` se excluye explícitamente: esas facturas ya tienen
    pago registrado y solo esperan conciliación bancaria, su saldo real es 0.
    Para máxima precisión histórica habría que reconstruir desde
    account.move.line con matched_credit_ids/matched_debit_ids.
    """
    if invoices.empty:
        return 0.0

    df = invoices[invoices["invoice_date"] <= fecha]
    if df.empty:
        return 0.0

    # Solo facturas con saldo real pendiente.
    abiertas = df[df["payment_state"].isin(["not_paid", "partial"])]
    return float(abiertas["amount_residual_signed"].abs().sum())


def compute_partner_metrics(
    invoices: pd.DataFrame,
    open_invoices: pd.DataFrame,
    payments: pd.DataFrame,
    cutoff_date: date | None = None,
    analysis_window_days: int | None = None,
    exclude_cash_sales: bool = True,
) -> pd.DataFrame:
    """
    Calcula métricas por cliente:
    - saldo_actual: total pendiente de cobro.
    - num_facturas_abiertas: cuántas facturas tiene abiertas.
    - dias_vencido_max / promedio.
    - dias_pago_promedio_historico (facturas pagadas).
    - pct_pagado_a_tiempo.
    - num_facturas_historicas, monto_facturado_historico.
    - ultima_factura, ultimo_pago.

    Args:
        analysis_window_days: si se pasa, restringe el histórico de facturas
            usado para calcular hábito de pago a las últimas N días desde
            `cutoff_date`. Las facturas abiertas y los pagos también se
            recortan al mismo lapso para que el análisis sea consistente.
            None = usar todo el histórico cargado.
        exclude_cash_sales: si True (default), excluye facturas de contado
            del pool de cálculo de DSO/mora/% a tiempo. Esto alinea las
            métricas con la página de Detalle Cliente: las facturas de
            contado (pago día 0) jalaban el DSO promedio hacia abajo y
            ensuciaban el % de pago a tiempo.
    """
    cutoff = pd.Timestamp(cutoff_date or datetime.now().date())

    # Recorte por ventana de análisis (lapso de tiempo del estudio).
    # Sólo afecta al histórico de hábito de pago, no al saldo actual.
    if analysis_window_days and analysis_window_days > 0 and not invoices.empty:
        window_start = cutoff - pd.Timedelta(days=int(analysis_window_days))
        inv_dates = pd.to_datetime(invoices["invoice_date"], errors="coerce")
        invoices = invoices[inv_dates >= window_start].copy()
        if not payments.empty:
            pay_dates = pd.to_datetime(payments["date"], errors="coerce")
            payments = payments[pay_dates >= window_start].copy()

    # 1. Resumen de facturas abiertas por cliente.
    # Usamos due_efectivo (con payments) para que dias_vencido_max y
    # monto_vencido reflejen la realidad: facturas de contado mal puestas
    # como crédito por Odoo se vencen el día de la factura, no +30d.
    if not open_invoices.empty:
        open_with_aging = compute_days_overdue(
            open_invoices, cutoff_date, payments=payments,
        )
        abiertas_grp = open_with_aging.groupby("partner_id", dropna=False).agg(
            saldo_actual=("amount_residual_signed", lambda s: s.abs().sum()),
            num_facturas_abiertas=("id", "count"),
            dias_vencido_max=("dias_vencido", "max"),
            dias_vencido_promedio=("dias_vencido", "mean"),
            monto_vencido=(
                "amount_residual_signed",
                lambda s: s.where(open_with_aging.loc[s.index, "esta_vencida"], 0).abs().sum(),
            ),
            partner_name=("partner_name", "first"),
        )
    else:
        abiertas_grp = pd.DataFrame(
            columns=[
                "saldo_actual",
                "num_facturas_abiertas",
                "dias_vencido_max",
                "dias_vencido_promedio",
                "monto_vencido",
                "partner_name",
            ]
        )

    # 2. Histórico de comportamiento de pago.
    # IMPORTANTE: usamos settlement_date REAL (de reconciled_invoice_ids) en
    # vez de invoice.date — ese campo es la fecha contable de la factura, no
    # del pago, y daba dias_pago=0 siempre.
    if not invoices.empty:
        # Alinea con Detalle Cliente: por defecto excluimos contado del pool
        # de hábito de pago. Las facturas de contado pagan día 0 y jalaban el
        # DSO promedio hacia abajo (haciendo que un cliente con plazo real de
        # 45d pareciera de 30d). El override es explícito vía exclude_cash_sales.
        settlement = _compute_invoice_settlement_dates(
            invoices=invoices,
            payments=payments,
            exclude_cash_sales=exclude_cash_sales,
        )
        if not settlement.empty:
            # Dos due dates con propósitos distintos:
            #
            # `due_otorgado` (sin payments): respeta sólo el contrato.
            #   - Si payment_term_name dice "Contado" → 0d.
            #   - Si dice "30 días" o el documento tiene due > invoice_date → ese plazo.
            #   - NO mira el comportamiento real de pago (sin override por settlement).
            #   Sirve para reportar el PLAZO QUE LE OTORGAMOS al cliente —
            #   un cliente con plazo 30d que paga en 2d sigue teniendo plazo 30d.
            #
            # `due_efectivo` (con payments): aplica además el override por
            #   settlement ≤3d. Sirve para calcular MORA real, porque una
            #   factura nominal 30d que se pagó el mismo día no es "mora −30".
            due_otorgado = compute_effective_due_date(invoices, payments=None)
            due_efectivo = compute_effective_due_date(invoices, payments=payments)
            inv_with_due = invoices[["id", "invoice_date_due"]].copy()
            inv_with_due["due_otorgado"] = due_otorgado.values
            inv_with_due["due_efectivo"] = due_efectivo.values

            pagadas = settlement.merge(
                inv_with_due.rename(columns={"id": "invoice_id"}),
                on="invoice_id",
                how="left",
            )
            pagadas["invoice_date_due"] = pd.to_datetime(
                pagadas["invoice_date_due"], errors="coerce"
            )
            pagadas["due_otorgado"] = pd.to_datetime(
                pagadas["due_otorgado"], errors="coerce"
            )
            pagadas["due_efectivo"] = pd.to_datetime(
                pagadas["due_efectivo"], errors="coerce"
            )
            # Mora REAL = settlement vs due efectivo (con override settlement)
            pagadas["dias_de_mora"] = (
                pagadas["settlement_date"] - pagadas["due_efectivo"]
            ).dt.days
            pagadas["pago_a_tiempo"] = pagadas["dias_de_mora"] <= 0
            # Plazo OTORGADO = lo que dice el contrato, sin reclasificar por
            # comportamiento. Un cliente con plazo 30d sigue teniendo 30d
            # aunque pague rápido (eso lo refleja el DSO).
            pagadas["plazo_factura_dias"] = (
                pagadas["due_otorgado"] - pagadas["invoice_date"]
            ).dt.days.clip(lower=0).fillna(0)
            pagadas["id"] = pagadas["invoice_id"]  # para compatibilidad con count

            hist_grp = pagadas.groupby("partner_id", dropna=False).agg(
                dias_pago_promedio=("dias_pago", "mean"),
                dias_mora_promedio=("dias_de_mora", "mean"),
                pct_pagado_a_tiempo=("pago_a_tiempo", lambda s: s.mean() * 100),
                num_facturas_pagadas=("id", "count"),
                plazo_promedio_dias=("plazo_factura_dias", "mean"),
            )
        else:
            hist_grp = pd.DataFrame(
                columns=[
                    "dias_pago_promedio",
                    "dias_mora_promedio",
                    "pct_pagado_a_tiempo",
                    "num_facturas_pagadas",
                    "plazo_promedio_dias",
                ]
            )

        # Conteo de facturas a CRÉDITO vs CONTADO por cliente.
        # Se usa para distinguir clientes puramente de contado (que no aplican
        # al scoring de cartera) de clientes con operación a crédito.
        is_credito_inv = classify_invoices_credit_vs_cash(invoices, payments=payments)
        invoices_tipo = invoices.assign(_es_credito=is_credito_inv.values)
        tipo_grp = invoices_tipo.groupby("partner_id", dropna=False).agg(
            num_facturas_credito=("_es_credito", "sum"),
            num_facturas_historicas_total=("_es_credito", "count"),
        )
        tipo_grp["num_facturas_contado"] = (
            tipo_grp["num_facturas_historicas_total"]
            - tipo_grp["num_facturas_credito"]
        )

        todas_grp = invoices.groupby("partner_id", dropna=False).agg(
            num_facturas_historicas=("id", "count"),
            monto_facturado_historico=(
                "amount_total_signed",
                lambda s: s.abs().sum(),
            ),
            ultima_factura=("invoice_date", "max"),
            primera_factura=("invoice_date", "min"),
        )
        todas_grp = todas_grp.join(
            tipo_grp[["num_facturas_credito", "num_facturas_contado"]],
            how="left",
        )
    else:
        hist_grp = pd.DataFrame(
            columns=[
                "dias_pago_promedio",
                "dias_mora_promedio",
                "pct_pagado_a_tiempo",
                "num_facturas_pagadas",
                "plazo_promedio_dias",
            ]
        )
        todas_grp = pd.DataFrame(
            columns=[
                "num_facturas_historicas",
                "monto_facturado_historico",
                "ultima_factura",
                "primera_factura",
                "num_facturas_credito",
                "num_facturas_contado",
            ]
        )

    # 3. Último pago
    if not payments.empty:
        ult_pago = payments.groupby("partner_id", dropna=False).agg(
            ultimo_pago=("date", "max"),
            monto_pagado_historico=("amount", "sum"),
            num_pagos_historicos=("id", "count"),
        )
    else:
        ult_pago = pd.DataFrame(
            columns=["ultimo_pago", "monto_pagado_historico", "num_pagos_historicos"]
        )

    # Combinar todo
    result = abiertas_grp.join(hist_grp, how="outer").join(todas_grp, how="outer").join(
        ult_pago, how="outer"
    )
    result = result.fillna(
        {
            "saldo_actual": 0.0,
            "num_facturas_abiertas": 0,
            "monto_vencido": 0.0,
            "num_facturas_historicas": 0,
            "monto_facturado_historico": 0.0,
            "num_facturas_pagadas": 0,
            "pct_pagado_a_tiempo": 0.0,
            "monto_pagado_historico": 0.0,
            "num_pagos_historicos": 0,
            "num_facturas_credito": 0,
            "num_facturas_contado": 0,
        }
    )

    # Tipo de cliente: CRÉDITO si tiene al menos una factura a crédito;
    # CONTADO si TODAS sus facturas históricas son de contado; MIXTO si
    # tiene de ambos tipos. La página de Scoring oculta CONTADO por
    # defecto porque el scoring evalúa hábito de pago a crédito.
    def _tipo_cliente(row) -> str:
        c = int(row.get("num_facturas_credito") or 0)
        ct = int(row.get("num_facturas_contado") or 0)
        if c == 0 and ct == 0:
            return "SIN_FACTURAS"
        if c == 0:
            return "CONTADO"
        if ct == 0:
            return "CREDITO"
        return "MIXTO"

    result["tipo_cliente"] = result.apply(_tipo_cliente, axis=1)

    # Antigüedad como cliente (días)
    result["antiguedad_dias"] = (
        cutoff - pd.to_datetime(result.get("primera_factura"), errors="coerce")
    ).dt.days

    # % vencido sobre saldo actual del cliente
    result["pct_vencido_cliente"] = np.where(
        result["saldo_actual"] > 0,
        result["monto_vencido"] / result["saldo_actual"] * 100,
        0,
    )

    # ------------------------------------------------------------------
    # DSO POR CLIENTE — promedio real de días de pago.
    #
    # Antes usábamos la fórmula contable (saldo / ventas × días), que es
    # rápida pero promediaba clientes muy distintos (uno que pagó a 30d y
    # otro a 60d salían igual si tenían el mismo saldo/ventas). Eso
    # producía la discrepancia que reportó el negocio: un cliente que en
    # Detalle Cliente sale con DSO 43-45d aparecía con 30d en Scoring.
    #
    # Ahora usamos el mismo cálculo que Detalle Cliente:
    #     DSO_cliente = mean(settlement_date − invoice_date) sobre las
    #                   facturas pagadas del periodo, excluyendo contado
    #                   por defecto.
    #
    # `dias_pago_promedio` ya viene calculado arriba con esa lógica, así
    # que sólo lo re-exponemos como `dso_cliente`.
    # Si no hay facturas pagadas, queda NaN.
    # ------------------------------------------------------------------
    if "dias_pago_promedio" in result.columns:
        result["dso_cliente"] = pd.to_numeric(
            result["dias_pago_promedio"], errors="coerce"
        ).round(1)
    else:
        result["dso_cliente"] = np.nan

    # Cumplimiento del plazo: el cliente paga dentro/fuera de su propio plazo.
    # Esta métrica es por-cliente (cada uno tiene su propio plazo otorgado),
    # NO compara contra un promedio global.
    if "plazo_promedio_dias" in result.columns and "dso_cliente" in result.columns:
        plazo = pd.to_numeric(result["plazo_promedio_dias"], errors="coerce")
        dso = pd.to_numeric(result["dso_cliente"], errors="coerce")
        result["dias_sobre_plazo"] = (dso - plazo).round(1)

        def _describe_cumplimiento(row) -> str:
            p = row.get("plazo_promedio_dias")
            d = row.get("dso_cliente")
            if pd.isna(p) or pd.isna(d) or row.get("num_facturas_pagadas", 0) < 1:
                return "Sin histórico de pagos"
            sobre = d - p
            if p <= 0:
                # cliente de contado
                if d <= 1:
                    return "Contado al día"
                return f"Contado pero pagó {int(round(d))} días después"
            if sobre <= -1:
                return f"Paga {int(round(abs(sobre)))} días antes de su plazo de {int(round(p))} días"
            if sobre <= 1:
                return f"Cumple su plazo de {int(round(p))} días"
            return f"Excede su plazo de {int(round(p))} días en {int(round(sobre))} días"

        result["cumplimiento_plazo"] = result.apply(_describe_cumplimiento, axis=1)

    result = result.reset_index().rename(columns={"index": "partner_id"})
    return result


# ---------------------------------------------------------------------------
# API principal
# ---------------------------------------------------------------------------


def analyze_cartera(
    invoices: pd.DataFrame,
    open_invoices: pd.DataFrame,
    payments: pd.DataFrame,
    partners: pd.DataFrame,
    cutoff_date: date | None = None,
    rotation_period_days: int = 365,
    exclude_cash_sales: bool = True,
    analysis_window_days: int | None = None,
) -> CarteraMetrics:
    """
    Función principal: calcula todas las métricas de cartera y devuelve un objeto
    consolidado con dataframes y KPIs.

    Args:
        exclude_cash_sales: Si True, excluye facturas con fecha_factura ==
            fecha_vencimiento (ventas de contado) del cálculo de rotación.
        analysis_window_days: lapso de tiempo (días) para acotar el cálculo de
            hábito de pago / scoring por cliente. None = todo el histórico.
    """
    cutoff = cutoff_date or datetime.now().date()

    # Filtro defensivo: `in_payment` significa pagada pero pendiente de
    # conciliación (saldo real = 0). La excluimos del modelo para no
    # inflar # de facturas abiertas, aging ni vencido.
    if not open_invoices.empty and "payment_state" in open_invoices.columns:
        open_invoices = open_invoices[
            open_invoices["payment_state"].isin(["not_paid", "partial"])
        ].copy()

    # Aging y días vencidos.
    # Pasamos `payments` para que el due efectivo respete:
    #   - payment_term_name explícito ("Contado" override)
    #   - settlement override (si hay liquidación parcial registrada)
    open_with_aging = compute_days_overdue(open_invoices, cutoff, payments=payments)
    aging_report = build_aging_report(open_invoices, cutoff, payments=payments)

    # Rotación.
    # Pasamos open_invoices + payments para anclar el saldo final en el saldo
    # real de hoy (no estimado a partir de la ventana de invoices), de modo que
    # la rotación global coincida con la fórmula que usa Odoo y con el rolling
    # 90d del histórico mensual.
    rotation = compute_rotation(
        invoices,
        cutoff,
        rotation_period_days,
        exclude_cash_sales=exclude_cash_sales,
        open_invoices=open_invoices,
        payments=payments,
    )

    # Métricas por cliente.
    # Pasamos exclude_cash_sales para que el DSO/mora/pct_a_tiempo se calcule
    # sobre el mismo pool que la rotación global (y que Detalle Cliente).
    by_partner = compute_partner_metrics(
        invoices,
        open_invoices,
        payments,
        cutoff,
        analysis_window_days=analysis_window_days,
        exclude_cash_sales=exclude_cash_sales,
    )

    # Enriquecer by_partner con info de res.partner (cupo, NIT, DSO Odoo, etc.)
    if not partners.empty:
        partner_cols = [
            "id",
            "name",
            "vat",
            "credit_limit",
            "use_partner_credit_limit",  # bool: indica si Odoo controla el límite
            "days_sales_outstanding",  # DSO nativo de Odoo (si está disponible)
            "email",
            "phone",
            "mobile",
            "city",
        ]
        cols_present = [c for c in partner_cols if c in partners.columns]
        by_partner = by_partner.merge(
            partners[cols_present].rename(
                columns={
                    "id": "partner_id",
                    "days_sales_outstanding": "dso_odoo",
                }
            ),
            on="partner_id",
            how="left",
            suffixes=("", "_partner"),
        )
        # Si no había partner_name desde abiertas, completarlo desde partners
        if "name" in by_partner.columns:
            by_partner["partner_name"] = by_partner["partner_name"].fillna(
                by_partner["name"]
            )

    # KPIs globales
    saldo_total = float(open_with_aging["amount_residual_signed"].abs().sum()) if not open_with_aging.empty else 0.0
    monto_vencido = float(
        open_with_aging.loc[open_with_aging["esta_vencida"], "amount_residual_signed"].abs().sum()
    ) if not open_with_aging.empty else 0.0
    num_vencidas = int(open_with_aging["esta_vencida"].sum()) if not open_with_aging.empty else 0

    return CarteraMetrics(
        cutoff_date=cutoff,
        saldo_cartera=saldo_total,
        saldo_cartera_promedio=rotation["saldo_promedio"],
        ventas_credito_periodo=rotation["ventas_credito"],
        rotacion_veces=rotation["rotacion_veces"],
        rotacion_dias=rotation["rotacion_dias"],
        dso=rotation["rotacion_dias"],
        facturas_abiertas=len(open_invoices),
        facturas_vencidas=num_vencidas,
        monto_vencido=monto_vencido,
        pct_vencido=(monto_vencido / saldo_total * 100) if saldo_total else 0.0,
        clientes_con_saldo=int(by_partner[by_partner["saldo_actual"] > 0].shape[0]) if not by_partner.empty else 0,
        facturas_credito_periodo=int(rotation.get("facturas_credito", 0)),
        facturas_contado_excluidas=int(rotation.get("facturas_contado_excluidas", 0)),
        exclude_cash_sales=exclude_cash_sales,
        aging=aging_report,
        open_invoices=open_with_aging,
        by_partner=by_partner,
    )


# ---------------------------------------------------------------------------
# FIFO matching factura ↔ pago (para reconstruir días reales de pago)
# ---------------------------------------------------------------------------


def _compute_invoice_settlement_dates(
    invoices: pd.DataFrame,
    payments: pd.DataFrame,
    exclude_cash_sales: bool = True,
) -> pd.DataFrame:
    """
    Reconstruye la **fecha en que cada factura quedó completamente liquidada**
    (settlement_date) y los días al pago (`settlement_date - invoice_date`).

    Devuelve un DataFrame con una fila por factura liquidada:
        - invoice_id
        - partner_id
        - invoice_date
        - settlement_date  (fecha del ÚLTIMO pago vinculado a esa factura)
        - amount
        - dias_pago

    Las facturas sin liquidar (todavía con saldo) NO aparecen.

    Estrategia:
    -----------
    1) **Vínculo directo `account.payment.reconciled_invoice_ids`** (preferido).
       Es el campo nativo de Odoo Enterprise que vincula cada pago con las
       facturas que liquidó. Si una factura tiene varios pagos vinculados,
       el `settlement_date` es la fecha del PAGO MÁS RECIENTE — el día en
       que la factura quedó totalmente cubierta.

    2) **FIFO matching** (fallback). Si `reconciled_invoice_ids` no viene en
       el extracto (versiones viejas, permisos, etc.), reconstruimos el
       matching aplicando los pagos del cliente en orden cronológico a sus
       facturas más viejas primero. Es una aproximación; menos exacta que el
       vínculo directo pero razonable a nivel agregado.
    """
    cols_out = [
        "invoice_id", "partner_id", "invoice_date",
        "settlement_date", "amount", "dias_pago",
    ]
    if invoices is None or invoices.empty:
        return pd.DataFrame(columns=cols_out)

    inv = invoices[invoices["move_type"] == "out_invoice"].copy()
    if exclude_cash_sales:
        inv = inv[_is_credit_sale(inv)]
    if inv.empty:
        return pd.DataFrame(columns=cols_out)

    inv["invoice_date"] = pd.to_datetime(inv["invoice_date"], errors="coerce")
    inv = inv.dropna(subset=["invoice_date"])
    inv["amount"] = inv["amount_total_signed"].abs()
    inv = inv[inv["amount"] > 0]

    # ------------------------------------------------------------------
    # Estrategia 1: usar reconciled_invoice_ids (vínculo directo Odoo)
    # ------------------------------------------------------------------
    use_native_link = (
        payments is not None
        and not payments.empty
        and "reconciled_invoice_ids" in payments.columns
    )

    if use_native_link:
        pay = payments.copy()
        pay["date"] = pd.to_datetime(pay["date"], errors="coerce")
        pay = pay.dropna(subset=["date"])

        # invoice_id (int) → fecha del último pago vinculado
        invoice_to_settle: dict[int, pd.Timestamp] = {}
        any_link = False
        for _, prow in pay.iterrows():
            inv_ids_raw = prow.get("reconciled_invoice_ids")
            if not isinstance(inv_ids_raw, (list, tuple)) or len(inv_ids_raw) == 0:
                continue
            any_link = True
            pdate = prow["date"]
            for raw_id in inv_ids_raw:
                try:
                    iid = int(raw_id)
                except (TypeError, ValueError):
                    continue
                prev = invoice_to_settle.get(iid)
                if prev is None or pdate > prev:
                    invoice_to_settle[iid] = pdate

        # Si no había NINGÚN vínculo, caemos al FIFO (campo vacío / no soportado)
        if any_link:
            rows: list[dict] = []
            for _, row in inv.iterrows():
                try:
                    iid = int(row.get("id"))
                except (TypeError, ValueError):
                    continue
                settlement_date = invoice_to_settle.get(iid)
                if settlement_date is None:
                    continue
                dias = (settlement_date - row["invoice_date"]).days
                if dias < 0:
                    continue
                rows.append({
                    "invoice_id": iid,
                    "partner_id": row.get("partner_id"),
                    "invoice_date": row["invoice_date"],
                    "settlement_date": settlement_date,
                    "amount": float(row["amount"]),
                    "dias_pago": int(dias),
                })
            logger.info(
                "Settlement dates desde reconciled_invoice_ids: %s/%s facturas vinculadas.",
                len(rows), len(inv),
            )
            return pd.DataFrame(rows, columns=cols_out)

    # ------------------------------------------------------------------
    # Estrategia 2: fallback FIFO matching por cliente
    # ------------------------------------------------------------------
    inv = inv.sort_values(["partner_id", "invoice_date", "id"])

    if payments is not None and not payments.empty:
        pay = payments.copy()
        pay["date"] = pd.to_datetime(pay["date"], errors="coerce")
        pay = pay.dropna(subset=["date"])
        pay = pay[pay["amount"] > 0]
        pay = pay.sort_values(["partner_id", "date"])
    else:
        pay = pd.DataFrame(columns=["partner_id", "date", "amount"])

    rows = []
    EPS = 0.01

    for partner_id, inv_grp in inv.groupby("partner_id"):
        if not pay.empty:
            pay_grp = pay[pay["partner_id"] == partner_id]
            pay_queue: list[list] = [
                [d, float(a)]
                for d, a in zip(pay_grp["date"].tolist(), pay_grp["amount"].astype(float).tolist())
            ]
        else:
            pay_queue = []

        pq_idx = 0
        for _, row in inv_grp.iterrows():
            inv_remaining = float(row["amount"])
            settlement_date = None
            while inv_remaining > EPS:
                while pq_idx < len(pay_queue) and pay_queue[pq_idx][1] <= EPS:
                    pq_idx += 1
                if pq_idx >= len(pay_queue):
                    break
                applied = min(inv_remaining, pay_queue[pq_idx][1])
                inv_remaining -= applied
                pay_queue[pq_idx][1] -= applied
                if inv_remaining <= EPS:
                    settlement_date = pay_queue[pq_idx][0]

            if settlement_date is not None:
                dias = (settlement_date - row["invoice_date"]).days
                if dias >= 0:
                    rows.append({
                        "invoice_id": row.get("id"),
                        "partner_id": partner_id,
                        "invoice_date": row["invoice_date"],
                        "settlement_date": settlement_date,
                        "amount": float(row["amount"]),
                        "dias_pago": int(dias),
                    })

    logger.info("Settlement dates desde FIFO fallback: %s facturas liquidadas.", len(rows))
    return pd.DataFrame(rows, columns=cols_out)


# ---------------------------------------------------------------------------
# Histórico mensual (para dashboard de tendencias)
# ---------------------------------------------------------------------------


def compute_monthly_history(
    invoices: pd.DataFrame,
    payments: pd.DataFrame,
    months: int = 12,
    cutoff_date: date | None = None,
    exclude_cash_sales: bool = True,
    open_invoices: pd.DataFrame | None = None,
    dso_method: str = "balance_based",
) -> pd.DataFrame:
    """
    Construye un histórico mensual con métricas clave de cartera.

    Devuelve DataFrame con columnas (una fila por mes):
        - mes (Timestamp, primer día del mes)
        - mes_label (str "YYYY-MM")
        - facturado_credito: ventas a crédito en el mes (solo out_invoice)
        - notas_credito: total de notas crédito (out_refund) en el mes
        - facturado_neto: facturado_credito - notas_credito
        - cobrado: pagos recibidos en el mes
        - saldo_acumulado: saldo aproximado al fin de mes (ver más abajo)
        - dso_rolling: DSO rolling 90 días: saldo / ventas_90d * 90

    Args:
        exclude_cash_sales: si True, excluye facturas de contado del facturado
            de crédito y del DSO (consistente con la rotación principal).
        open_invoices: snapshot de facturas abiertas HOY (sin filtrar por fecha).
            Si se pasa, el saldo histórico se calcula partiendo de este saldo
            real y caminando hacia atrás. Si no se pasa, se cae al método
            acumulativo (menos preciso, queda en 0 si los pagos del periodo
            son por facturas anteriores a la ventana).

    NOTA: cálculo del saldo a fin de mes
    --------------------------------------------
    El saldo de hoy lo conocemos exactamente (= suma de amount_residual de
    `open_invoices`). Para meses anteriores caminamos hacia atrás:

        saldo_X = saldo_hoy + (cobrado_después_de_X)
                            + (notas_credito_después_de_X)
                            - (facturado_credito_después_de_X)

    Esto es exacto siempre que tengamos todos los movimientos posteriores a X
    (facturas y pagos), que es justo lo que `extract_all_for_cartera` trae
    para la ventana visualizada.

    Si `open_invoices` no viene, caemos al acumulado clásico (puede subestimar
    cuando los cobros del periodo son de facturas anteriores a la ventana).
    """
    cutoff = pd.Timestamp(cutoff_date or datetime.now().date())
    period_end = cutoff.to_period("M").to_timestamp(how="start")

    # Generar serie de meses (primer día de cada mes hacia atrás)
    month_starts = pd.date_range(
        end=period_end, periods=max(months, 1), freq="MS"
    )

    # Truncar al piso de datos confiables: no mostramos meses anteriores al
    # cargue del sistema porque tendrían facturas parciales/saldos iniciales.
    floor_ts = pd.Timestamp(get_data_floor_date())
    floor_month_start = floor_ts.to_period("M").to_timestamp(how="start")
    month_starts = month_starts[month_starts >= floor_month_start]
    if len(month_starts) == 0:
        # El cutoff cae antes del piso → no hay histórico que mostrar.
        return pd.DataFrame(
            columns=[
                "mes", "mes_label", "facturado_credito", "notas_credito",
                "facturado_neto", "cobrado", "saldo_acumulado", "dso_rolling",
            ]
        )

    if invoices is None or invoices.empty:
        rows = [
            {
                "mes": m,
                "mes_label": m.strftime("%Y-%m"),
                "facturado_credito": 0.0,
                "notas_credito": 0.0,
                "facturado_neto": 0.0,
                "cobrado": 0.0,
                "saldo_acumulado": 0.0,
                "dso_rolling": 0.0,
            }
            for m in month_starts
        ]
        return pd.DataFrame(rows)

    inv = invoices.copy()
    inv["invoice_date"] = pd.to_datetime(inv["invoice_date"], errors="coerce")
    inv["mes_factura"] = inv["invoice_date"].dt.to_period("M").dt.to_timestamp()

    # Crédito vs contado (clasificación robusta con settlement real).
    is_credit = classify_invoices_credit_vs_cash(inv, payments=payments)
    if exclude_cash_sales:
        inv_credit = inv[is_credit & (inv["move_type"] == "out_invoice")]
    else:
        inv_credit = inv[inv["move_type"] == "out_invoice"]

    inv_refund = inv[inv["move_type"] == "out_refund"]

    # Pagos
    if payments is not None and not payments.empty:
        pay = payments.copy()
        pay["date"] = pd.to_datetime(pay["date"], errors="coerce")
        pay["mes_pago"] = pay["date"].dt.to_period("M").dt.to_timestamp()
    else:
        pay = pd.DataFrame(columns=["date", "amount", "mes_pago"])

    # ------------------------------------------------------------------
    # Saldo de hoy (anchor para caminar hacia atrás).
    # ------------------------------------------------------------------
    saldo_hoy = 0.0
    use_anchor = False
    if open_invoices is not None and not open_invoices.empty:
        if "amount_residual_signed" in open_invoices.columns:
            saldo_hoy = float(open_invoices["amount_residual_signed"].abs().sum())
            use_anchor = True

    # Pre-cálculo de movimientos por mes para la pasada hacia atrás.
    # Necesitamos para cada mes: facturado, notas, cobrado.
    monthly_data: list[dict] = []
    for m_start in month_starts:
        next_m = m_start + pd.offsets.MonthBegin(1)

        fact_credit_mes = float(
            inv_credit.loc[
                (inv_credit["invoice_date"] >= m_start)
                & (inv_credit["invoice_date"] < next_m),
                "amount_total_signed",
            ]
            .abs()
            .sum()
        )
        notas_mes = float(
            inv_refund.loc[
                (inv_refund["invoice_date"] >= m_start)
                & (inv_refund["invoice_date"] < next_m),
                "amount_total_signed",
            ]
            .abs()
            .sum()
        )
        cobrado_mes = float(
            pay.loc[
                (pay["date"] >= m_start) & (pay["date"] < next_m),
                "amount",
            ].sum()
        ) if not pay.empty else 0.0

        monthly_data.append({
            "m_start": m_start,
            "next_m": next_m,
            "fact_credit_mes": fact_credit_mes,
            "notas_mes": notas_mes,
            "cobrado_mes": cobrado_mes,
        })

    # ------------------------------------------------------------------
    # Calcular saldo por mes:
    # - Si tenemos saldo_hoy real (open_invoices), caminamos hacia atrás.
    # - Si no, caemos al acumulado tradicional desde 0.
    # ------------------------------------------------------------------
    saldos_por_mes: dict = {}
    if use_anchor:
        # Movimientos POSTERIORES a la ventana (entre el último mes mostrado
        # y "hoy"). Si los hay, contribuyen al saldo del último mes.
        last_next = monthly_data[-1]["next_m"] if monthly_data else cutoff
        if not pay.empty:
            cobrado_post = float(pay.loc[pay["date"] >= last_next, "amount"].sum())
        else:
            cobrado_post = 0.0
        fact_post = float(
            inv_credit.loc[inv_credit["invoice_date"] >= last_next, "amount_total_signed"]
            .abs().sum()
        )
        notas_post = float(
            inv_refund.loc[inv_refund["invoice_date"] >= last_next, "amount_total_signed"]
            .abs().sum()
        )

        # saldo al fin del último mes mostrado:
        #   saldo_hoy + cobros_post + notas_post - facturado_post
        saldo_corriente = saldo_hoy + cobrado_post + notas_post - fact_post

        # Caminamos hacia atrás: para cada mes (de más reciente a más antiguo),
        # saldo_anterior = saldo_actual + cobrado_mes + notas_mes - facturado_mes
        for d in reversed(monthly_data):
            saldos_por_mes[d["m_start"]] = max(saldo_corriente, 0.0)
            saldo_corriente = (
                saldo_corriente
                + d["cobrado_mes"]
                + d["notas_mes"]
                - d["fact_credit_mes"]
            )
    else:
        # Fallback: acumulado desde 0 (puede subestimar).
        facturado_acum = 0.0
        cobrado_acum = 0.0
        for d in monthly_data:
            facturado_acum += d["fact_credit_mes"] - d["notas_mes"]
            cobrado_acum += d["cobrado_mes"]
            saldos_por_mes[d["m_start"]] = max(facturado_acum - cobrado_acum, 0.0)

    # ------------------------------------------------------------------
    # Pre-cómputo para `dso_method='payment_days'`:
    # FIFO matching entre facturas y pagos por cliente para reconstruir la
    # "fecha de liquidación" real de cada factura (settlement_date).
    # `dias_pago = settlement_date - invoice_date`.
    #
    # Importante: NO usamos `account.move.date` como fecha de pago — ese campo
    # es la fecha contable de la factura misma, no del pago. La fecha real del
    # pago vive en `account.payment.date` y se asocia a la factura por
    # conciliación. Como no extraemos la conciliación, hacemos FIFO matching
    # por cliente (el cliente paga sus facturas más viejas primero), que es
    # una excelente aproximación para vista per-cliente.
    # ------------------------------------------------------------------
    paid_inv = pd.DataFrame()
    if dso_method == "payment_days":
        paid_inv = _compute_invoice_settlement_dates(
            invoices=invoices,
            payments=payments,
            exclude_cash_sales=exclude_cash_sales,
        )

    # ------------------------------------------------------------------
    # Construir filas finales con DSO rolling 90d.
    # ------------------------------------------------------------------
    rows: list[dict] = []
    for d in monthly_data:
        m_start = d["m_start"]
        next_m = d["next_m"]
        fact_credit_mes = d["fact_credit_mes"]
        notas_mes = d["notas_mes"]
        cobrado_mes = d["cobrado_mes"]
        facturado_neto_mes = fact_credit_mes - notas_mes
        saldo_estim = saldos_por_mes.get(m_start, 0.0)

        # DSO rolling 90 días al final del mes
        end_of_month = next_m - pd.Timedelta(days=1)
        start_90 = end_of_month - pd.Timedelta(days=90)
        ventas_90d = float(
            inv_credit.loc[
                (inv_credit["invoice_date"] >= start_90)
                & (inv_credit["invoice_date"] <= end_of_month),
                "amount_total_signed",
            ]
            .abs()
            .sum()
        )

        if dso_method == "payment_days":
            # Promedio de días-de-pago para facturas LIQUIDADAS dentro de la
            # ventana 90d (settlement_date entre [end_of_month - 90d, end_of_month]).
            # settlement_date viene del FIFO matching factura↔pago por cliente.
            if not paid_inv.empty:
                window = paid_inv[
                    (paid_inv["settlement_date"] >= start_90)
                    & (paid_inv["settlement_date"] <= end_of_month)
                ]
                dso_rolling = float(window["dias_pago"].mean()) if len(window) else 0.0
            else:
                dso_rolling = 0.0
        else:
            # balance_based: fórmula clásica (apropiada para portafolios).
            dso_rolling = (saldo_estim / ventas_90d * 90) if ventas_90d > 0 else 0.0

        rows.append(
            {
                "mes": m_start,
                "mes_label": m_start.strftime("%Y-%m"),
                "facturado_credito": fact_credit_mes,
                "notas_credito": notas_mes,
                "facturado_neto": facturado_neto_mes,
                "cobrado": cobrado_mes,
                "saldo_acumulado": saldo_estim,
                "dso_rolling": round(dso_rolling, 1),
            }
        )

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Detalle por cliente individual
# ---------------------------------------------------------------------------


def filter_partner_data(
    invoices: pd.DataFrame,
    payments: pd.DataFrame,
    open_invoices: pd.DataFrame,
    partner_id: int,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict[str, pd.DataFrame]:
    """
    Filtra facturas/pagos/abiertas para un solo cliente y un rango de fechas.

    Útil para alimentar la página de detalle por cliente. El rango de fechas
    se aplica a `invoice_date` (facturas) y `date` (pagos); las facturas
    abiertas se devuelven sin filtrar por fecha (siempre relevantes).
    """
    inv = (
        invoices.copy()
        if invoices is not None and not invoices.empty
        else pd.DataFrame()
    )
    pay = (
        payments.copy()
        if payments is not None and not payments.empty
        else pd.DataFrame()
    )
    open_inv = (
        open_invoices.copy()
        if open_invoices is not None and not open_invoices.empty
        else pd.DataFrame()
    )

    if not inv.empty and "partner_id" in inv.columns:
        inv = inv[inv["partner_id"] == partner_id]
        inv["invoice_date"] = pd.to_datetime(inv["invoice_date"], errors="coerce")
        if date_from is not None:
            inv = inv[inv["invoice_date"] >= pd.Timestamp(date_from)]
        if date_to is not None:
            inv = inv[inv["invoice_date"] <= pd.Timestamp(date_to)]

    if not pay.empty and "partner_id" in pay.columns:
        pay = pay[pay["partner_id"] == partner_id]
        pay["date"] = pd.to_datetime(pay["date"], errors="coerce")
        if date_from is not None:
            pay = pay[pay["date"] >= pd.Timestamp(date_from)]
        if date_to is not None:
            pay = pay[pay["date"] <= pd.Timestamp(date_to)]

    if not open_inv.empty and "partner_id" in open_inv.columns:
        open_inv = open_inv[open_inv["partner_id"] == partner_id]

    return {
        "invoices": inv.reset_index(drop=True),
        "payments": pay.reset_index(drop=True),
        "open_invoices": open_inv.reset_index(drop=True),
    }


def compute_partner_payment_distribution(
    invoices: pd.DataFrame,
    payments: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    Distribución de días de mora de las facturas LIQUIDADAS de un cliente,
    bucketizada para histograma.

    Usa el `settlement_date` real (de reconciled_invoice_ids o FIFO) cuando se
    pasa `payments`. Si no, hace fallback al campo `invoice.date` (que es la
    fecha contable de la factura, no del pago — métrica imprecisa).

    Columnas: bucket, num_facturas, monto_total.
    """
    cols = ["bucket", "num_facturas", "monto_total"]
    if invoices is None or invoices.empty:
        return pd.DataFrame(columns=cols)

    if payments is not None:
        # Camino correcto: usar settlement_date real
        sett = _compute_invoice_settlement_dates(invoices, payments, exclude_cash_sales=False)
        if sett.empty:
            return pd.DataFrame(columns=cols)
        df = sett.merge(
            invoices[["id", "invoice_date_due"]].rename(columns={"id": "invoice_id"}),
            on="invoice_id",
            how="left",
        )
        df["invoice_date_due"] = pd.to_datetime(df["invoice_date_due"], errors="coerce")
        df["dias_de_mora"] = (df["settlement_date"] - df["invoice_date_due"]).dt.days
        df["amount_abs"] = df["amount"]
    else:
        # Fallback (impreciso) — mantenido por compatibilidad
        pagadas = invoices[invoices["payment_state"] == "paid"].copy()
        if pagadas.empty:
            return pd.DataFrame(columns=cols)
        pagadas["dias_de_mora"] = (
            pd.to_datetime(pagadas["date"], errors="coerce")
            - pd.to_datetime(pagadas["invoice_date_due"], errors="coerce")
        ).dt.days
        df = pagadas
        df["amount_abs"] = df["amount_total_signed"].abs()

    buckets = [
        ("Antes del plazo (≤ -1d)", -10000, -1),
        ("A tiempo (0d)", 0, 0),
        ("1–7 días", 1, 7),
        ("8–15 días", 8, 15),
        ("16–30 días", 16, 30),
        ("31–60 días", 31, 60),
        ("61–90 días", 61, 90),
        ("Más de 90 días", 91, 100000),
    ]

    rows = []
    for label, lo, hi in buckets:
        mask = (df["dias_de_mora"] >= lo) & (df["dias_de_mora"] <= hi)
        rows.append(
            {
                "bucket": label,
                "num_facturas": int(mask.sum()),
                "monto_total": float(df.loc[mask, "amount_abs"].sum()),
            }
        )
    return pd.DataFrame(rows)


def compute_partner_payment_timeline(
    invoices: pd.DataFrame,
    payments: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    Línea de tiempo de pagos de un cliente: una fila por factura LIQUIDADA con
    fecha de liquidación real y días de mora respecto al vencimiento.

    Usa el `settlement_date` real (de reconciled_invoice_ids o FIFO) cuando se
    pasa `payments`. Si no, fallback al campo `invoice.date` (impreciso).

    Columnas: factura, invoice_date, fecha_pago, dias_de_mora, monto, plazo.
    """
    cols = ["factura", "invoice_date", "fecha_pago", "dias_de_mora", "monto", "plazo"]
    if invoices is None or invoices.empty:
        return pd.DataFrame(columns=cols)

    if payments is not None:
        sett = _compute_invoice_settlement_dates(invoices, payments, exclude_cash_sales=False)
        if sett.empty:
            return pd.DataFrame(columns=cols)
        df = sett.merge(
            invoices[["id", "invoice_date_due", "name"]].rename(columns={"id": "invoice_id"}),
            on="invoice_id",
            how="left",
        )
        df["invoice_date_due"] = pd.to_datetime(df["invoice_date_due"], errors="coerce")
        df["fecha_pago"] = df["settlement_date"]
        df["dias_de_mora"] = (df["fecha_pago"] - df["invoice_date_due"]).dt.days
        df["plazo"] = (df["invoice_date_due"] - df["invoice_date"]).dt.days
        df["monto"] = df["amount"]
        df["factura"] = df.get("name", "")
        return df[cols].sort_values("fecha_pago")

    # Fallback impreciso
    pagadas = invoices[invoices["payment_state"] == "paid"].copy()
    if pagadas.empty:
        return pd.DataFrame(columns=cols)
    pagadas["invoice_date"] = pd.to_datetime(pagadas["invoice_date"], errors="coerce")
    pagadas["invoice_date_due"] = pd.to_datetime(pagadas["invoice_date_due"], errors="coerce")
    pagadas["fecha_pago"] = pd.to_datetime(pagadas["date"], errors="coerce")
    pagadas["dias_de_mora"] = (pagadas["fecha_pago"] - pagadas["invoice_date_due"]).dt.days
    pagadas["plazo"] = (pagadas["invoice_date_due"] - pagadas["invoice_date"]).dt.days
    pagadas["monto"] = pagadas["amount_total_signed"].abs()
    pagadas["factura"] = pagadas.get("name", pagadas.get("ref", ""))
    return pagadas[cols].sort_values("fecha_pago")
