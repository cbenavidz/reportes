# -*- coding: utf-8 -*-
"""
Motor de recomendaciones de cobro.

Genera un plan accionable que prioriza:
1. A quién contactar primero (mayor impacto en cartera, mayor riesgo)
2. Qué tono usar (recordatorio, gestión, jurídico)
3. Sugerencias de cupo y plazo según calificación
4. Próximos vencimientos para cobro proactivo
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from enum import Enum

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class CollectionPriority(str, Enum):
    URGENTE = "URGENTE"  # Mora >90 o calificación D
    ALTA = "ALTA"        # Mora 30-90 o monto alto
    MEDIA = "MEDIA"      # Mora 1-30
    PROACTIVA = "PROACTIVA"  # Por vencer
    BAJA = "BAJA"


class CollectionAction(str, Enum):
    LLAMADA_RECORDATORIO = "Llamada de recordatorio amable"
    EMAIL_RECORDATORIO = "Email de recordatorio"
    LLAMADA_GESTION = "Llamada de gestión de cobro"
    VISITA_COMERCIAL = "Visita o reunión presencial"
    ACUERDO_PAGO = "Proponer acuerdo de pago"
    ULTIMA_GESTION = "Última gestión de cobro pre-jurídico"
    JURIDICO = "Pasar a jurídico"
    BLOQUEAR_CREDITO = "Bloquear crédito temporalmente"


def _priorizar_cliente(row: pd.Series) -> tuple[CollectionPriority, CollectionAction]:
    """Determina prioridad y acción según métricas del cliente."""
    dias_max = float(row.get("dias_vencido_max") or 0)
    calificacion = row.get("calificacion", "SIN_HISTORICO")
    saldo_vencido = float(row.get("monto_vencido") or 0)
    saldo_total = float(row.get("saldo_actual") or 0)

    # Sin saldo o sin vencido → no requiere gestión
    if saldo_total <= 0:
        return CollectionPriority.BAJA, CollectionAction.EMAIL_RECORDATORIO
    if saldo_vencido <= 0 and dias_max <= 0:
        return CollectionPriority.PROACTIVA, CollectionAction.EMAIL_RECORDATORIO

    # Casos críticos
    if dias_max > 120:
        return CollectionPriority.URGENTE, CollectionAction.JURIDICO
    if dias_max > 90:
        return CollectionPriority.URGENTE, CollectionAction.ULTIMA_GESTION
    if calificacion == "D" and dias_max > 30:
        return CollectionPriority.URGENTE, CollectionAction.BLOQUEAR_CREDITO

    # Casos altos
    if dias_max > 60:
        return CollectionPriority.ALTA, CollectionAction.ACUERDO_PAGO
    if dias_max > 30:
        return CollectionPriority.ALTA, CollectionAction.LLAMADA_GESTION
    if saldo_vencido > 0 and saldo_vencido > 50_000_000:  # umbral COP
        return CollectionPriority.ALTA, CollectionAction.VISITA_COMERCIAL

    # Casos medios
    if dias_max > 0:
        return CollectionPriority.MEDIA, CollectionAction.LLAMADA_RECORDATORIO

    return CollectionPriority.PROACTIVA, CollectionAction.EMAIL_RECORDATORIO


def _sugerir_cupo_plazo(row: pd.Series) -> dict[str, str]:
    """Sugiere ajustes a cupo y plazo según calificación."""
    calificacion = row.get("calificacion", "SIN_HISTORICO")
    saldo = float(row.get("saldo_actual") or 0)
    cupo = float(row.get("credit_limit") or 0)

    if calificacion == "A":
        return {
            "sugerencia_cupo": "Mantener o ampliar cupo. Cliente confiable.",
            "sugerencia_plazo": "Plazos amplios (30-60 días). Considerar descuento por pronto pago.",
        }
    if calificacion == "B":
        return {
            "sugerencia_cupo": "Mantener cupo actual. Monitorear trimestralmente.",
            "sugerencia_plazo": "Plazos estándar (30 días).",
        }
    if calificacion == "C":
        ajuste = "Reducir cupo 20-30%" if cupo and saldo / cupo > 0.5 else "Mantener cupo, no aumentar."
        return {
            "sugerencia_cupo": ajuste,
            "sugerencia_plazo": "Acortar plazo a 15-20 días. Pedir abonos parciales.",
        }
    if calificacion == "D":
        return {
            "sugerencia_cupo": "Bloquear crédito hasta normalizar cartera vencida.",
            "sugerencia_plazo": "Solo prepago o entrega contra pago.",
        }
    return {
        "sugerencia_cupo": "Asignar cupo conservador. Construir histórico antes de ampliar.",
        "sugerencia_plazo": "Plazo corto (15 días) hasta tener histórico de 3-6 meses.",
    }


# ---------------------------------------------------------------------------
# API principal
# ---------------------------------------------------------------------------


def build_collection_plan(
    by_partner_scored: pd.DataFrame,
    open_invoices: pd.DataFrame | None = None,
    top_n: int | None = None,
) -> pd.DataFrame:
    """
    Construye plan de cobro priorizado por cliente.

    Args:
        by_partner_scored: DataFrame de clientes con scoring aplicado.
        open_invoices: Facturas abiertas con días vencidos (opcional, enriquece).
        top_n: Si se pasa, devuelve solo los top_n clientes a contactar.

    Returns:
        DataFrame con columnas: prioridad, accion, cliente, telefono, email,
        saldo_actual, monto_vencido, dias_vencido_max, calificacion,
        sugerencia_cupo, sugerencia_plazo, observaciones.
    """
    if by_partner_scored.empty:
        return pd.DataFrame()

    # Filtrar clientes con saldo
    df = by_partner_scored[by_partner_scored["saldo_actual"] > 0].copy()
    if df.empty:
        return pd.DataFrame()

    # Calcular prioridad y acción
    prioridades = df.apply(
        lambda row: pd.Series(_priorizar_cliente(row), index=["prioridad", "accion"]),
        axis=1,
    )
    df = pd.concat([df, prioridades], axis=1)

    # Sugerencias de cupo/plazo
    sugerencias = df.apply(_sugerir_cupo_plazo, axis=1, result_type="expand")
    df = pd.concat([df, sugerencias], axis=1)

    # Observaciones en lenguaje natural
    def observar(row) -> str:
        obs = []
        if row.get("dias_vencido_max", 0) > 0:
            obs.append(f"Factura más vencida: {int(row['dias_vencido_max'])} días")
        if row.get("monto_vencido", 0) > 0:
            obs.append(f"Vencido: {float(row['monto_vencido']):,.0f}")
        if row.get("ultimo_pago") and pd.notna(row["ultimo_pago"]):
            try:
                ult = pd.Timestamp(row["ultimo_pago"]).strftime("%Y-%m-%d")
                obs.append(f"Último pago: {ult}")
            except Exception:
                pass
        if row.get("habito_pago"):
            obs.append(row["habito_pago"])
        return " | ".join(obs)

    df["observaciones"] = df.apply(observar, axis=1)

    # Score de prioridad (numérico para ordenar)
    pri_order = {"URGENTE": 0, "ALTA": 1, "MEDIA": 2, "PROACTIVA": 3, "BAJA": 4}
    df["_pri_order"] = df["prioridad"].astype(str).map(
        lambda x: pri_order.get(x, 99)
    )

    # Convertir Enums a string para serialización
    df["prioridad"] = df["prioridad"].astype(str).str.replace("CollectionPriority.", "", regex=False)
    df["accion"] = df["accion"].astype(str).str.replace("CollectionAction.", "", regex=False)

    # Columnas de salida ordenadas
    cols_out = [
        "prioridad",
        "accion",
        "partner_name",
        "phone",
        "mobile",
        "email",
        "saldo_actual",
        "monto_vencido",
        "dias_vencido_max",
        "calificacion",
        "score_total",
        "sugerencia_cupo",
        "sugerencia_plazo",
        "observaciones",
        "partner_id",
    ]
    cols_present = [c for c in cols_out if c in df.columns]

    df = df.sort_values(
        ["_pri_order", "monto_vencido", "saldo_actual"],
        ascending=[True, False, False],
    )[cols_present].reset_index(drop=True)

    if top_n is not None:
        df = df.head(top_n)

    return df


def upcoming_dues(
    open_invoices: pd.DataFrame,
    days_ahead: int = 7,
    cutoff_date: date | None = None,
) -> pd.DataFrame:
    """
    Facturas que vencen en los próximos N días — para cobro proactivo.

    Devuelve DataFrame con: partner_name, factura, fecha_vencimiento,
    dias_para_vencer, saldo.
    """
    if open_invoices.empty:
        return pd.DataFrame()

    cutoff = pd.Timestamp(cutoff_date or datetime.now().date())
    horizon = cutoff + pd.Timedelta(days=days_ahead)

    df = open_invoices.copy()
    due = df.get("fecha_vencimiento_efectiva", df["invoice_date_due"]).fillna(
        df["invoice_date"]
    )
    df["fecha_vencimiento"] = due
    df["dias_para_vencer"] = (due - cutoff).dt.days

    proximas = df[(df["dias_para_vencer"] >= 0) & (df["dias_para_vencer"] <= days_ahead)]
    proximas = proximas.sort_values(["dias_para_vencer", "amount_residual_signed"])

    cols = [
        "partner_name",
        "name",
        "fecha_vencimiento",
        "dias_para_vencer",
        "amount_residual_signed",
    ]
    return proximas[[c for c in cols if c in proximas.columns]].rename(
        columns={
            "name": "factura",
            "amount_residual_signed": "saldo",
        }
    )
