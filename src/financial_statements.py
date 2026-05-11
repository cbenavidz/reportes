# -*- coding: utf-8 -*-
"""
Motor de Estados Financieros — Balance, P&L, KTNO, Flujo.

Trabaja sobre dos DataFrames:
  - chart: plan de cuentas (account.account) con `code`, `name`, `account_type`
  - moves: movimientos (account.move.line) con `account_id`, `debit`, `credit`,
           `balance`, `date`

Clasificación PUC colombiano (primer dígito del código):
  1xxx → Activo
  2xxx → Pasivo
  3xxx → Patrimonio
  4xxx → Ingresos
  5xxx → Gastos operacionales
  6xxx → Costo de ventas
  7xxx → Costos de producción
  8/9xxx → Cuentas de orden (no afectan estados financieros)

Cuentas clave para KTNO:
  1305xx → Clientes (cuentas por cobrar)
  1435xx, 1465xx → Inventarios
  2205xx → Proveedores nacionales
  2335xx → Costos y gastos por pagar

Para Working Capital:
  Activo corriente: 11xx (disponible) + 13xx (deudores) + 14xx (inventarios)
                    + 17xx (diferidos corto plazo)
  Pasivo corriente: 21xx + 22xx + 23xx + 24xx + 25xx + 26xx (corto plazo)
"""
from __future__ import annotations

import logging
from datetime import date

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# =============================================================================
# Clasificación PUC
# =============================================================================

PUC_GROUPS = {
    "1": "Activo",
    "2": "Pasivo",
    "3": "Patrimonio",
    "4": "Ingresos",
    "5": "Gastos",
    "6": "Costo de ventas",
    "7": "Costos de producción",
    "8": "Cuentas de orden D",
    "9": "Cuentas de orden A",
}


def classify_account(code: str) -> dict:
    """
    Clasifica una cuenta por su código PUC. Devuelve:
      grupo: "Activo" | "Pasivo" | etc.
      es_corriente: True/False (corto plazo)
      es_resultado: True si afecta P&L (4, 5, 6, 7)
      subgrupo: descripción más fina

    Robusto contra códigos con prefijos no estándar (ceros iniciales,
    letras como "F", "A", etc.). Busca el primer dígito 1-7 dentro del
    código que sea válido como grupo PUC.
    """
    code = str(code or "")
    if not code:
        return {"grupo": "Otro", "es_corriente": False,
                "es_resultado": False, "subgrupo": "Otro"}

    # Buscar el primer dígito 1-7 en el código (saltando ceros y letras)
    g = None
    for ch in code:
        if ch in "1234567":
            g = ch
            break
    if g is None:
        return {"grupo": "Otro", "es_corriente": False,
                "es_resultado": False, "subgrupo": "Otro"}

    # Para subclasificación, extraer los primeros 4 dígitos significativos
    # (saltando ceros y letras iniciales)
    digits = "".join(ch for ch in code if ch.isdigit())
    digits = digits.lstrip("0")  # quitar ceros iniciales

    # Reconstruir prefijos
    g = digits[0] if digits else g
    code_for_subclass = digits if digits else code
    grupo = PUC_GROUPS.get(g, "Otro")
    es_resultado = g in ("4", "5", "6", "7")

    # Usar code_for_subclass (dígitos sin prefijos) para subclasificar
    c = code_for_subclass
    es_corriente = False
    subgrupo = grupo

    # Activos corrientes: 11 (disponible), 12 (inversiones CP), 13 (deudores),
    # 14 (inventarios), 17 (diferidos)
    if g == "1":
        if c[:2] in ("11", "12", "13", "14", "17"):
            es_corriente = True
        # Subgrupo
        if c.startswith("11"):
            subgrupo = "Disponible (caja, bancos)"
        elif c.startswith("12"):
            subgrupo = "Inversiones"
        elif c.startswith("13"):
            subgrupo = "Deudores (CxC)"
        elif c.startswith("14"):
            subgrupo = "Inventarios"
        elif c.startswith("15"):
            subgrupo = "Propiedad, planta y equipo"
        elif c.startswith("16"):
            subgrupo = "Intangibles"
        elif c.startswith("17"):
            subgrupo = "Diferidos"
        elif c.startswith("18"):
            subgrupo = "Otros activos"
        elif c.startswith("19"):
            subgrupo = "Valorizaciones"
    elif g == "2":
        if c[:2] in ("21", "22", "23", "24", "25", "26", "27", "28"):
            es_corriente = True
        if c.startswith("21"):
            subgrupo = "Obligaciones financieras"
        elif c.startswith("22"):
            subgrupo = "Proveedores"
        elif c.startswith("23"):
            subgrupo = "Cuentas por pagar"
        elif c.startswith("24"):
            subgrupo = "Impuestos por pagar"
        elif c.startswith("25"):
            subgrupo = "Obligaciones laborales"
        elif c.startswith("26"):
            subgrupo = "Pasivos estimados"
        elif c.startswith("27"):
            subgrupo = "Diferidos"
        elif c.startswith("28"):
            subgrupo = "Otros pasivos"
    elif g == "3":
        if c.startswith("31"):
            subgrupo = "Capital social"
        elif c.startswith("32"):
            subgrupo = "Superávit de capital"
        elif c.startswith("33"):
            subgrupo = "Reservas"
        elif c.startswith("36"):
            subgrupo = "Resultados del ejercicio"
        elif c.startswith("37"):
            subgrupo = "Resultados de ejercicios anteriores"
    elif g == "4":
        if c.startswith("41"):
            subgrupo = "Ingresos operacionales"
        elif c.startswith("42"):
            subgrupo = "Ingresos no operacionales"
    elif g == "5":
        if c.startswith("51"):
            subgrupo = "Gastos administrativos"
        elif c.startswith("52"):
            subgrupo = "Gastos de ventas"
        elif c.startswith("53"):
            subgrupo = "Gastos no operacionales"
        elif c.startswith("54"):
            subgrupo = "Impuesto de renta"
    elif g == "6":
        subgrupo = "Costo de ventas"

    return {
        "grupo": grupo,
        "es_corriente": es_corriente,
        "es_resultado": es_resultado,
        "subgrupo": subgrupo,
    }


def enrich_chart_with_puc(chart: pd.DataFrame) -> pd.DataFrame:
    """Agrega columnas 'grupo', 'es_corriente', 'es_resultado', 'subgrupo'."""
    if chart is None or chart.empty:
        return chart
    out = chart.copy()
    cls = out["code"].astype(str).apply(classify_account)
    out["grupo"] = cls.apply(lambda d: d["grupo"])
    out["es_corriente"] = cls.apply(lambda d: d["es_corriente"])
    out["es_resultado"] = cls.apply(lambda d: d["es_resultado"])
    out["subgrupo"] = cls.apply(lambda d: d["subgrupo"])
    return out


# =============================================================================
# Helpers
# =============================================================================

def _filter_moves(
    moves: pd.DataFrame,
    date_from: date | None = None,
    date_to: date | None = None,
) -> pd.DataFrame:
    if moves is None or moves.empty:
        return pd.DataFrame()
    sub = moves.copy()
    if "date" in sub.columns:
        sub["date"] = pd.to_datetime(sub["date"], errors="coerce")
        sub = sub.dropna(subset=["date"])
        if date_from is not None:
            sub = sub[sub["date"] >= pd.Timestamp(date_from)]
        if date_to is not None:
            sub = sub[sub["date"] <= pd.Timestamp(date_to)]
    return sub


def _normalize_code(code: str) -> str:
    """
    Normaliza un código de cuenta para clasificación PUC:
      - Quita prefijos no numéricos (F, A, etc.)
      - Quita ceros iniciales
      - Devuelve solo los dígitos significativos
    Ejemplos: "F4135" → "4135", "00004135" → "4135", "FF1105" → "1105"
    """
    s = str(code or "")
    digits = "".join(ch for ch in s if ch.isdigit())
    digits = digits.lstrip("0")
    return digits or s


def _join_moves_chart(
    moves: pd.DataFrame, chart: pd.DataFrame
) -> pd.DataFrame:
    """Une movimientos con plan de cuentas para tener `code`, `grupo`, etc."""
    if moves is None or moves.empty:
        return pd.DataFrame()
    if chart is None or chart.empty:
        return moves
    chart_e = enrich_chart_with_puc(chart)
    # Agregar columna `code_normalized` para clasificación robusta
    if "code" in chart_e.columns:
        chart_e["code_normalized"] = chart_e["code"].apply(_normalize_code)
    # Solo seleccionar columnas que realmente existen (el chart puede haber
    # caído a un nivel mínimo en el fallback sin account_type)
    desired_cols = [
        "id", "code", "code_normalized", "name", "account_type",
        "grupo", "es_corriente", "es_resultado", "subgrupo",
    ]
    available_cols = [c for c in desired_cols if c in chart_e.columns]
    keep = chart_e[available_cols].rename(columns={
        "id": "account_id",
        "code": "account_code",
        "code_normalized": "account_code_norm",
        "name": "account_name",
    })
    return moves.merge(keep, on="account_id", how="left")


# =============================================================================
# 1. Estado de Resultados (P&L)
# =============================================================================

def compute_income_statement(
    moves: pd.DataFrame,
    chart: pd.DataFrame,
    date_from: date,
    date_to: date,
) -> dict:
    """
    Estado de Resultados (P&L) para el período.
    Devuelve dict con:
      - ingresos_operacionales, ingresos_no_operacionales
      - costo_ventas, gastos_admin, gastos_ventas, gastos_no_operacionales
      - utilidad_bruta, utilidad_operacional, utilidad_antes_impuestos,
        impuesto_renta, utilidad_neta
      - margen_bruto_pct, margen_operacional_pct, margen_neto_pct
      - tabla_detalle: DataFrame con cuentas y montos
    """
    sub = _filter_moves(moves, date_from, date_to)
    sub = _join_moves_chart(sub, chart)
    if sub.empty:
        return {
            "ingresos_operacionales": 0, "ingresos_no_operacionales": 0,
            "costo_ventas": 0, "gastos_admin": 0, "gastos_ventas": 0,
            "gastos_no_operacionales": 0, "impuesto_renta": 0,
            "utilidad_bruta": 0, "utilidad_operacional": 0,
            "utilidad_antes_impuestos": 0, "utilidad_neta": 0,
            "margen_bruto_pct": 0, "margen_operacional_pct": 0,
            "margen_neto_pct": 0,
            "tabla_detalle": pd.DataFrame(),
        }

    # Usar código normalizado (sin prefijos F, ceros iniciales, etc.)
    code_col = "account_code_norm" if "account_code_norm" in sub.columns else "account_code"
    codes = sub[code_col].astype(str)

    # Para cuentas de resultado:
    #   Ingresos (4xxx): saldo NORMAL es crédito → balance NEGATIVO
    #     credit - debit = ingreso positivo
    #   Gastos/Costos (5,6,7xxx): saldo normal es débito
    #     debit - credit = gasto positivo
    sub["monto_ingreso"] = (sub["credit"] - sub["debit"]).where(
        codes.str.startswith("4"), 0
    )
    sub["monto_gasto"] = (sub["debit"] - sub["credit"]).where(
        codes.str.startswith(("5", "6", "7")), 0
    )

    ingresos_op = float(sub.loc[codes.str.startswith("41"), "monto_ingreso"].sum())
    ingresos_no_op = float(sub.loc[codes.str.startswith("42"), "monto_ingreso"].sum())
    costo_ventas = float(sub.loc[codes.str.startswith("6"), "monto_gasto"].sum())
    gastos_admin = float(sub.loc[codes.str.startswith("51"), "monto_gasto"].sum())
    gastos_ventas = float(sub.loc[codes.str.startswith("52"), "monto_gasto"].sum())
    gastos_no_op = float(sub.loc[codes.str.startswith("53"), "monto_gasto"].sum())
    impto_renta = float(sub.loc[codes.str.startswith("54"), "monto_gasto"].sum())

    utilidad_bruta = ingresos_op - costo_ventas
    utilidad_operacional = utilidad_bruta - gastos_admin - gastos_ventas
    utilidad_antes_impuestos = utilidad_operacional + ingresos_no_op - gastos_no_op
    utilidad_neta = utilidad_antes_impuestos - impto_renta

    def _pct(n, d):
        return (n / d * 100) if d else 0

    # Tabla de detalle por cuenta
    detalle = sub[sub["es_resultado"] == True].copy()
    if not detalle.empty:
        detalle["monto"] = detalle["monto_ingreso"] + detalle["monto_gasto"]
        tabla = detalle.groupby(
            ["account_code", "account_name", "subgrupo"], as_index=False
        )["monto"].sum()
        tabla = tabla[tabla["monto"] != 0].sort_values(
            ["subgrupo", "monto"], ascending=[True, False]
        ).reset_index(drop=True)
    else:
        tabla = pd.DataFrame()

    return {
        "ingresos_operacionales": ingresos_op,
        "ingresos_no_operacionales": ingresos_no_op,
        "costo_ventas": costo_ventas,
        "gastos_admin": gastos_admin,
        "gastos_ventas": gastos_ventas,
        "gastos_no_operacionales": gastos_no_op,
        "impuesto_renta": impto_renta,
        "utilidad_bruta": utilidad_bruta,
        "utilidad_operacional": utilidad_operacional,
        "utilidad_antes_impuestos": utilidad_antes_impuestos,
        "utilidad_neta": utilidad_neta,
        "margen_bruto_pct": _pct(utilidad_bruta, ingresos_op),
        "margen_operacional_pct": _pct(utilidad_operacional, ingresos_op),
        "margen_neto_pct": _pct(utilidad_neta, ingresos_op),
        "tabla_detalle": tabla,
    }


# =============================================================================
# 2. Balance General (snapshot a una fecha)
# =============================================================================

def compute_balance_sheet(
    moves: pd.DataFrame,
    chart: pd.DataFrame,
    date_to: date,
) -> dict:
    """
    Balance General a la fecha de corte. Suma todos los movimientos hasta
    `date_to` y calcula saldos de cuentas balance (1, 2, 3).
    """
    sub = _filter_moves(moves, None, date_to)
    sub = _join_moves_chart(sub, chart)
    if sub.empty:
        return {
            "activo_corriente": 0, "activo_no_corriente": 0, "activo_total": 0,
            "pasivo_corriente": 0, "pasivo_no_corriente": 0, "pasivo_total": 0,
            "patrimonio": 0, "pasivo_patrimonio_total": 0,
            "tabla_activo": pd.DataFrame(),
            "tabla_pasivo": pd.DataFrame(),
            "tabla_patrimonio": pd.DataFrame(),
        }

    # Saldo por cuenta. Para Activos: debit - credit (saldo deudor)
    # Para Pasivos y Patrimonio: credit - debit (saldo acreedor)
    sub["saldo_deudor"] = sub["debit"] - sub["credit"]
    sub["saldo_acreedor"] = sub["credit"] - sub["debit"]

    # Filtrar solo cuentas de balance (1, 2, 3) — no incluimos resultados
    # porque el efecto del P&L del período se refleja en la utilidad de
    # ejercicios anteriores cuando se cierra el año. Para no doble-contar,
    # incluimos las cuentas 4-7 también pero como parte del patrimonio
    # del ejercicio en curso.
    is_asset = sub.get("account_code_norm", sub.get("account_code", pd.Series([], dtype=str))).astype(str).str.startswith("1")
    is_liab = sub.get("account_code_norm", sub.get("account_code", pd.Series([], dtype=str))).astype(str).str.startswith("2")
    is_equity = sub.get("account_code_norm", sub.get("account_code", pd.Series([], dtype=str))).astype(str).str.startswith("3")
    is_result = sub.get("account_code_norm", sub.get("account_code", pd.Series([], dtype=str))).astype(str).str.startswith(("4", "5", "6", "7"))

    # Por cuenta
    by_account = sub.groupby(
        ["account_code", "account_name", "grupo", "subgrupo", "es_corriente"],
        as_index=False,
    ).agg(saldo_deudor=("saldo_deudor", "sum"), saldo_acreedor=("saldo_acreedor", "sum"))

    # Activo: saldo deudor positivo
    activos = by_account[by_account.get("account_code_norm", by_account["account_code"]).astype(str).str.startswith("1")].copy()
    activos["saldo"] = activos["saldo_deudor"]
    activos = activos[activos["saldo"] != 0].sort_values(
        ["es_corriente", "subgrupo", "account_code"], ascending=[False, True, True],
    )
    activo_corriente = float(activos.loc[activos["es_corriente"], "saldo"].sum())
    activo_no_corriente = float(activos.loc[~activos["es_corriente"], "saldo"].sum())
    activo_total = activo_corriente + activo_no_corriente

    # Pasivo: saldo acreedor positivo
    pasivos = by_account[by_account.get("account_code_norm", by_account["account_code"]).astype(str).str.startswith("2")].copy()
    pasivos["saldo"] = pasivos["saldo_acreedor"]
    pasivos = pasivos[pasivos["saldo"] != 0].sort_values(
        ["es_corriente", "subgrupo", "account_code"], ascending=[False, True, True],
    )
    pasivo_corriente = float(pasivos.loc[pasivos["es_corriente"], "saldo"].sum())
    pasivo_no_corriente = float(pasivos.loc[~pasivos["es_corriente"], "saldo"].sum())
    pasivo_total = pasivo_corriente + pasivo_no_corriente

    # Patrimonio: saldo acreedor de cuentas 3xxx + utilidad del período
    patrimonio_cuentas = by_account[by_account.get("account_code_norm", by_account["account_code"]).astype(str).str.startswith("3")].copy()
    patrimonio_cuentas["saldo"] = patrimonio_cuentas["saldo_acreedor"]
    patrimonio_cuentas = patrimonio_cuentas[patrimonio_cuentas["saldo"] != 0].sort_values(
        "account_code"
    )
    patrimonio_3 = float(patrimonio_cuentas["saldo"].sum())

    # Utilidad del período actual = activo - pasivo - patrimonio_3
    # (debe cuadrar la ecuación contable Activo = Pasivo + Patrimonio)
    utilidad_periodo = activo_total - pasivo_total - patrimonio_3
    patrimonio_total = patrimonio_3 + utilidad_periodo

    # Agregar utilidad al patrimonio para mostrar
    if utilidad_periodo != 0:
        patrimonio_cuentas = pd.concat([
            patrimonio_cuentas,
            pd.DataFrame([{
                "account_code": "36/37",
                "account_name": "Utilidad del ejercicio en curso",
                "grupo": "Patrimonio", "subgrupo": "Resultado del período",
                "es_corriente": False,
                "saldo_deudor": 0, "saldo_acreedor": utilidad_periodo,
                "saldo": utilidad_periodo,
            }]),
        ], ignore_index=True)

    return {
        "activo_corriente": activo_corriente,
        "activo_no_corriente": activo_no_corriente,
        "activo_total": activo_total,
        "pasivo_corriente": pasivo_corriente,
        "pasivo_no_corriente": pasivo_no_corriente,
        "pasivo_total": pasivo_total,
        "patrimonio": patrimonio_total,
        "pasivo_patrimonio_total": pasivo_total + patrimonio_total,
        "tabla_activo": activos,
        "tabla_pasivo": pasivos,
        "tabla_patrimonio": patrimonio_cuentas,
    }


# =============================================================================
# 3. KTNO + Capital de Trabajo
# =============================================================================

def compute_working_capital(
    moves: pd.DataFrame,
    chart: pd.DataFrame,
    date_to: date,
) -> dict:
    """
    Capital de Trabajo y KTNO.

    Capital de trabajo (KT) = Activo Corriente - Pasivo Corriente
    KTNO (Capital de Trabajo Neto Operativo)
        = Cuentas por cobrar (13xx) + Inventarios (14xx) - Proveedores (22xx)
    """
    bs = compute_balance_sheet(moves, chart, date_to)
    sub = _filter_moves(moves, None, date_to)
    sub = _join_moves_chart(sub, chart)
    if sub.empty:
        return {
            "kt": 0, "ktno": 0,
            "cxc": 0, "inventario": 0, "proveedores": 0,
            "disponible": 0, "obligaciones_financieras_cp": 0,
            "razon_corriente": 0, "prueba_acida": 0,
            "activo_corriente": 0, "pasivo_corriente": 0,
        }

    sub["saldo_deudor"] = sub["debit"] - sub["credit"]
    sub["saldo_acreedor"] = sub["credit"] - sub["debit"]
    code = sub.get("account_code_norm", sub.get("account_code", pd.Series([], dtype=str))).astype(str)

    # KTNO components
    cxc = float(sub.loc[code.str.startswith("13"), "saldo_deudor"].sum())
    inventario = float(sub.loc[code.str.startswith("14"), "saldo_deudor"].sum())
    proveedores = float(sub.loc[code.str.startswith("22"), "saldo_acreedor"].sum())
    ktno = cxc + inventario - proveedores

    # Otros componentes
    disponible = float(sub.loc[code.str.startswith("11"), "saldo_deudor"].sum())
    obl_fin = float(sub.loc[code.str.startswith("21"), "saldo_acreedor"].sum())

    activo_corriente = bs["activo_corriente"]
    pasivo_corriente = bs["pasivo_corriente"]
    kt = activo_corriente - pasivo_corriente

    razon_corriente = activo_corriente / pasivo_corriente if pasivo_corriente else 0
    prueba_acida = (activo_corriente - inventario) / pasivo_corriente if pasivo_corriente else 0

    return {
        "kt": kt,
        "ktno": ktno,
        "cxc": cxc,
        "inventario": inventario,
        "proveedores": proveedores,
        "disponible": disponible,
        "obligaciones_financieras_cp": obl_fin,
        "razon_corriente": razon_corriente,
        "prueba_acida": prueba_acida,
        "activo_corriente": activo_corriente,
        "pasivo_corriente": pasivo_corriente,
    }


# =============================================================================
# 4. Flujo de Efectivo (simplificado, basado en movimientos de caja/banco)
# =============================================================================

def compute_cash_flow(
    moves: pd.DataFrame,
    chart: pd.DataFrame,
    date_from: date,
    date_to: date,
) -> dict:
    """
    Flujo de efectivo simplificado: muestra entradas/salidas de las cuentas
    de disponible (11xx) en el período.
    """
    sub = _filter_moves(moves, date_from, date_to)
    sub = _join_moves_chart(sub, chart)
    if sub.empty:
        return {
            "entradas": 0, "salidas": 0, "neto": 0,
            "saldo_inicial": 0, "saldo_final": 0,
            "tabla_diaria": pd.DataFrame(),
            "tabla_por_contraparte": pd.DataFrame(),
        }

    code = sub.get("account_code_norm", sub.get("account_code", pd.Series([], dtype=str))).astype(str)
    cash_lines = sub[code.str.startswith("11")].copy()
    if cash_lines.empty:
        return {
            "entradas": 0, "salidas": 0, "neto": 0,
            "saldo_inicial": 0, "saldo_final": 0,
            "tabla_diaria": pd.DataFrame(),
            "tabla_por_contraparte": pd.DataFrame(),
        }

    entradas = float(cash_lines["debit"].sum())
    salidas = float(cash_lines["credit"].sum())
    neto = entradas - salidas

    # Saldo inicial = todos los movimientos antes de date_from
    historico = _filter_moves(moves, None, pd.Timestamp(date_from) - pd.Timedelta(days=1))
    historico = _join_moves_chart(historico, chart)
    if not historico.empty:
        hist_cash = historico[historico["account_code"].astype(str).str.startswith("11")]
        saldo_inicial = float((hist_cash["debit"] - hist_cash["credit"]).sum())
    else:
        saldo_inicial = 0

    saldo_final = saldo_inicial + neto

    # Movimientos diarios
    cash_lines["fecha_dia"] = pd.to_datetime(cash_lines["date"]).dt.date
    daily = cash_lines.groupby("fecha_dia", as_index=False).agg(
        entradas=("debit", "sum"), salidas=("credit", "sum")
    )
    daily["neto"] = daily["entradas"] - daily["salidas"]
    daily = daily.sort_values("fecha_dia")

    # Por contraparte (top movimientos)
    cash_lines["partner_name_str"] = cash_lines["partner_id_name"].fillna("Sin contraparte")
    by_partner = cash_lines.groupby("partner_name_str", as_index=False).agg(
        entradas=("debit", "sum"), salidas=("credit", "sum")
    )
    by_partner["neto"] = by_partner["entradas"] - by_partner["salidas"]
    by_partner = by_partner[(by_partner["entradas"] != 0) | (by_partner["salidas"] != 0)]
    by_partner = by_partner.sort_values("entradas", ascending=False).head(50)

    return {
        "entradas": entradas, "salidas": salidas, "neto": neto,
        "saldo_inicial": saldo_inicial, "saldo_final": saldo_final,
        "tabla_diaria": daily,
        "tabla_por_contraparte": by_partner,
    }


# =============================================================================
# 5. Análisis de Gastos
# =============================================================================

def compute_expenses_breakdown(
    moves: pd.DataFrame,
    chart: pd.DataFrame,
    date_from: date,
    date_to: date,
) -> dict:
    """Desglose de gastos por subgrupo, cuenta, y mes."""
    sub = _filter_moves(moves, date_from, date_to)
    sub = _join_moves_chart(sub, chart)
    if sub.empty:
        return {
            "total_gastos": 0,
            "por_subgrupo": pd.DataFrame(),
            "por_cuenta": pd.DataFrame(),
            "por_mes": pd.DataFrame(),
        }

    code = sub.get("account_code_norm", sub.get("account_code", pd.Series([], dtype=str))).astype(str)
    gastos = sub[code.str.startswith(("5", "6"))].copy()
    if gastos.empty:
        return {
            "total_gastos": 0,
            "por_subgrupo": pd.DataFrame(),
            "por_cuenta": pd.DataFrame(),
            "por_mes": pd.DataFrame(),
        }

    gastos["monto"] = gastos["debit"] - gastos["credit"]
    total = float(gastos["monto"].sum())

    por_subgrupo = gastos.groupby("subgrupo", as_index=False)["monto"].sum()
    por_subgrupo["pct"] = por_subgrupo["monto"] / total * 100 if total else 0
    por_subgrupo = por_subgrupo.sort_values("monto", ascending=False).reset_index(drop=True)

    por_cuenta = gastos.groupby(
        ["account_code", "account_name", "subgrupo"], as_index=False
    )["monto"].sum()
    por_cuenta["pct"] = por_cuenta["monto"] / total * 100 if total else 0
    por_cuenta = por_cuenta[por_cuenta["monto"] != 0].sort_values(
        "monto", ascending=False
    ).reset_index(drop=True)

    gastos["mes"] = pd.to_datetime(gastos["date"]).dt.to_period("M").dt.to_timestamp()
    por_mes = gastos.groupby(["mes", "subgrupo"], as_index=False)["monto"].sum()

    return {
        "total_gastos": total,
        "por_subgrupo": por_subgrupo,
        "por_cuenta": por_cuenta,
        "por_mes": por_mes,
    }


# =============================================================================
# 6. Comparativos: P&L mes vs mes
# =============================================================================

def compute_pnl_monthly_evolution(
    moves: pd.DataFrame,
    chart: pd.DataFrame,
    date_from: date,
    date_to: date,
) -> pd.DataFrame:
    """
    P&L por mes: una fila por mes, columnas con ingresos, costos, gastos,
    utilidades y márgenes. Para gráficas de tendencia.
    """
    sub = _filter_moves(moves, date_from, date_to)
    sub = _join_moves_chart(sub, chart)
    if sub.empty:
        return pd.DataFrame()

    sub["mes"] = pd.to_datetime(sub["date"]).dt.to_period("M").dt.to_timestamp()
    code = sub.get("account_code_norm", sub.get("account_code", pd.Series([], dtype=str))).astype(str)

    sub["ingreso_op"] = (sub["credit"] - sub["debit"]).where(code.str.startswith("41"), 0)
    sub["ingreso_no_op"] = (sub["credit"] - sub["debit"]).where(code.str.startswith("42"), 0)
    sub["costo"] = (sub["debit"] - sub["credit"]).where(code.str.startswith("6"), 0)
    sub["gasto_admin"] = (sub["debit"] - sub["credit"]).where(code.str.startswith("51"), 0)
    sub["gasto_ventas"] = (sub["debit"] - sub["credit"]).where(code.str.startswith("52"), 0)
    sub["gasto_no_op"] = (sub["debit"] - sub["credit"]).where(code.str.startswith("53"), 0)
    sub["impto"] = (sub["debit"] - sub["credit"]).where(code.str.startswith("54"), 0)

    monthly = sub.groupby("mes", as_index=False).agg(
        ingreso_op=("ingreso_op", "sum"),
        ingreso_no_op=("ingreso_no_op", "sum"),
        costo=("costo", "sum"),
        gasto_admin=("gasto_admin", "sum"),
        gasto_ventas=("gasto_ventas", "sum"),
        gasto_no_op=("gasto_no_op", "sum"),
        impto=("impto", "sum"),
    )
    monthly["utilidad_bruta"] = monthly["ingreso_op"] - monthly["costo"]
    monthly["utilidad_op"] = (
        monthly["utilidad_bruta"] - monthly["gasto_admin"] - monthly["gasto_ventas"]
    )
    monthly["utilidad_neta"] = (
        monthly["utilidad_op"] + monthly["ingreso_no_op"]
        - monthly["gasto_no_op"] - monthly["impto"]
    )
    monthly["margen_bruto_pct"] = (
        monthly["utilidad_bruta"] / monthly["ingreso_op"].replace(0, np.nan) * 100
    ).fillna(0)
    monthly["margen_op_pct"] = (
        monthly["utilidad_op"] / monthly["ingreso_op"].replace(0, np.nan) * 100
    ).fillna(0)
    monthly["margen_neto_pct"] = (
        monthly["utilidad_neta"] / monthly["ingreso_op"].replace(0, np.nan) * 100
    ).fillna(0)
    return monthly.sort_values("mes").reset_index(drop=True)
