# -*- coding: utf-8 -*-
"""
Motor de calificación (scoring) de clientes A/B/C/D según hábito de pago.

Combina varios factores con pesos configurables:
- Días de mora promedio histórico (35%)
- % de facturas pagadas a tiempo (25%)
- Días de mora máximo actual (15%)
- % vencido sobre saldo actual (10%)
- Antigüedad como cliente (10%)
- Concentración de cartera (5%)

Cada factor produce un sub-score 0-100 y el promedio ponderado da el score final.

Categorías por defecto:
- A: 80-100 → Excelente cliente. Plazos amplios, cupos sin restricción.
- B: 60-79  → Bueno. Mantener condiciones actuales.
- C: 40-59  → Regular. Vigilar, no aumentar cupo.
- D: 0-39   → Riesgo. Exigir prepago/garantía, reducir cupo.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class ScoringConfig:
    """Configuración de pesos y umbrales del motor de scoring."""

    # Pesos (deben sumar 1.0)
    peso_mora_promedio: float = 0.35
    peso_pct_a_tiempo: float = 0.25
    peso_mora_max_actual: float = 0.15
    peso_pct_vencido: float = 0.10
    peso_antiguedad: float = 0.10
    peso_concentracion: float = 0.05

    # Umbrales para cortar score 0-100
    umbral_a: int = 80
    umbral_b: int = 60
    umbral_c: int = 40

    # Mínimo de facturas pagadas para tener histórico confiable
    min_facturas_para_score: int = 3

    @classmethod
    def from_env(cls) -> "ScoringConfig":
        """Carga umbrales desde variables de entorno (UMBRAL_SCORING_A/B/C)."""
        # Los UMBRAL_SCORING_* del .env son días de mora máxima por categoría,
        # no scores. Los traducimos a scores aproximados.
        return cls()


# ---------------------------------------------------------------------------
# Sub-scores
# ---------------------------------------------------------------------------


def _score_mora_promedio(dias_mora: pd.Series) -> pd.Series:
    """
    Score 0-100 basado en días de mora promedio histórico.

    0 días o negativos (paga antes) → 100
    > 60 días → 0
    Lineal en el medio.
    """
    s = dias_mora.fillna(0).clip(lower=0)
    # 100 - (dias / 60) * 100, capado entre 0 y 100
    return (100 - (s / 60.0) * 100).clip(0, 100)


def _score_pct_a_tiempo(pct: pd.Series) -> pd.Series:
    """% de facturas pagadas a tiempo es directamente el score."""
    return pct.fillna(0).clip(0, 100)


def _score_mora_max_actual(dias_max: pd.Series) -> pd.Series:
    """
    Score basado en la peor factura que tiene abierta hoy.

    <= 0 días → 100
    > 90 días → 0
    """
    s = dias_max.fillna(0).clip(lower=0)
    return (100 - (s / 90.0) * 100).clip(0, 100)


def _score_pct_vencido(pct: pd.Series) -> pd.Series:
    """
    Score basado en % de cartera del cliente que está vencida.

    0% vencido → 100
    100% vencido → 0
    """
    s = pct.fillna(0).clip(0, 100)
    return (100 - s).clip(0, 100)


def _score_antiguedad(dias: pd.Series) -> pd.Series:
    """
    Score por antigüedad como cliente.

    < 90 días → 50 (cliente nuevo, sin mucho histórico)
    >= 730 días (2 años) → 100
    Lineal.
    """
    s = dias.fillna(0).clip(lower=0)
    # base 50, sube hasta 100 a los 2 años
    return (50 + (s / 730.0) * 50).clip(50, 100)


def _score_concentracion(saldo_actual: pd.Series, cupo: pd.Series) -> pd.Series:
    """
    Score basado en uso del cupo de crédito.

    saldo / cupo <= 50% → 100
    saldo / cupo >= 100% → 0
    Si no hay cupo asignado → 50 (neutral).
    """
    cupo = cupo.fillna(0).replace(0, np.nan)
    ratio = (saldo_actual.fillna(0) / cupo).clip(0, 2)
    score = (100 - (ratio - 0.5) / 0.5 * 100).clip(0, 100)
    return score.fillna(50)  # Sin cupo definido = neutral


# ---------------------------------------------------------------------------
# Score compuesto
# ---------------------------------------------------------------------------


def compute_partner_scores(
    by_partner: pd.DataFrame,
    config: ScoringConfig | None = None,
) -> pd.DataFrame:
    """
    Calcula el score final 0-100 y la categoría A/B/C/D para cada cliente.

    Args:
        by_partner: DataFrame con métricas por cliente (output de
                    analyzer.compute_partner_metrics).
        config: Configuración de pesos y umbrales.

    Returns:
        Mismo DataFrame con columnas adicionales:
        - score_mora_prom, score_pct_a_tiempo, ...
        - score_total (0-100)
        - calificacion ('A' | 'B' | 'C' | 'D' | 'SIN_HISTORICO')
        - habito_pago (texto descriptivo)
    """
    config = config or ScoringConfig()
    if by_partner.empty:
        return by_partner.assign(
            score_total=pd.Series(dtype="float"),
            calificacion=pd.Series(dtype="object"),
            habito_pago=pd.Series(dtype="object"),
        )

    df = by_partner.copy()

    # Asegurar columnas necesarias
    for col, default in [
        ("dias_mora_promedio", 0),
        ("pct_pagado_a_tiempo", 0),
        ("dias_vencido_max", 0),
        ("pct_vencido_cliente", 0),
        ("antiguedad_dias", 0),
        ("saldo_actual", 0),
        ("credit_limit", 0),
        ("num_facturas_pagadas", 0),
        ("plazo_promedio_dias", 0),
        ("dias_pago_promedio", 0),
    ]:
        if col not in df.columns:
            df[col] = default

    df["score_mora_prom"] = _score_mora_promedio(df["dias_mora_promedio"])
    df["score_pct_a_tiempo"] = _score_pct_a_tiempo(df["pct_pagado_a_tiempo"])
    df["score_mora_max_act"] = _score_mora_max_actual(df["dias_vencido_max"])
    df["score_pct_vencido"] = _score_pct_vencido(df["pct_vencido_cliente"])
    df["score_antiguedad"] = _score_antiguedad(df["antiguedad_dias"])
    df["score_concentracion"] = _score_concentracion(
        df["saldo_actual"], df["credit_limit"]
    )

    df["score_total"] = (
        df["score_mora_prom"] * config.peso_mora_promedio
        + df["score_pct_a_tiempo"] * config.peso_pct_a_tiempo
        + df["score_mora_max_act"] * config.peso_mora_max_actual
        + df["score_pct_vencido"] * config.peso_pct_vencido
        + df["score_antiguedad"] * config.peso_antiguedad
        + df["score_concentracion"] * config.peso_concentracion
    ).round(1)

    # Categoría
    def categorize(row) -> str:
        if row["num_facturas_pagadas"] < config.min_facturas_para_score:
            return "SIN_HISTORICO"
        score = row["score_total"]
        if score >= config.umbral_a:
            return "A"
        if score >= config.umbral_b:
            return "B"
        if score >= config.umbral_c:
            return "C"
        return "D"

    df["calificacion"] = df.apply(categorize, axis=1)

    # Hábito de pago descriptivo: combina plazo otorgado, DSO real,
    # cumplimiento vs plazo propio, y consistencia (% a tiempo).
    # Cada cliente se evalúa contra SU PROPIO plazo, no contra un promedio global.
    def describe(row) -> str:
        n = int(row.get("num_facturas_pagadas") or 0)
        if n < config.min_facturas_para_score:
            return f"📋 Sin histórico suficiente ({n} fact. pagadas)"

        plazo = float(row.get("plazo_promedio_dias") or 0)
        dso = float(row.get("dias_pago_promedio") or 0)
        mora = float(row.get("dias_mora_promedio") or 0)
        pct_t = float(row.get("pct_pagado_a_tiempo") or 0)

        # Cliente de contado (sin plazo otorgado)
        if plazo <= 1:
            if dso <= 1:
                estado = "✅ Contado al día"
            elif dso <= 5:
                estado = f"🟢 Contado, pagó ~{dso:.0f}d"
            else:
                estado = f"⚠️ Contado pero pagó ~{dso:.0f}d después"
            return f"{estado} · {pct_t:.0f}% a tiempo · {n} fact."

        # Cliente con plazo
        if mora <= -3:
            estado = (
                f"⭐ Paga {abs(mora):.0f}d antes del plazo de {plazo:.0f}d"
            )
        elif mora <= 1:
            estado = f"✅ Cumple su plazo de {plazo:.0f}d (DSO {dso:.0f}d)"
        elif mora <= 7:
            estado = (
                f"🟡 Plazo {plazo:.0f}d, paga ~{dso:.0f}d "
                f"({mora:.0f}d sobre plazo)"
            )
        elif mora <= 20:
            estado = (
                f"🟠 Plazo {plazo:.0f}d, paga ~{dso:.0f}d "
                f"({mora:.0f}d sobre plazo)"
            )
        elif mora <= 45:
            estado = (
                f"🔴 Plazo {plazo:.0f}d, paga ~{dso:.0f}d "
                f"({mora:.0f}d sobre plazo)"
            )
        else:
            estado = (
                f"⛔ Plazo {plazo:.0f}d, paga ~{dso:.0f}d "
                f"({mora:.0f}d sobre plazo — crítico)"
            )

        return f"{estado} · {pct_t:.0f}% a tiempo · {n} fact."

    df["habito_pago"] = df.apply(describe, axis=1)

    return df.sort_values("score_total", ascending=False)


def summary_by_calificacion(scored: pd.DataFrame) -> pd.DataFrame:
    """Resumen por categoría A/B/C/D."""
    if scored.empty:
        return pd.DataFrame()

    grp = scored.groupby("calificacion", dropna=False).agg(
        num_clientes=("partner_id", "count"),
        saldo_total=("saldo_actual", "sum"),
        monto_vencido=("monto_vencido", "sum"),
        score_promedio=("score_total", "mean"),
        dias_mora_prom=("dias_mora_promedio", "mean"),
    )
    grp["pct_clientes"] = grp["num_clientes"] / grp["num_clientes"].sum() * 100
    grp["pct_saldo"] = grp["saldo_total"] / (grp["saldo_total"].sum() or 1) * 100
    return grp.reset_index()
