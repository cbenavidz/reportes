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


# Mapeo de account_type (campo nativo de Odoo) → grupo PUC.
# Esta es la fuente AUTORITATIVA porque Odoo asigna el tipo correctamente
# en cada cuenta del plan, independiente de cómo numere los códigos.
ACCOUNT_TYPE_TO_PUC = {
    # ACTIVOS (1)
    "asset_receivable":   {"puc": "1", "subpuc": "13", "subgrupo": "Deudores (CxC)",         "corr": True},
    "asset_cash":         {"puc": "1", "subpuc": "11", "subgrupo": "Disponible (caja, bancos)", "corr": True},
    "asset_current":      {"puc": "1", "subpuc": "1",  "subgrupo": "Activo corriente",       "corr": True},
    "asset_prepayments":  {"puc": "1", "subpuc": "17", "subgrupo": "Diferidos",              "corr": True},
    "asset_non_current":  {"puc": "1", "subpuc": "1",  "subgrupo": "Activo no corriente",    "corr": False},
    "asset_fixed":        {"puc": "1", "subpuc": "15", "subgrupo": "Propiedad, planta y equipo", "corr": False},
    # PASIVOS (2)
    "liability_payable":      {"puc": "2", "subpuc": "22", "subgrupo": "Proveedores",          "corr": True},
    "liability_credit_card":  {"puc": "2", "subpuc": "23", "subgrupo": "Tarjetas de crédito",  "corr": True},
    "liability_current":      {"puc": "2", "subpuc": "2",  "subgrupo": "Pasivo corriente",    "corr": True},
    "liability_non_current":  {"puc": "2", "subpuc": "2",  "subgrupo": "Pasivo no corriente", "corr": False},
    # PATRIMONIO (3)
    "equity":            {"puc": "3", "subpuc": "3",  "subgrupo": "Patrimonio",            "corr": False},
    "equity_unaffected": {"puc": "3", "subpuc": "37", "subgrupo": "Resultados anteriores", "corr": False},
    # INGRESOS (4) — income operacional, income_other no operacional
    "income":       {"puc": "4", "subpuc": "41", "subgrupo": "Ingresos operacionales",     "corr": False},
    "income_other": {"puc": "4", "subpuc": "42", "subgrupo": "Ingresos no operacionales",  "corr": False},
    # GASTOS Y COSTOS (5, 6)
    "expense":             {"puc": "5", "subpuc": "51", "subgrupo": "Gastos operacionales",   "corr": False},
    "expense_depreciation":{"puc": "5", "subpuc": "51", "subgrupo": "Depreciaciones",         "corr": False},
    "expense_direct_cost": {"puc": "6", "subpuc": "6",  "subgrupo": "Costo de ventas",        "corr": False},
    # FUERA DE BALANCE
    "off_balance":  {"puc": "8", "subpuc": "8",  "subgrupo": "Cuentas de orden",  "corr": False},
}


def classify_by_name(name: str) -> dict | None:
    """
    Clasifica por palabras clave en el nombre (fallback de último recurso
    cuando no hay account_type ni código PUC parseable). Usa keywords del
    PUC colombiano.
    """
    if not name:
        return None
    n = name.lower().strip()

    # INGRESOS (4)
    if any(kw in n for kw in [
        "venta de", "ventas gravad", "ventas excluida", "ingresos por",
        "comercio al por", "devolución en venta", "descuento en venta",
    ]):
        if "no operacional" in n or "financier" in n or "rendimi" in n:
            return _puc_dict("4", "42", "Ingresos no operacionales", False)
        return _puc_dict("4", "41", "Ingresos operacionales", False)
    if "ingreso" in n and "no operac" in n:
        return _puc_dict("4", "42", "Ingresos no operacionales", False)
    if "ingreso" in n:
        return _puc_dict("4", "41", "Ingresos operacionales", False)

    # COSTOS (6)
    if any(kw in n for kw in [
        "costo de venta", "costo de mercanc", "costo de comercializ",
    ]):
        return _puc_dict("6", "6", "Costo de ventas", False)

    # GASTOS (5)
    if any(kw in n for kw in [
        "gastos de personal", "gastos de admin", "gastos generales",
        "gasto de", "honorarios", "arrendamiento", "servicios públicos",
    ]):
        if "venta" in n or "distribuci" in n or "comercial" in n:
            return _puc_dict("5", "52", "Gastos de ventas", False)
        return _puc_dict("5", "51", "Gastos administrativos", False)
    if "gastos financier" in n or "intereses" in n:
        return _puc_dict("5", "53", "Gastos no operacionales", False)
    if "impuesto de renta" in n or "impuesto renta" in n:
        return _puc_dict("5", "54", "Impuesto de renta", False)

    # ACTIVOS (1)
    if any(kw in n for kw in [
        "banco ", "bco ", "caja ", "cajas ", "efectivo", "fiduciar",
        "addi", "sistecredit",
    ]):
        return _puc_dict("1", "11", "Disponible (caja, bancos)", True)
    if any(kw in n for kw in [
        "clientes", "deudores", "cuentas por cobrar",
        "anticipos a", "préstamo a empleado",
    ]):
        return _puc_dict("1", "13", "Deudores (CxC)", True)
    if any(kw in n for kw in [
        "inventario", "mercanc", "existencias", "bodega", "almacén",
        "almacen", "stock", "repuestos en", "producto terminado",
        "materia prima", "mercader", "lubricantes en", "en tránsito",
        "en transito",
    ]):
        return _puc_dict("1", "14", "Inventarios", True)
    if "iva descontable" in n or "iva por cobrar" in n or "anticipo de imp" in n:
        return _puc_dict("1", "13", "Deudores (impuestos)", True)
    if any(kw in n for kw in [
        "edifici", "construc", "maquinaria", "equipo de", "muebles",
        "vehícul", "terreno", "depreciaci",
    ]):
        return _puc_dict("1", "15", "Propiedad, planta y equipo", False)

    # PASIVOS (2)
    if "proveed" in n or "a proveedor" in n:
        return _puc_dict("2", "22", "Proveedores", True)
    if any(kw in n for kw in [
        "obligacion financ", "obligaciones bancar", "préstamo bancar",
    ]):
        return _puc_dict("2", "21", "Obligaciones financieras", True)
    if "iva generado" in n or "iva por pagar" in n or "retención en la fuente" in n:
        return _puc_dict("2", "24", "Impuestos por pagar", True)
    if "prestaciones" in n or "salario por pagar" in n or "cesantía" in n:
        return _puc_dict("2", "25", "Obligaciones laborales", True)
    if "cuentas por pagar" in n:
        return _puc_dict("2", "23", "Cuentas por pagar", True)

    # PATRIMONIO (3)
    if any(kw in n for kw in [
        "capital social", "capital suscrito", "reserva", "utilidad del ejercicio",
        "patrimonio",
    ]):
        return _puc_dict("3", "3", "Patrimonio", False)

    return None


def _puc_dict(grupo: str, subgrupo: str, nombre_sub: str, corriente: bool) -> dict:
    """Helper para crear dict de clasificación."""
    return {
        "grupo": PUC_GROUPS.get(grupo, "Otro"),
        "es_corriente": corriente,
        "es_resultado": grupo in ("4", "5", "6", "7"),
        "subgrupo": nombre_sub,
        "puc_group": grupo,
        "puc_subgroup": subgrupo,
    }


def classify_by_account_type(account_type: str) -> dict | None:
    """Clasifica por account_type (campo nativo de Odoo). None si no mapea."""
    if not account_type:
        return None
    mapping = ACCOUNT_TYPE_TO_PUC.get(account_type)
    if not mapping:
        return None
    grupo_name = PUC_GROUPS.get(mapping["puc"], "Otro")
    return {
        "grupo": grupo_name,
        "es_corriente": mapping["corr"],
        "es_resultado": mapping["puc"] in ("4", "5", "6", "7"),
        "subgrupo": mapping["subgrupo"],
        "puc_group": mapping["puc"],
        "puc_subgroup": mapping["subpuc"],
    }


def enrich_chart_with_puc(chart: pd.DataFrame) -> pd.DataFrame:
    """
    Agrega columnas 'grupo', 'es_corriente', 'es_resultado', 'subgrupo',
    'puc_group', 'puc_subgroup' al plan de cuentas.

    Fuente PRIMARIA: `account_type` de Odoo (clasificación nativa).
    Fallback: clasificación por código (primer dígito numérico).
    """
    if chart is None or chart.empty:
        return chart
    out = chart.copy()

    def _classify_row(row):
        code = str(row.get("code", "") or "").strip()
        digits = "".join(ch for ch in code if ch.isdigit()).lstrip("0")
        account_type = row.get("account_type", "")

        # 1. PRIMARIO: código PUC si es válido (2+ dígitos significativos
        #    empezando por 1-9). Esto es lo MÁS preciso porque distingue
        #    subgrupos: 41 vs 42 (ingresos op vs no op), 51/52/53 (gastos
        #    admin/ventas/no op), etc. account_type NO puede distinguir eso
        #    porque para Odoo "expense" es expense, sin importar si es 51/52/53.
        if len(digits) >= 2 and digits[0] in "123456789":
            code_cls = classify_account(code)
            code_cls["puc_group"] = digits[0]
            code_cls["puc_subgroup"] = digits[:2]
            return code_cls

        # 2. SECUNDARIO: account_type de Odoo (para cuentas auxiliares
        #    de balance con códigos no-PUC como "00000001", "F4135").
        if account_type:
            result = classify_by_account_type(account_type)
            if result:
                # IMPORTANTE: Odoo NO tiene account_type específico para
                # inventarios — las cuentas de inventario son `asset_current`
                # genérico. Cuando el subgrupo es genérico ("1" activo
                # corriente, "2" pasivo corriente), refinamos con el NOMBRE
                # para distinguir inventarios (14), CxC (13), etc.
                if result["puc_subgroup"] in ("1", "2"):
                    name_cls = classify_by_name(row.get("name", ""))
                    if name_cls and name_cls["puc_group"] == result["puc_group"]:
                        # El nombre da un subgrupo más fino dentro del
                        # mismo grupo → lo usamos.
                        return name_cls
                return result

        # 3. TERCIARIO: clasificar por nombre (palabras clave PUC)
        name_cls = classify_by_name(row.get("name", ""))
        if name_cls:
            return name_cls

        # 4. Último recurso: Otro
        return {
            "grupo": "Otro", "es_corriente": False,
            "es_resultado": False, "subgrupo": "Otro",
            "puc_group": "", "puc_subgroup": "",
        }

    cls = out.apply(_classify_row, axis=1)
    out["grupo"] = cls.apply(lambda d: d["grupo"])
    out["es_corriente"] = cls.apply(lambda d: d["es_corriente"])
    out["es_resultado"] = cls.apply(lambda d: d["es_resultado"])
    out["subgrupo"] = cls.apply(lambda d: d["subgrupo"])
    out["puc_group"] = cls.apply(lambda d: d.get("puc_group", ""))
    out["puc_subgroup"] = cls.apply(lambda d: d.get("puc_subgroup", ""))
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
    # Agregar columna `code_normalized` para clasificación robusta (fallback)
    if "code" in chart_e.columns:
        chart_e["code_normalized"] = chart_e["code"].apply(_normalize_code)
    desired_cols = [
        "id", "code", "code_normalized", "name", "account_type",
        "grupo", "es_corriente", "es_resultado", "subgrupo",
        "puc_group", "puc_subgroup",  # de account_type
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
    balances_aggregated: pd.DataFrame | None = None,
) -> dict:
    """
    Estado de Resultados (P&L) para el período.

    OPTIMIZACIÓN: si se pasa `balances_aggregated` (saldos por cuenta del
    período, vía read_group server-side), se usa eso en lugar de sumar
    líneas individuales. ~100x más rápido.
    """
    # Si tenemos balances agregados, los usamos
    if balances_aggregated is not None and not balances_aggregated.empty:
        sub = balances_aggregated.copy()
        chart_e = enrich_chart_with_puc(chart)
        if "code" in chart_e.columns:
            chart_e["code_normalized"] = chart_e["code"].apply(_normalize_code)
        keep_cols = [c for c in [
            "id", "code", "code_normalized", "name", "account_type",
            "grupo", "es_corriente", "es_resultado", "subgrupo",
            "puc_group", "puc_subgroup",
        ] if c in chart_e.columns]
        keep = chart_e[keep_cols].rename(columns={
            "id": "account_id", "code": "account_code",
            "code_normalized": "account_code_norm", "name": "account_name",
        })
        sub = sub.merge(keep, on="account_id", how="left")
    else:
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

    # Clasificación PRIMARIA: puc_group y puc_subgroup (derivados de
    # account_type de Odoo). Fallback a account_code_norm si no hay.
    if "puc_group" in sub.columns:
        puc_g = sub["puc_group"].astype(str)
        puc_sg = sub["puc_subgroup"].astype(str) if "puc_subgroup" in sub.columns else puc_g
    else:
        # Fallback: usar código normalizado
        code_col = "account_code_norm" if "account_code_norm" in sub.columns else "account_code"
        codes = sub[code_col].astype(str)
        puc_g = codes.str[:1]
        puc_sg = codes.str[:2]

    # Para cuentas de resultado:
    #   Ingresos (4): saldo normal es crédito → credit - debit = ingreso positivo
    #   Gastos/Costos (5,6,7): saldo normal es débito → debit - credit
    sub["monto_ingreso"] = (sub["credit"] - sub["debit"]).where(
        puc_g == "4", 0
    )
    sub["monto_gasto"] = (sub["debit"] - sub["credit"]).where(
        puc_g.isin(["5", "6", "7"]), 0
    )

    ingresos_op = float(sub.loc[puc_sg == "41", "monto_ingreso"].sum())
    ingresos_no_op = float(sub.loc[puc_sg == "42", "monto_ingreso"].sum())
    costo_ventas = float(sub.loc[puc_g == "6", "monto_gasto"].sum())
    gastos_admin = float(sub.loc[puc_sg == "51", "monto_gasto"].sum())
    gastos_ventas = float(sub.loc[puc_sg == "52", "monto_gasto"].sum())
    gastos_no_op = float(sub.loc[puc_sg == "53", "monto_gasto"].sum())
    impto_renta = float(sub.loc[puc_sg == "54", "monto_gasto"].sum())

    # Si no hay distinción fina entre 51/52/53 (account_type "expense" genérico),
    # todos los gastos van a gastos_admin como fallback
    if (gastos_admin == 0 and gastos_ventas == 0 and gastos_no_op == 0):
        gastos_admin = float(sub.loc[puc_g == "5", "monto_gasto"].sum())

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
    balances_hist: pd.DataFrame | None = None,
) -> dict:
    """
    Balance General a la fecha de corte. Si se pasa `balances_hist`
    (DataFrame con saldos agregados pre-calculados con read_group), lo usa
    en lugar de sumar moves línea-por-línea (mucho más rápido).
    """
    # Si tenemos balances agregados, los usamos directamente
    if balances_hist is not None and not balances_hist.empty:
        sub = balances_hist.copy()
        # Hacer join con chart para tener code/name
        chart_e = enrich_chart_with_puc(chart)
        if "code" in chart_e.columns:
            chart_e["code_normalized"] = chart_e["code"].apply(_normalize_code)
        keep_cols = [c for c in [
            "id", "code", "code_normalized", "name", "grupo",
            "es_corriente", "es_resultado", "subgrupo",
            "puc_group", "puc_subgroup", "account_type",
        ] if c in chart_e.columns]
        keep = chart_e[keep_cols].rename(columns={
            "id": "account_id", "code": "account_code",
            "code_normalized": "account_code_norm", "name": "account_name",
        })
        sub = sub.merge(keep, on="account_id", how="left")
    else:
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

    # Clasificación PRIMARIA por puc_group (de account_type Odoo)
    if "puc_group" in sub.columns:
        puc_g = sub["puc_group"].astype(str)
    else:
        puc_g = sub.get(
            "account_code_norm",
            sub.get("account_code", pd.Series([""] * len(sub)))
        ).astype(str).str[:1]
    is_asset = puc_g == "1"
    is_liab = puc_g == "2"
    is_equity = puc_g == "3"
    is_result = puc_g.isin(["4", "5", "6", "7"])

    # Por cuenta — agregar puc_group para clasificar
    groupby_cols = ["account_code", "account_name", "grupo", "subgrupo", "es_corriente"]
    if "puc_group" in sub.columns:
        groupby_cols.append("puc_group")
    by_account = sub.groupby(
        groupby_cols, as_index=False,
    ).agg(saldo_deudor=("saldo_deudor", "sum"), saldo_acreedor=("saldo_acreedor", "sum"))

    # Función helper para filtrar por puc_group con fallback a código
    def _filter_puc(df: pd.DataFrame, group: str) -> pd.DataFrame:
        if "puc_group" in df.columns:
            return df[df["puc_group"].astype(str) == group].copy()
        return df[
            df.get(
                "account_code_norm",
                df.get("account_code", pd.Series([""] * len(df)))
            ).astype(str).str.startswith(group)
        ].copy()

    # Activo: saldo deudor positivo
    activos = _filter_puc(by_account, "1")
    activos["saldo"] = activos["saldo_deudor"]
    activos = activos[activos["saldo"] != 0].sort_values(
        ["es_corriente", "subgrupo", "account_code"], ascending=[False, True, True],
    )
    activo_corriente = float(activos.loc[activos["es_corriente"], "saldo"].sum())
    activo_no_corriente = float(activos.loc[~activos["es_corriente"], "saldo"].sum())
    activo_total = activo_corriente + activo_no_corriente

    # Pasivo: saldo acreedor positivo
    pasivos = _filter_puc(by_account, "2")
    pasivos["saldo"] = pasivos["saldo_acreedor"]
    pasivos = pasivos[pasivos["saldo"] != 0].sort_values(
        ["es_corriente", "subgrupo", "account_code"], ascending=[False, True, True],
    )
    pasivo_corriente = float(pasivos.loc[pasivos["es_corriente"], "saldo"].sum())
    pasivo_no_corriente = float(pasivos.loc[~pasivos["es_corriente"], "saldo"].sum())
    pasivo_total = pasivo_corriente + pasivo_no_corriente

    # Patrimonio: saldo acreedor de cuentas 3 + utilidad del período
    patrimonio_cuentas = _filter_puc(by_account, "3")
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
    balances_hist: pd.DataFrame | None = None,
) -> dict:
    """
    Capital de Trabajo y KTNO.

    Capital de trabajo (KT) = Activo Corriente - Pasivo Corriente
    KTNO (Capital de Trabajo Neto Operativo)
        = Cuentas por cobrar (13xx) + Inventarios (14xx) - Proveedores (22xx)
    """
    bs = compute_balance_sheet(moves, chart, date_to, balances_hist=balances_hist)
    # Si tenemos balances agregados, los usamos
    if balances_hist is not None and not balances_hist.empty:
        sub = balances_hist.copy()
        chart_e = enrich_chart_with_puc(chart)
        if "code" in chart_e.columns:
            chart_e["code_normalized"] = chart_e["code"].apply(_normalize_code)
        keep_cols = [c for c in [
            "id", "code", "code_normalized", "name", "grupo",
            "es_corriente", "es_resultado", "subgrupo",
            "puc_group", "puc_subgroup", "account_type",
        ] if c in chart_e.columns]
        keep = chart_e[keep_cols].rename(columns={
            "id": "account_id", "code": "account_code",
            "code_normalized": "account_code_norm", "name": "account_name",
        })
        sub = sub.merge(keep, on="account_id", how="left")
    else:
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

    # Clasificación: usar puc_subgroup (de account_type) si existe, sino código
    if "puc_subgroup" in sub.columns:
        subg = sub["puc_subgroup"].astype(str)
    else:
        subg = sub.get(
            "account_code_norm",
            sub.get("account_code", pd.Series([""] * len(sub)))
        ).astype(str).str[:2]

    # KTNO components
    cxc = float(sub.loc[subg == "13", "saldo_deudor"].sum())
    inventario = float(sub.loc[subg == "14", "saldo_deudor"].sum())
    proveedores = float(sub.loc[subg == "22", "saldo_acreedor"].sum())
    ktno = cxc + inventario - proveedores

    # Otros componentes
    disponible = float(sub.loc[subg == "11", "saldo_deudor"].sum())
    obl_fin = float(sub.loc[subg == "21", "saldo_acreedor"].sum())

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

    # Filtrar cuentas de disponible (11xx en PUC, o asset_cash en account_type)
    if "puc_subgroup" in sub.columns:
        is_cash = sub["puc_subgroup"].astype(str) == "11"
    else:
        is_cash = sub.get(
            "account_code_norm",
            sub.get("account_code", pd.Series([""] * len(sub)))
        ).astype(str).str.startswith("11")
    cash_lines = sub[is_cash].copy()
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
        if "puc_subgroup" in historico.columns:
            hist_is_cash = historico["puc_subgroup"].astype(str) == "11"
        else:
            hist_is_cash = historico.get(
                "account_code_norm",
                historico.get("account_code", pd.Series([""] * len(historico)))
            ).astype(str).str.startswith("11")
        hist_cash = historico[hist_is_cash]
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
    balances_aggregated: pd.DataFrame | None = None,
) -> dict:
    """Desglose de gastos por subgrupo, cuenta, y mes."""
    if balances_aggregated is not None and not balances_aggregated.empty:
        sub = balances_aggregated.copy()
        chart_e = enrich_chart_with_puc(chart)
        if "code" in chart_e.columns:
            chart_e["code_normalized"] = chart_e["code"].apply(_normalize_code)
        keep_cols = [c for c in [
            "id", "code", "code_normalized", "name", "grupo", "subgrupo",
            "puc_group", "puc_subgroup", "es_corriente", "es_resultado",
        ] if c in chart_e.columns]
        keep = chart_e[keep_cols].rename(columns={
            "id": "account_id", "code": "account_code",
            "code_normalized": "account_code_norm", "name": "account_name",
        })
        sub = sub.merge(keep, on="account_id", how="left")
        # No tenemos `date` en aggregated → no podemos hacer breakdown por mes
        sub["date"] = pd.Timestamp(date_from)
    else:
        sub = _filter_moves(moves, date_from, date_to)
        sub = _join_moves_chart(sub, chart)
    if sub.empty:
        return {
            "total_gastos": 0,
            "por_subgrupo": pd.DataFrame(),
            "por_cuenta": pd.DataFrame(),
            "por_mes": pd.DataFrame(),
        }

    # Solo gastos (grupo 5): 51 Administrativos, 52 Ventas, 53 No operacionales/financieros
    # NO incluir costos (grupo 6) - esos van en el costo de ventas, no en análisis de gastos
    if "puc_group" in sub.columns:
        is_exp = sub["puc_group"].astype(str) == "5"
    else:
        is_exp = sub.get(
            "account_code_norm",
            sub.get("account_code", pd.Series([""] * len(sub)))
        ).astype(str).str.startswith("5")
    gastos = sub[is_exp].copy()
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

    # Clasificación PRIMARIA: puc_group/puc_subgroup (de account_type)
    if "puc_group" in sub.columns:
        puc_g = sub["puc_group"].astype(str)
        puc_sg = sub["puc_subgroup"].astype(str) if "puc_subgroup" in sub.columns else puc_g
    else:
        codes = sub.get(
            "account_code_norm",
            sub.get("account_code", pd.Series([""] * len(sub)))
        ).astype(str)
        puc_g = codes.str[:1]
        puc_sg = codes.str[:2]

    sub["ingreso_op"] = (sub["credit"] - sub["debit"]).where(puc_sg == "41", 0)
    sub["ingreso_no_op"] = (sub["credit"] - sub["debit"]).where(puc_sg == "42", 0)
    sub["costo"] = (sub["debit"] - sub["credit"]).where(puc_g == "6", 0)
    sub["gasto_admin"] = (sub["debit"] - sub["credit"]).where(puc_sg == "51", 0)
    sub["gasto_ventas"] = (sub["debit"] - sub["credit"]).where(puc_sg == "52", 0)
    sub["gasto_no_op"] = (sub["debit"] - sub["credit"]).where(puc_sg == "53", 0)
    sub["impto"] = (sub["debit"] - sub["credit"]).where(puc_sg == "54", 0)
    # Si no hay subdivisión 51/52/53 (account_type genérico), todo a admin
    if sub["gasto_admin"].sum() == 0 and sub["gasto_ventas"].sum() == 0:
        sub["gasto_admin"] = (sub["debit"] - sub["credit"]).where(puc_g == "5", 0)

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
