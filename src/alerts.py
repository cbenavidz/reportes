# -*- coding: utf-8 -*-
"""
Sistema de alertas de riesgo de cartera.

Reglas configurables que disparan alertas con nivel (info, warning, critical):

- Factura vencida > 90 días
- Cliente con > 50% de cartera vencida
- Cliente con score < 40 (calificación D)
- Cliente que excede su cupo de crédito
- Caída fuerte en score reciente (deterioro)
- Cliente sin pagos en 60+ días pero con saldo activo
- Concentración: cliente con > 20% del saldo total de cartera
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum

import pandas as pd

logger = logging.getLogger(__name__)


class AlertLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class Alert:
    """Una alerta concreta sobre un cliente o factura."""

    level: AlertLevel
    rule: str
    title: str
    message: str
    partner_id: int | None = None
    partner_name: str | None = None
    invoice_id: int | None = None
    invoice_name: str | None = None
    amount: float = 0.0
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "nivel": self.level.value,
            "regla": self.rule,
            "titulo": self.title,
            "mensaje": self.message,
            "partner_id": self.partner_id,
            "cliente": self.partner_name,
            "factura": self.invoice_name,
            "monto": self.amount,
        }


@dataclass
class AlertConfig:
    """Umbrales configurables para las reglas de alertas."""

    dias_vencido_critico: int = 90
    dias_vencido_warning: int = 60
    pct_vencido_alto: float = 50.0
    score_riesgo: float = 40.0
    dias_sin_pago: int = 60
    concentracion_alta_pct: float = 20.0
    exceso_cupo_warning_pct: float = 90.0


# ---------------------------------------------------------------------------
# Reglas individuales
# ---------------------------------------------------------------------------


def _rule_facturas_muy_vencidas(
    open_invoices: pd.DataFrame, config: AlertConfig
) -> list[Alert]:
    """Facturas con más de N días de vencidas."""
    if open_invoices.empty or "dias_vencido" not in open_invoices.columns:
        return []

    alerts: list[Alert] = []
    criticas = open_invoices[open_invoices["dias_vencido"] >= config.dias_vencido_critico]
    for _, row in criticas.iterrows():
        alerts.append(
            Alert(
                level=AlertLevel.CRITICAL,
                rule="factura_critica",
                title=f"Factura {row.get('name', '')} con {int(row['dias_vencido'])} días de vencida",
                message=(
                    f"La factura {row.get('name', '')} del cliente "
                    f"{row.get('partner_name', '')} lleva {int(row['dias_vencido'])} "
                    f"días vencida (saldo: {abs(float(row.get('amount_residual_signed', 0))):,.0f})."
                ),
                partner_id=int(row["partner_id"]) if pd.notna(row.get("partner_id")) else None,
                partner_name=row.get("partner_name"),
                invoice_id=int(row["id"]) if pd.notna(row.get("id")) else None,
                invoice_name=row.get("name"),
                amount=abs(float(row.get("amount_residual_signed", 0))),
            )
        )
    return alerts


def _rule_cliente_alto_vencido(
    by_partner_scored: pd.DataFrame, config: AlertConfig
) -> list[Alert]:
    """Cliente con porcentaje alto de su cartera vencida."""
    if by_partner_scored.empty:
        return []

    alerts: list[Alert] = []
    riesgosos = by_partner_scored[
        (by_partner_scored["pct_vencido_cliente"] >= config.pct_vencido_alto)
        & (by_partner_scored["saldo_actual"] > 0)
    ]
    for _, row in riesgosos.iterrows():
        alerts.append(
            Alert(
                level=AlertLevel.WARNING,
                rule="alto_vencido_cliente",
                title=f"{row.get('partner_name', '')}: {row['pct_vencido_cliente']:.0f}% vencido",
                message=(
                    f"El cliente tiene {row['pct_vencido_cliente']:.0f}% de su cartera "
                    f"vencida. Saldo actual: {float(row['saldo_actual']):,.0f}, "
                    f"vencido: {float(row.get('monto_vencido', 0)):,.0f}."
                ),
                partner_id=int(row["partner_id"]) if pd.notna(row.get("partner_id")) else None,
                partner_name=row.get("partner_name"),
                amount=float(row.get("monto_vencido", 0)),
            )
        )
    return alerts


def _rule_score_bajo(
    by_partner_scored: pd.DataFrame, config: AlertConfig
) -> list[Alert]:
    """Cliente con score por debajo del umbral de riesgo y saldo activo."""
    if by_partner_scored.empty or "score_total" not in by_partner_scored.columns:
        return []

    alerts: list[Alert] = []
    riesgosos = by_partner_scored[
        (by_partner_scored["score_total"] < config.score_riesgo)
        & (by_partner_scored["saldo_actual"] > 0)
        & (by_partner_scored["calificacion"] == "D")
    ]
    for _, row in riesgosos.iterrows():
        alerts.append(
            Alert(
                level=AlertLevel.CRITICAL,
                rule="score_bajo",
                title=f"{row.get('partner_name', '')}: calificación D (score {row['score_total']:.0f})",
                message=(
                    f"Cliente con calificación de riesgo. Hábito: {row.get('habito_pago', 'N/A')}. "
                    f"Saldo actual: {float(row['saldo_actual']):,.0f}."
                ),
                partner_id=int(row["partner_id"]) if pd.notna(row.get("partner_id")) else None,
                partner_name=row.get("partner_name"),
                amount=float(row.get("saldo_actual", 0)),
            )
        )
    return alerts


def _rule_exceso_cupo(
    by_partner_scored: pd.DataFrame, config: AlertConfig
) -> list[Alert]:
    """Cliente que excede o está cerca de exceder su cupo de crédito."""
    if by_partner_scored.empty:
        return []

    alerts: list[Alert] = []
    df = by_partner_scored.copy()
    df = df[df["credit_limit"].fillna(0) > 0]
    df["uso_cupo_pct"] = df["saldo_actual"] / df["credit_limit"] * 100

    # Crítico: pasó del cupo
    excedidos = df[df["uso_cupo_pct"] > 100]
    for _, row in excedidos.iterrows():
        alerts.append(
            Alert(
                level=AlertLevel.CRITICAL,
                rule="exceso_cupo",
                title=f"{row.get('partner_name', '')} excedió cupo ({row['uso_cupo_pct']:.0f}%)",
                message=(
                    f"Cupo asignado: {float(row['credit_limit']):,.0f}, "
                    f"saldo actual: {float(row['saldo_actual']):,.0f}. "
                    f"Bloquear nuevas facturas hasta regularizar."
                ),
                partner_id=int(row["partner_id"]) if pd.notna(row.get("partner_id")) else None,
                partner_name=row.get("partner_name"),
                amount=float(row["saldo_actual"]) - float(row["credit_limit"]),
            )
        )

    # Warning: cerca del cupo
    cerca = df[
        (df["uso_cupo_pct"] >= config.exceso_cupo_warning_pct)
        & (df["uso_cupo_pct"] <= 100)
    ]
    for _, row in cerca.iterrows():
        alerts.append(
            Alert(
                level=AlertLevel.WARNING,
                rule="cerca_cupo",
                title=f"{row.get('partner_name', '')} al {row['uso_cupo_pct']:.0f}% del cupo",
                message=(
                    f"Cupo: {float(row['credit_limit']):,.0f}, saldo: "
                    f"{float(row['saldo_actual']):,.0f}. Vigilar."
                ),
                partner_id=int(row["partner_id"]) if pd.notna(row.get("partner_id")) else None,
                partner_name=row.get("partner_name"),
                amount=float(row["saldo_actual"]),
            )
        )

    return alerts


def _rule_concentracion_alta(
    by_partner_scored: pd.DataFrame, config: AlertConfig
) -> list[Alert]:
    """Cliente que concentra mucho del saldo total → riesgo de concentración."""
    if by_partner_scored.empty:
        return []

    saldo_total = by_partner_scored["saldo_actual"].sum()
    if saldo_total <= 0:
        return []

    df = by_partner_scored.copy()
    df["pct_concentracion"] = df["saldo_actual"] / saldo_total * 100
    concentrados = df[df["pct_concentracion"] >= config.concentracion_alta_pct]

    alerts: list[Alert] = []
    for _, row in concentrados.iterrows():
        alerts.append(
            Alert(
                level=AlertLevel.WARNING,
                rule="concentracion_alta",
                title=f"{row.get('partner_name', '')}: {row['pct_concentracion']:.0f}% de la cartera",
                message=(
                    f"Este cliente concentra {row['pct_concentracion']:.0f}% del saldo total "
                    f"de cartera ({float(row['saldo_actual']):,.0f}). "
                    f"Considerar diversificación y seguimiento estrecho."
                ),
                partner_id=int(row["partner_id"]) if pd.notna(row.get("partner_id")) else None,
                partner_name=row.get("partner_name"),
                amount=float(row["saldo_actual"]),
            )
        )
    return alerts


def _rule_sin_pagos_recientes(
    by_partner_scored: pd.DataFrame,
    config: AlertConfig,
    cutoff_date: date | None = None,
) -> list[Alert]:
    """Cliente con saldo activo pero sin pagos en N días."""
    if by_partner_scored.empty or "ultimo_pago" not in by_partner_scored.columns:
        return []

    cutoff = pd.Timestamp(cutoff_date or datetime.now().date())
    df = by_partner_scored.copy()
    df["dias_sin_pago"] = (cutoff - pd.to_datetime(df["ultimo_pago"], errors="coerce")).dt.days

    silenciosos = df[
        (df["saldo_actual"] > 0)
        & (df["dias_sin_pago"] >= config.dias_sin_pago)
    ]
    alerts: list[Alert] = []
    for _, row in silenciosos.iterrows():
        alerts.append(
            Alert(
                level=AlertLevel.WARNING,
                rule="sin_pagos",
                title=f"{row.get('partner_name', '')}: sin pagos en {int(row['dias_sin_pago'])} días",
                message=(
                    f"Tiene saldo de {float(row['saldo_actual']):,.0f} pero no ha hecho "
                    f"pagos hace {int(row['dias_sin_pago'])} días. Contactar."
                ),
                partner_id=int(row["partner_id"]) if pd.notna(row.get("partner_id")) else None,
                partner_name=row.get("partner_name"),
                amount=float(row["saldo_actual"]),
            )
        )
    return alerts


# ---------------------------------------------------------------------------
# Orquestador
# ---------------------------------------------------------------------------


def generate_alerts(
    open_invoices: pd.DataFrame,
    by_partner_scored: pd.DataFrame,
    config: AlertConfig | None = None,
    cutoff_date: date | None = None,
) -> pd.DataFrame:
    """
    Ejecuta todas las reglas y devuelve un DataFrame consolidado de alertas.
    """
    config = config or AlertConfig()

    all_alerts: list[Alert] = []
    all_alerts.extend(_rule_facturas_muy_vencidas(open_invoices, config))
    all_alerts.extend(_rule_cliente_alto_vencido(by_partner_scored, config))
    all_alerts.extend(_rule_score_bajo(by_partner_scored, config))
    all_alerts.extend(_rule_exceso_cupo(by_partner_scored, config))
    all_alerts.extend(_rule_concentracion_alta(by_partner_scored, config))
    all_alerts.extend(_rule_sin_pagos_recientes(by_partner_scored, config, cutoff_date))

    if not all_alerts:
        return pd.DataFrame(
            columns=["nivel", "regla", "titulo", "mensaje", "cliente", "factura", "monto"]
        )

    df = pd.DataFrame([a.to_dict() for a in all_alerts])

    # Ordenar: críticas primero, luego por monto descendente
    level_order = {"critical": 0, "warning": 1, "info": 2}
    df["_order"] = df["nivel"].map(level_order)
    df = df.sort_values(["_order", "monto"], ascending=[True, False]).drop(columns=["_order"])
    return df.reset_index(drop=True)
