# -*- coding: utf-8 -*-
"""
src/medios_magneticos.py
========================
Generación de Información Exógena (Medios Magnéticos) para la DIAN
de Colombia.

Formatos soportados:
  - 1001: Pagos o abonos en cuenta y retenciones practicadas
  - 1003: Retenciones que nos practicaron (en ventas)
  - 1004: Descuentos tributarios
  - 1005: IVA descontable
  - 1006: IVA generado
  - 1007: Ingresos recibidos
  - 1008: Cuentas por cobrar al 31 de diciembre
  - 1009: Cuentas por pagar al 31 de diciembre
  - 1010: Información de socios y accionistas
  - 1011: Información declaraciones tributarias (consolidado)
  - 1012: Información de saldos (caja, bancos, inversiones)

NOTAS:
  - Los formatos 1010, 1011 y 1012 requieren información que NO siempre
    está completa en Odoo. Se generan plantillas con los datos disponibles
    y el usuario debe completar manualmente lo que falte.
  - El mapeo PUC → concepto DIAN es GENÉRICO. Cada empresa puede
    requerir ajustes según su catálogo contable específico.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

import pandas as pd

from .financial_statements import enrich_chart_with_puc


def _enrich_moves_with_puc(
    moves: pd.DataFrame,
    chart: pd.DataFrame,
) -> pd.DataFrame:
    """
    Enriquece movimientos contables con columnas de clasificación PUC.

    GARANTÍA: filtra explícitamente parent_state == 'posted' para excluir
    asientos en borrador o cancelados, aunque el extractor ya lo haga
    server-side (doble validación defensiva).

    Agrega:
      - account_code: código real (o inferido desde account_id_name)
      - account_name: nombre de la cuenta
      - puc_group: '1', '2', '3', '4', '5', '6', '7'
      - puc_subgroup: '11', '14', '21', '22', '23', '24', '41', '51', etc.

    Usa la misma lógica robusta que financial_statements: si el `code`
    de la cuenta no es código PUC, infiere desde `account_type` y nombre.
    """
    if moves is None or moves.empty:
        return moves

    # DOBLE VALIDACIÓN: filtrar parent_state=posted si la columna existe.
    # El extractor ya lo hace server-side pero esto garantiza que si en
    # el futuro alguien cambia el extractor, los formatos DIAN sigan
    # excluyendo borradores y cancelados.
    moves = moves.copy()
    if "parent_state" in moves.columns:
        moves = moves[moves["parent_state"] == "posted"]
    elif "state" in moves.columns:
        moves = moves[moves["state"] == "posted"]
    if chart is None or chart.empty:
        df = moves.copy()
        df["account_code"] = df.get("account_code", "").astype(str)
        df["puc_group"] = ""
        df["puc_subgroup"] = ""
        return df

    # Enriquecer chart con clasificación PUC
    chart_e = enrich_chart_with_puc(chart)
    keep_cols = [c for c in [
        "id", "code", "name", "puc_group", "puc_subgroup",
        "subgrupo", "account_type",
    ] if c in chart_e.columns]
    chart_min = chart_e[keep_cols].rename(columns={
        "id": "account_id",
        "code": "account_code",
        "name": "account_name",
    })

    df = moves.copy()
    # Si moves ya tiene account_code (de un merge previo), no lo pisamos
    if "account_code" in df.columns and df["account_code"].notna().any():
        # Hacer merge solo para añadir puc_group/puc_subgroup
        cols_to_add = [
            "account_id", "puc_group", "puc_subgroup",
        ]
        if "account_name" not in df.columns:
            cols_to_add.append("account_name")
        df = df.merge(chart_min[cols_to_add], on="account_id", how="left")
    else:
        df = df.merge(chart_min, on="account_id", how="left")

    # Defaults seguros
    df["account_code"] = df["account_code"].fillna("").astype(str)
    df["puc_group"] = df["puc_group"].fillna("").astype(str)
    df["puc_subgroup"] = df["puc_subgroup"].fillna("").astype(str)
    return df


# ===========================================================================
# Mapeo genérico PUC colombiano → Concepto DIAN
# ===========================================================================
#
# Para FORMATO 1001 (pagos a terceros), los conceptos DIAN principales son:
#   5001 Salarios, prestaciones sociales y demás pagos laborales
#   5002 Honorarios
#   5003 Comisiones
#   5004 Servicios
#   5005 Arrendamientos
#   5006 Intereses y rendimientos financieros
#   5007 Otros costos y deducciones
#   5009 Compra de bienes raíces
#   5010 Compras de activos fijos diferentes a bienes raíces
#   5011 Aportes a sistemas de seguridad social en salud
#   5012 Aportes a pensiones obligatorias
#   ... (lista completa en resoluciones DIAN anuales)
#
# Mapeo simplificado por prefijo de cuenta PUC:
CONCEPTOS_1001_POR_PUC: dict[str, str] = {
    # Gastos personales / laborales
    "5105": "5001",  # Gastos de personal → Salarios
    "5106": "5001",
    "5108": "5001",
    "7105": "5001",  # Costos de personal (CMV)
    # Honorarios
    "5110": "5002",  # Honorarios → Honorarios
    # Comisiones
    "5115": "5003",
    # Servicios técnicos / asistencia técnica / servicios generales
    "5135": "5004",  # Servicios → Servicios
    "5140": "5004",
    "5145": "5004",
    "5150": "5004",
    "5155": "5004",
    "5160": "5004",
    "5165": "5004",
    # Arrendamientos
    "5120": "5005",
    "5125": "5005",
    # Intereses y financieros
    "5305": "5006",
    "5310": "5006",
    "5315": "5006",
    # Compras de mercancía (CMV)
    "6135": "5009",  # Costo de ventas → Compras (genérico)
    "1435": "5009",  # Inventario → Compras
    # Activos fijos
    "1504": "5010",  # Terrenos
    "1516": "5010",  # Construcciones
    "1520": "5010",  # Maquinaria y equipo
    "1524": "5010",  # Muebles y enseres
    "1528": "5010",  # Equipo computación
    "1540": "5010",  # Vehículos
    # Default
    "_default": "5007",  # Otros costos y deducciones
}

# Para FORMATO 1007 (ingresos):
#   4001 Operacionales — venta neta de bienes/servicios
#   4002 Otros ingresos
CONCEPTOS_1007_POR_PUC: dict[str, str] = {
    "4135": "4001",  # Comercio al por mayor → Operacional
    "4140": "4001",
    "4145": "4001",
    "4150": "4001",
    "4155": "4001",
    "4160": "4001",
    "4165": "4001",
    "4170": "4002",  # Otros ingresos
    "_default": "4001",
}

# Para FORMATO 1009 (cuentas por pagar):
#   2201 Proveedores nacionales
#   2202 Proveedores del exterior
#   2335 Costos y gastos por pagar
#   2370 Retenciones y aportes laborales por pagar
#   ...
# Concepto único típicamente: 1303-1399
CONCEPTOS_1009_POR_PUC: dict[str, str] = {
    "2205": "1301",  # Proveedores → Saldo a favor proveedores
    "2335": "1306",  # Costos y gastos por pagar
    "2380": "1304",  # Acreedores varios
    "_default": "1306",
}


def _get_concepto(account_code: str, mapping: dict) -> str:
    """Mapea código PUC a concepto DIAN usando el mapeo dado."""
    if not account_code:
        return mapping.get("_default", "")
    s = str(account_code).strip()
    # Buscar primero el prefijo exacto de 4 dígitos
    if len(s) >= 4 and s[:4] in mapping:
        return mapping[s[:4]]
    # Después prefijo de 2 dígitos (grupo)
    if len(s) >= 2 and s[:2] in mapping:
        return mapping[s[:2]]
    return mapping.get("_default", "")


# ===========================================================================
# Helpers de identificación de terceros
# ===========================================================================

# Mapeo de tipos de documento DIAN
TIPO_DOC_DIAN: dict[str, str] = {
    "11": "Registro civil",
    "12": "Tarjeta de identidad",
    "13": "Cédula de ciudadanía",
    "21": "Tarjeta de extranjería",
    "22": "Cédula de extranjería",
    "31": "NIT",
    "41": "Pasaporte",
    "42": "Documento extranjero",
    "43": "Sin identificación del exterior",
}


def _get_partner_document(partner: dict) -> str:
    """
    Obtiene el documento del partner probando múltiples campos en orden
    de prioridad (importante para Odoo con localización Colombia):
      1. vat (estándar Odoo)
      2. l10n_co_document_number (localización CO)
      3. identification_document (otro módulo CO)
      4. ref (campo de referencia interna, algunos lo usan para cédula)
    """
    for field in ("vat", "l10n_co_document_number",
                  "identification_document", "ref"):
        value = partner.get(field)
        if value and str(value).strip() and str(value).strip().lower() != "false":
            return str(value).strip()
    return ""


def _infer_tipo_doc(partner: dict) -> str:
    """
    Infiere el tipo de documento DIAN desde info del partner.

    Si hay `l10n_latam_identification_type_id` (localización CO), usa
    ese mapping directamente. Si no, heurística por is_company/longitud.
    """
    # Intentar leer el tipo de localización CO
    id_type = partner.get("l10n_latam_identification_type_id")
    if isinstance(id_type, (list, tuple)) and len(id_type) > 1:
        name = str(id_type[1]).lower()
        if "nit" in name:
            return "31"
        if "cedula de ciudadan" in name or "cédula de ciudadan" in name:
            return "13"
        if "tarjeta de identidad" in name:
            return "12"
        if "cedula de extranjer" in name or "cédula de extranjer" in name:
            return "22"
        if "pasaporte" in name:
            return "41"

    is_company = partner.get("is_company", False)
    doc = _get_partner_document(partner)
    if is_company:
        return "31"  # NIT
    if doc:
        return "13"
    return "13"  # Default


def _clean_vat(vat_or_doc: str, partner_dv: Optional[str] = None) -> tuple[str, str]:
    """
    Limpia un NIT/cédula. Devuelve (número_sin_dv, dv).
    Si tiene formato '900123456-7' separa el DV.
    Si se pasa `partner_dv` separadamente (de l10n_co_dv), lo usa.
    """
    if not vat_or_doc:
        return "", ""
    v = str(vat_or_doc).replace(".", "").replace(" ", "").strip()
    if v.lower() == "false":
        return "", ""
    # Si ya viene con guión (NIT colombiano)
    if "-" in v:
        parts = v.split("-")
        return parts[0], parts[1] if len(parts) > 1 else (partner_dv or "")
    # Si no tiene guión pero tenemos DV externo, usarlo
    return v, str(partner_dv or "").strip()


def _split_nombre(partner_name: str) -> tuple[str, str, str, str]:
    """
    Divide un nombre en (apellido1, apellido2, nombre1, nombre2).
    Para personas naturales.
    """
    if not partner_name:
        return "", "", "", ""
    parts = partner_name.strip().split()
    if len(parts) == 1:
        return parts[0], "", "", ""
    if len(parts) == 2:
        return parts[0], "", parts[1], ""
    if len(parts) == 3:
        return parts[0], parts[1], parts[2], ""
    if len(parts) >= 4:
        return parts[0], parts[1], parts[2], " ".join(parts[3:])
    return "", "", "", ""


def _row_tercero(partner: dict) -> dict:
    """
    Construye los campos comunes de tercero para los formatos DIAN.

    Devuelve dict con:
      tipo_doc, numero_doc, dv,
      apellido1, apellido2, nombre1, nombre2, razon_social,
      direccion, departamento, municipio, pais
    """
    tipo_doc = _infer_tipo_doc(partner)
    doc = _get_partner_document(partner)
    dv_externo = partner.get("l10n_co_dv") or ""
    numero_doc, dv = _clean_vat(doc, partner_dv=str(dv_externo))
    name = partner.get("name", "")
    is_company = partner.get("is_company", False)

    if is_company:
        razon_social = name
        ap1 = ap2 = no1 = no2 = ""
    else:
        razon_social = ""
        ap1, ap2, no1, no2 = _split_nombre(name)

    return {
        "tipo_doc": tipo_doc,
        "numero_doc": numero_doc,
        "dv": dv,
        "apellido1": ap1,
        "apellido2": ap2,
        "nombre1": no1,
        "nombre2": no2,
        "razon_social": razon_social,
        "direccion": partner.get("street", "") or "",
        "departamento": partner.get("state_name", "") or "",
        "municipio": partner.get("city", "") or "",
        "pais": partner.get("country_name", "Colombia") or "Colombia",
    }


# ===========================================================================
# FORMATO 1001 — Pagos o abonos en cuenta y retenciones practicadas
# ===========================================================================


def build_formato_1001(
    moves: pd.DataFrame,
    chart: pd.DataFrame,
    partners: pd.DataFrame,
    year: int,
    umbral_minimo: float = 0,
) -> pd.DataFrame:
    """
    Construye Formato 1001: pagos a terceros por concepto.

    Toma movimientos de cuentas grupo 5 (gastos) y 6 (costos), agrupados
    por (tercero, concepto). El "pago" es el débito a esas cuentas.

    Retenciones: suma de movimientos a cuentas 2365 (Retención en la
    fuente) y 2367 (IVA retenido) del mismo período por cada tercero.

    Args:
        umbral_minimo: monto mínimo para incluir un pago. Por defecto 0
            (sin filtro). Ajusta según resolución DIAN.
    """
    if moves is None or moves.empty:
        return pd.DataFrame()

    df = moves.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df[df["date"].dt.year == year]
    if df.empty:
        return pd.DataFrame()

    # Enriquecer con clasificación PUC robusta (igual que Estados Financieros)
    df = _enrich_moves_with_puc(df, chart)

    # Filtrar pagos a terceros: usa puc_group (más robusto que el code)
    pagos_mask = df["puc_group"].isin(["5", "6"]) | (
        df["account_code"].str.startswith("143")
    )
    pagos = df[pagos_mask & (df["partner_id"].notna())].copy()
    if pagos.empty:
        return pd.DataFrame()

    pagos["monto"] = pagos["debit"].fillna(0) - pagos["credit"].fillna(0)
    pagos["concepto"] = pagos["account_code"].apply(
        lambda c: _get_concepto(c, CONCEPTOS_1001_POR_PUC)
    )

    # ── Retenciones cruzadas por ASIENTO (move_id) ──
    # Las retenciones en Colombia siempre se contabilizan en el MISMO
    # asiento contable que el pago/factura. Por eso es más confiable
    # cruzar por move_id que por partner_id (que puede no estar bien
    # asignado en algunas líneas de retención).
    ret_mask = df["account_code"].str.startswith(("2365", "2367", "2368"))
    rets = df[ret_mask].copy()
    rets_by_move = pd.DataFrame()
    if not rets.empty and "move_id" in rets.columns:
        rets["monto_ret"] = rets["credit"].fillna(0) - rets["debit"].fillna(0)
        rets["tipo_ret"] = rets["account_code"].str[:4].map({
            "2365": "ret_fuente_renta",
            "2367": "ret_iva",
            "2368": "ret_ica",
        })
        # Pivot por move_id × tipo_ret
        rets_by_move = rets.pivot_table(
            index="move_id", columns="tipo_ret",
            values="monto_ret", aggfunc="sum", fill_value=0,
        ).reset_index()

    # Asociar retenciones al pago vía move_id, agrupado por (partner, concepto)
    if not rets_by_move.empty and "move_id" in pagos.columns:
        pagos_con_ret = pagos.merge(rets_by_move, on="move_id", how="left")
    else:
        pagos_con_ret = pagos.copy()
    for col in ("ret_fuente_renta", "ret_iva", "ret_ica"):
        if col not in pagos_con_ret.columns:
            pagos_con_ret[col] = 0
        pagos_con_ret[col] = pagos_con_ret[col].fillna(0)

    # Agrupar pagos + retenciones por (tercero, concepto)
    grp = pagos_con_ret.groupby(
        ["partner_id", "concepto"], as_index=False,
    ).agg(
        pago_deducible=("monto", "sum"),
        ret_fuente_renta=("ret_fuente_renta", "sum"),
        ret_iva=("ret_iva", "sum"),
        ret_ica=("ret_ica", "sum"),
    )

    # Join con info de tercero
    if partners is not None and not partners.empty:
        partner_info = partners.set_index("id")
        out_rows = []
        for _, r in grp.iterrows():
            pid = r["partner_id"]
            try:
                p = partner_info.loc[int(pid)].to_dict() if pid in partner_info.index else {}
            except Exception:  # noqa: BLE001
                p = {}
            t = _row_tercero(p)
            row = {
                **t,
                "partner_id": int(pid) if pid is not None else None,
                "concepto": r["concepto"],
                "pago_deducible": round(float(r["pago_deducible"]), 0),
                "pago_no_deducible": 0,
                "ret_fuente_renta": round(float(r["ret_fuente_renta"]), 0),
                "ret_iva": round(float(r["ret_iva"]), 0),
                "ret_ica": round(float(r["ret_ica"]), 0),
            }
            out_rows.append(row)
        out = pd.DataFrame(out_rows)
    else:
        out = grp

    # Aplicar umbral si > 0
    if umbral_minimo > 0 and "pago_deducible" in out.columns:
        out = out[
            out["pago_deducible"].abs() >= umbral_minimo
        ].reset_index(drop=True)
    return out


def diagnosticar_formato_1001(
    moves: pd.DataFrame,
    chart: pd.DataFrame,
    partners: pd.DataFrame,
    year: int,
) -> dict:
    """
    Diagnóstico del Formato 1001. Identifica problemas:

    1. Retenciones SIN pago asociado en el mismo asiento (cuenta 2365/67/68
       con tercero pero sin gasto en el mismo move_id).
    2. Retenciones con partner_id distinto al del pago (puede indicar
       error en la contabilización).
    3. Terceros sin NIT o datos incompletos.

    Devuelve dict con:
      - sin_nit: DF con terceros sin NIT que tienen pagos
      - ret_huerfanas: retenciones sin pago en mismo asiento
      - ret_diferente_partner: retenciones con partner distinto al pago
    """
    if moves is None or moves.empty:
        return {}
    df = moves.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df[df["date"].dt.year == year]
    if df.empty:
        return {}
    df = _enrich_moves_with_puc(df, chart)

    # 1) Pagos a terceros
    pagos = df[
        df["puc_group"].isin(["5", "6"]) & df["partner_id"].notna()
    ].copy()
    pagos["monto"] = pagos["debit"].fillna(0) - pagos["credit"].fillna(0)

    # 2) Retenciones
    rets = df[
        df["account_code"].str.startswith(("2365", "2367", "2368"))
    ].copy()
    rets["monto_ret"] = rets["credit"].fillna(0) - rets["debit"].fillna(0)

    diag = {}

    # === A) Terceros con pagos pero SIN NIT ===
    # Devolvemos DOS DataFrames:
    #   - sin_nit (resumen): partner_id, nombre, total pagos
    #   - sin_nit_detalle: una fila por asiento contable, con
    #     move_id_name, ref, fecha, account_code, account_name, monto
    if partners is not None and not partners.empty and not pagos.empty:
        partners_idx = partners.set_index("id")
        # Identificar partners sin NIT
        partners_sin_nit_ids = set()
        partners_info = {}
        for pid in pagos["partner_id"].dropna().unique():
            try:
                p = partners_idx.loc[int(pid)].to_dict() if pid in partners_idx.index else {}
            except Exception:  # noqa: BLE001
                p = {}
            # Usar _get_partner_document que revisa varios campos (vat,
            # l10n_co_document_number, identification_document, ref)
            doc = _get_partner_document(p)
            partners_info[int(pid)] = {
                "name": p.get("name", "(sin nombre)"),
                "documento": doc,
            }
            if not doc:
                partners_sin_nit_ids.add(int(pid))

        # A.1) Resumen: total pagos por partner sin NIT
        pagos_sin_nit = pagos[pagos["partner_id"].isin(partners_sin_nit_ids)]
        if not pagos_sin_nit.empty:
            resumen = pagos_sin_nit.groupby("partner_id", as_index=False).agg(
                monto_pagos=("monto", "sum"),
                n_asientos=("move_id", "nunique") if "move_id" in pagos_sin_nit.columns else ("monto", "count"),
            )
            resumen["nombre"] = resumen["partner_id"].map(
                lambda p: partners_info.get(int(p), {}).get("name", "")
            )
            diag["sin_nit"] = resumen[[
                "partner_id", "nombre", "monto_pagos", "n_asientos",
            ]].sort_values("monto_pagos", ascending=False).reset_index(drop=True)

            # A.2) Detalle de cada asiento donde aparecen
            detalle_cols = [c for c in [
                "partner_id", "date", "move_id", "move_id_name",
                "ref", "name",
                "account_code", "account_name",
                "debit", "credit", "monto",
            ] if c in pagos_sin_nit.columns]
            det = pagos_sin_nit[detalle_cols].copy()
            det["nombre_tercero"] = det["partner_id"].map(
                lambda p: partners_info.get(int(p), {}).get("name", "")
            )
            # Reordenar columnas
            cols_order = ["partner_id", "nombre_tercero", "date"]
            if "move_id_name" in det.columns:
                cols_order.append("move_id_name")
            elif "move_id" in det.columns:
                cols_order.append("move_id")
            if "ref" in det.columns:
                cols_order.append("ref")
            if "name" in det.columns:
                cols_order.append("name")
            if "account_code" in det.columns:
                cols_order.append("account_code")
            if "account_name" in det.columns:
                cols_order.append("account_name")
            cols_order.append("monto")
            det = det[[c for c in cols_order if c in det.columns]]
            det = det.sort_values(["partner_id", "date"]).reset_index(drop=True)
            diag["sin_nit_detalle"] = det
        else:
            diag["sin_nit"] = pd.DataFrame()
            diag["sin_nit_detalle"] = pd.DataFrame()

    # === B) Retenciones HUÉRFANAS: con tercero pero sin pago en el mismo asiento ===
    if not rets.empty and "move_id" in rets.columns:
        moves_con_pago = set(pagos["move_id"].dropna().unique())
        rets_con_partner = rets[rets["partner_id"].notna()].copy()
        rets_huerfanas = rets_con_partner[
            ~rets_con_partner["move_id"].isin(moves_con_pago)
        ].copy()
        if not rets_huerfanas.empty:
            grp_h = rets_huerfanas.groupby(
                "partner_id", as_index=False,
            )["monto_ret"].sum()
            if partners is not None and not partners.empty:
                grp_h["nombre"] = grp_h["partner_id"].map(
                    partners.set_index("id")["name"]
                )
            diag["ret_huerfanas"] = grp_h.sort_values(
                "monto_ret", ascending=False,
            )

    # === C) Retenciones con partner_id DIFERENTE al partner del pago ===
    if not rets.empty and not pagos.empty and "move_id" in rets.columns:
        # Partner por move (del pago)
        partner_pago_por_move = (
            pagos.dropna(subset=["partner_id"])
            .groupby("move_id")["partner_id"]
            .first()
        )
        # Partner de la retención
        rets_con_pago = rets.dropna(subset=["partner_id"]).merge(
            partner_pago_por_move.rename("partner_pago"),
            on="move_id", how="inner",
        )
        diff = rets_con_pago[
            rets_con_pago["partner_id"] != rets_con_pago["partner_pago"]
        ]
        if not diff.empty:
            if partners is not None and not partners.empty:
                partner_names = partners.set_index("id")["name"]
                diff = diff.copy()
                diff["nombre_ret"] = diff["partner_id"].map(partner_names)
                diff["nombre_pago"] = diff["partner_pago"].map(partner_names)
            diag["ret_diferente_partner"] = diff[[
                c for c in [
                    "move_id", "partner_id", "nombre_ret",
                    "partner_pago", "nombre_pago",
                    "account_code", "monto_ret",
                ] if c in diff.columns
            ]]

    return diag


# ===========================================================================
# FORMATO 1003 — Retenciones que NOS practicaron (en ventas)
# ===========================================================================


def build_formato_1003(
    moves: pd.DataFrame,
    chart: pd.DataFrame,
    partners: pd.DataFrame,
    year: int,
) -> pd.DataFrame:
    """
    Retenciones que clientes practicaron a la empresa.

    Cuentas típicas: 1355 (Anticipo de impuestos y contribuciones).
    """
    if moves is None or moves.empty:
        return pd.DataFrame()
    df = moves.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df[df["date"].dt.year == year]
    if df.empty:
        return pd.DataFrame()
    df = _enrich_moves_with_puc(df, chart)

    # 1355xx = anticipos y retenciones practicadas a la empresa
    ret_mask = df["account_code"].str.startswith("1355")
    rets = df[ret_mask & (df["partner_id"].notna())].copy()
    if rets.empty:
        return pd.DataFrame()
    rets["monto_ret"] = rets["debit"].fillna(0) - rets["credit"].fillna(0)
    grp = rets.groupby("partner_id", as_index=False)["monto_ret"].sum()

    if partners is not None and not partners.empty:
        partner_info = partners.set_index("id")
        out_rows = []
        for _, r in grp.iterrows():
            pid = r["partner_id"]
            try:
                p = partner_info.loc[int(pid)].to_dict() if pid in partner_info.index else {}
            except Exception:  # noqa: BLE001
                p = {}
            t = _row_tercero(p)
            out_rows.append({
                **t,
                "retencion_practicada": round(float(r["monto_ret"]), 0),
            })
        return pd.DataFrame(out_rows)
    return grp


# ===========================================================================
# FORMATO 1004 — Descuentos tributarios
# ===========================================================================


def build_formato_1004(
    moves: pd.DataFrame,
    chart: pd.DataFrame,
    partners: pd.DataFrame,
    year: int,
) -> pd.DataFrame:
    """
    Descuentos tributarios: cuentas 1715 (Impuestos diferidos) o 1620
    (Descuentos tributarios). Devuelve por tercero.
    """
    if moves is None or moves.empty:
        return pd.DataFrame()
    df = moves.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df[df["date"].dt.year == year]
    if df.empty:
        return pd.DataFrame()
    df = _enrich_moves_with_puc(df, chart)

    # Cuentas típicas para descuentos tributarios
    mask = df["account_code"].str.startswith(("1715", "1620"))
    sub = df[mask & (df["partner_id"].notna())].copy()
    if sub.empty:
        return pd.DataFrame()
    sub["monto"] = sub["debit"].fillna(0) - sub["credit"].fillna(0)
    grp = sub.groupby("partner_id", as_index=False)["monto"].sum()
    if grp.empty:
        return pd.DataFrame()
    return _enrich_with_partner(grp, partners, "monto", "descuento_tributario")


# ===========================================================================
# FORMATO 1005 — IVA descontable (compras)
# ===========================================================================


def build_formato_1005(
    moves: pd.DataFrame,
    chart: pd.DataFrame,
    partners: pd.DataFrame,
    year: int,
) -> pd.DataFrame:
    """
    IVA descontable por proveedor. Cuenta 2408 = IVA descontable.
    """
    if moves is None or moves.empty:
        return pd.DataFrame()
    df = moves.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df[df["date"].dt.year == year]
    if df.empty:
        return pd.DataFrame()
    df = _enrich_moves_with_puc(df, chart)

    mask = df["account_code"].str.startswith("2408")
    sub = df[mask & (df["partner_id"].notna())].copy()
    if sub.empty:
        return pd.DataFrame()
    # IVA descontable: el descuento se acredita (credit) cuando es a favor
    sub["iva_descontable"] = sub["debit"].fillna(0) - sub["credit"].fillna(0)
    grp = sub.groupby("partner_id", as_index=False)["iva_descontable"].sum()
    grp = grp[grp["iva_descontable"].abs() >= 1]
    return _enrich_with_partner(grp, partners, "iva_descontable", "iva_descontable")


# ===========================================================================
# FORMATO 1006 — IVA generado (ventas)
# ===========================================================================


def build_formato_1006(
    moves: pd.DataFrame,
    chart: pd.DataFrame,
    partners: pd.DataFrame,
    year: int,
) -> pd.DataFrame:
    """IVA generado por cliente. Cuenta 2408 cuando es por ventas."""
    if moves is None or moves.empty:
        return pd.DataFrame()
    df = moves.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df[df["date"].dt.year == year]
    if df.empty:
        return pd.DataFrame()
    df = _enrich_moves_with_puc(df, chart)

    mask = df["account_code"].str.startswith(("2408", "240805"))
    sub = df[mask & (df["partner_id"].notna())].copy()
    if sub.empty:
        return pd.DataFrame()
    # IVA generado: crédito a 2408 cuando se factura
    sub["iva_generado"] = sub["credit"].fillna(0) - sub["debit"].fillna(0)
    grp = sub.groupby("partner_id", as_index=False)["iva_generado"].sum()
    grp = grp[grp["iva_generado"] > 0]
    return _enrich_with_partner(grp, partners, "iva_generado", "iva_generado")


# ===========================================================================
# FORMATO 1007 — Ingresos recibidos
# ===========================================================================


def build_formato_1007(
    moves: pd.DataFrame,
    chart: pd.DataFrame,
    partners: pd.DataFrame,
    year: int,
) -> pd.DataFrame:
    """
    Ingresos por tercero (cliente). Suma los créditos a cuentas grupo 4
    (ingresos) del año fiscal, agrupado por (tercero, concepto).
    """
    if moves is None or moves.empty:
        return pd.DataFrame()
    df = moves.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df[df["date"].dt.year == year]
    if df.empty:
        return pd.DataFrame()
    df = _enrich_moves_with_puc(df, chart)

    mask = df["puc_group"] == "4"
    sub = df[mask & (df["partner_id"].notna())].copy()
    if sub.empty:
        return pd.DataFrame()
    sub["ingreso"] = sub["credit"].fillna(0) - sub["debit"].fillna(0)
    sub["concepto"] = sub["account_code"].apply(
        lambda c: _get_concepto(c, CONCEPTOS_1007_POR_PUC)
    )
    grp = sub.groupby(["partner_id", "concepto"], as_index=False)["ingreso"].sum()
    grp = grp[grp["ingreso"] > 0]
    return _enrich_with_partner(grp, partners, "ingreso", "ingreso_bruto")


# ===========================================================================
# FORMATO 1008 — Cuentas por cobrar al 31 de diciembre
# ===========================================================================


def build_formato_1008(
    moves: pd.DataFrame,
    chart: pd.DataFrame,
    partners: pd.DataFrame,
    year: int,
) -> pd.DataFrame:
    """
    Saldo de cuentas por cobrar (subgrupo 13) al 31 dic del año.
    """
    if moves is None or moves.empty:
        return pd.DataFrame()
    df = moves.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df[df["date"].dt.date <= date(year, 12, 31)]
    if df.empty:
        return pd.DataFrame()
    df = _enrich_moves_with_puc(df, chart)

    # CxC: subgrupo 13 (deudores comerciales) usando PUC robusto + fallback código
    mask = (df["puc_subgroup"] == "13") | df["account_code"].str.startswith((
        "1305", "1310", "1325", "1330",
    ))
    sub = df[mask & (df["partner_id"].notna())].copy()
    if sub.empty:
        return pd.DataFrame()
    sub["saldo"] = sub["debit"].fillna(0) - sub["credit"].fillna(0)
    grp = sub.groupby("partner_id", as_index=False)["saldo"].sum()
    grp = grp[grp["saldo"] > 0]  # solo deudoras
    return _enrich_with_partner(grp, partners, "saldo", "saldo_cuenta_cobrar")


# ===========================================================================
# FORMATO 1009 — Cuentas por pagar al 31 de diciembre
# ===========================================================================


def build_formato_1009(
    moves: pd.DataFrame,
    chart: pd.DataFrame,
    partners: pd.DataFrame,
    year: int,
) -> pd.DataFrame:
    """Saldos de CxP (subgrupos 22-23) al 31 dic."""
    if moves is None or moves.empty:
        return pd.DataFrame()
    df = moves.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df[df["date"].dt.date <= date(year, 12, 31)]
    if df.empty:
        return pd.DataFrame()
    df = _enrich_moves_with_puc(df, chart)

    # CxP: subgrupos 22 (proveedores) y 23 (cuentas por pagar)
    mask = df["puc_subgroup"].isin(["22", "23"]) | df["account_code"].str.startswith((
        "22", "2335", "2380",
    ))
    sub = df[mask & (df["partner_id"].notna())].copy()
    if sub.empty:
        return pd.DataFrame()
    sub["saldo"] = sub["credit"].fillna(0) - sub["debit"].fillna(0)
    sub["concepto"] = sub["account_code"].apply(
        lambda c: _get_concepto(c, CONCEPTOS_1009_POR_PUC)
    )
    grp = sub.groupby(["partner_id", "concepto"], as_index=False)["saldo"].sum()
    grp = grp[grp["saldo"] > 0]  # solo acreedoras
    return _enrich_with_partner(grp, partners, "saldo", "saldo_cuenta_pagar")


# ===========================================================================
# FORMATO 1010 — Información de socios y accionistas
# ===========================================================================


def build_formato_1010(
    partners: pd.DataFrame,
    year: int,
) -> pd.DataFrame:
    """
    Información de socios/accionistas. Odoo NO marca terceros como socios
    automáticamente. Devuelve una plantilla con todos los partners tipo
    'company' o con campo `is_shareholder` si existe, para que el usuario
    revise y complete.
    """
    if partners is None or partners.empty:
        return pd.DataFrame()
    df = partners.copy()
    # Tomar partners que potencialmente sean socios (heurística simple)
    if "is_shareholder" in df.columns:
        df = df[df["is_shareholder"] == True]  # noqa: E712
    else:
        # Sin campo específico, devolvemos plantilla con TODOS los partners
        # y el usuario debe filtrar manualmente cuáles son socios
        pass
    rows = []
    for _, p in df.iterrows():
        t = _row_tercero(p.to_dict())
        rows.append({
            **t,
            "porcentaje_participacion": 0,  # llenar manualmente
            "valor_patrimonio": 0,
        })
    return pd.DataFrame(rows)


# ===========================================================================
# FORMATO 1011 — Información declaraciones tributarias (consolidado)
# ===========================================================================


def build_formato_1011(
    moves: pd.DataFrame,
    chart: pd.DataFrame,
    year: int,
) -> pd.DataFrame:
    """
    Información agregada de la declaración de renta del año.

    Devuelve totales de:
      - Ingresos brutos
      - Costos
      - Gastos operacionales
      - Gastos no operacionales
      - Impuesto al patrimonio (si aplica)
      - Renta exenta
    """
    if moves is None or moves.empty:
        return pd.DataFrame()
    df = moves.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df[df["date"].dt.year == year]
    if df.empty:
        return pd.DataFrame()
    df = _enrich_moves_with_puc(df, chart)

    def _suma_subgroup(subgroup: str) -> float:
        m = df["puc_subgroup"] == subgroup
        sub = df[m]
        return float(sub["credit"].sum() - sub["debit"].sum())

    def _suma_group(group: str) -> float:
        m = df["puc_group"] == group
        sub = df[m]
        return float(sub["credit"].sum() - sub["debit"].sum())

    rows = [
        ("Concepto", "Valor"),
        ("Ingresos brutos operacionales (41)", _suma_subgroup("41")),
        ("Ingresos no operacionales (42)", _suma_subgroup("42")),
        ("Costo de ventas (6)", -_suma_group("6")),  # invertir signo
        ("Gastos administración (51)", -_suma_subgroup("51")),
        ("Gastos ventas (52)", -_suma_subgroup("52")),
        ("Gastos no operacionales (53)", -_suma_subgroup("53")),
    ]
    return pd.DataFrame(rows[1:], columns=rows[0])


# ===========================================================================
# FORMATO 1012 — Información de saldos (caja, bancos, inversiones)
# ===========================================================================


def build_formato_1012(
    moves: pd.DataFrame,
    chart: pd.DataFrame,
    year: int,
) -> pd.DataFrame:
    """
    Saldos al 31 dic de cuentas de:
      - 1105 Caja
      - 1110 Bancos
      - 1120 Cuentas de ahorro
      - 1205 Inversiones
      - 1305 CxC
    """
    if moves is None or moves.empty:
        return pd.DataFrame()
    df = moves.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df[df["date"].dt.date <= date(year, 12, 31)]
    if df.empty:
        return pd.DataFrame()
    df = _enrich_moves_with_puc(df, chart)

    # Subgrupos 11 (Disponible: caja/bancos) y 12 (Inversiones)
    mask = df["puc_subgroup"].isin(["11", "12"]) | df["account_code"].str.startswith(
        ("11", "12")
    )
    sub = df[mask].copy()
    if sub.empty:
        return pd.DataFrame()
    sub["saldo"] = sub["debit"].fillna(0) - sub["credit"].fillna(0)
    grp = sub.groupby(
        ["account_code", "account_name"], as_index=False,
    )["saldo"].sum()
    return grp.sort_values("account_code").reset_index(drop=True)


# ===========================================================================
# Helper: enriquecer DataFrame con info de tercero
# ===========================================================================


def _enrich_with_partner(
    df: pd.DataFrame,
    partners: pd.DataFrame,
    valor_col: str,
    nombre_final: str,
) -> pd.DataFrame:
    """Agrega columnas de tercero a un DataFrame que tenga partner_id."""
    if partners is None or partners.empty or df.empty:
        return df
    partner_info = partners.set_index("id")
    rows = []
    for _, r in df.iterrows():
        pid = r["partner_id"]
        try:
            p = partner_info.loc[int(pid)].to_dict() if pid in partner_info.index else {}
        except Exception:  # noqa: BLE001
            p = {}
        t = _row_tercero(p)
        row = {**t}
        # Conservar columnas adicionales del df original (ej. concepto)
        for c in df.columns:
            if c not in ("partner_id", valor_col):
                row[c] = r[c]
        row[nombre_final] = round(float(r[valor_col]), 0)
        rows.append(row)
    return pd.DataFrame(rows)


# ===========================================================================
# FORMATO 1015 — Pasivos al 31 de diciembre
# ===========================================================================


def build_formato_1015(
    moves: pd.DataFrame,
    chart: pd.DataFrame,
    partners: pd.DataFrame,
    year: int,
) -> pd.DataFrame:
    """
    Saldos de pasivos al cierre del año. Incluye:
      - 21xx Obligaciones financieras (préstamos bancarios)
      - 22xx Proveedores
      - 23xx Cuentas por pagar
      - 24xx Impuestos por pagar
      - 25xx Obligaciones laborales

    NOTA: 1015 a veces se usa solo para obligaciones financieras (21xx).
    Aquí devolvemos TODAS las cuentas 21-25 con su tercero.
    """
    if moves is None or moves.empty:
        return pd.DataFrame()
    df = moves.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df[df["date"].dt.date <= date(year, 12, 31)]
    if df.empty:
        return pd.DataFrame()
    df = _enrich_moves_with_puc(df, chart)

    # Pasivos: puc_group "2" (pasivos) — incluye 21-25
    mask = (df["puc_group"] == "2") | df["account_code"].str.startswith(
        ("21", "22", "23", "24", "25")
    )
    sub = df[mask & (df["partner_id"].notna())].copy()
    if sub.empty:
        return pd.DataFrame()
    sub["saldo_pasivo"] = sub["credit"].fillna(0) - sub["debit"].fillna(0)
    sub["grupo_pasivo"] = sub["account_code"].str[:2]
    grp = sub.groupby(
        ["partner_id", "grupo_pasivo"], as_index=False,
    )["saldo_pasivo"].sum()
    grp = grp[grp["saldo_pasivo"] > 0]
    if grp.empty:
        return pd.DataFrame()
    return _enrich_with_partner(grp, partners, "saldo_pasivo", "saldo_pasivo")


# ===========================================================================
# FORMATO 1056 — Devoluciones, anulaciones, rescisiones
# ===========================================================================


def build_formato_1056(
    moves: pd.DataFrame,
    chart: pd.DataFrame,
    partners: pd.DataFrame,
    year: int,
) -> pd.DataFrame:
    """
    Devoluciones en ventas (notas crédito) y compras (notas crédito de
    proveedor) del año, por tercero.

    Identificación: movimientos a cuentas 4175 (Devoluciones en ventas) y
    6225 (Devoluciones en compras), o cualquier débito a 4xxx (NC ventas).
    """
    if moves is None or moves.empty:
        return pd.DataFrame()
    df = moves.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df[df["date"].dt.year == year]
    if df.empty:
        return pd.DataFrame()
    df = _enrich_moves_with_puc(df, chart)

    # Devoluciones en ventas: cuentas 4175 o débitos a ingresos (puc_group="4")
    mask_dev_v = df["account_code"].str.startswith("4175") | (
        (df["puc_group"] == "4") & (df["debit"] > 0)
    )
    # Devoluciones en compras: cuentas 6225 o créditos a costos (puc_group="6")
    mask_dev_c = df["account_code"].str.startswith("6225") | (
        (df["puc_group"] == "6") & (df["credit"] > 0)
    )

    devoluciones_ventas = df[mask_dev_v & (df["partner_id"].notna())].copy()
    devoluciones_ventas["devolucion_venta"] = devoluciones_ventas["debit"].fillna(0)

    devoluciones_compras = df[mask_dev_c & (df["partner_id"].notna())].copy()
    devoluciones_compras["devolucion_compra"] = devoluciones_compras["credit"].fillna(0)

    rows_v = devoluciones_ventas.groupby("partner_id", as_index=False)[
        "devolucion_venta"
    ].sum()
    rows_c = devoluciones_compras.groupby("partner_id", as_index=False)[
        "devolucion_compra"
    ].sum()
    out = rows_v.merge(rows_c, on="partner_id", how="outer").fillna(0)
    out = out[(out["devolucion_venta"] > 0) | (out["devolucion_compra"] > 0)]
    if out.empty:
        return pd.DataFrame()

    # Enriquecer con info de tercero
    if partners is not None and not partners.empty:
        partner_info = partners.set_index("id")
        out_rows = []
        for _, r in out.iterrows():
            pid = r["partner_id"]
            try:
                p = partner_info.loc[int(pid)].to_dict() if pid in partner_info.index else {}
            except Exception:  # noqa: BLE001
                p = {}
            t = _row_tercero(p)
            out_rows.append({
                **t,
                "devolucion_venta": round(float(r["devolucion_venta"]), 0),
                "devolucion_compra": round(float(r["devolucion_compra"]), 0),
            })
        return pd.DataFrame(out_rows)
    return out


# ===========================================================================
# FORMATO 1647 — Ingresos recibidos para terceros
# ===========================================================================


def build_formato_1647(
    moves: pd.DataFrame,
    chart: pd.DataFrame,
    partners: pd.DataFrame,
    year: int,
) -> pd.DataFrame:
    """
    Ingresos que la empresa recibe POR CUENTA de terceros (no son ingresos
    propios). Cuenta típica: 2815 Ingresos recibidos para terceros.
    """
    if moves is None or moves.empty:
        return pd.DataFrame()
    df = moves.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df[df["date"].dt.year == year]
    if df.empty:
        return pd.DataFrame()
    df = _enrich_moves_with_puc(df, chart)

    mask = df["account_code"].str.startswith("2815")
    sub = df[mask & (df["partner_id"].notna())].copy()
    if sub.empty:
        return pd.DataFrame()
    sub["ingreso_terceros"] = sub["credit"].fillna(0) - sub["debit"].fillna(0)
    grp = sub.groupby("partner_id", as_index=False)["ingreso_terceros"].sum()
    grp = grp[grp["ingreso_terceros"] > 0]
    if grp.empty:
        return pd.DataFrame()
    return _enrich_with_partner(grp, partners, "ingreso_terceros", "ingreso_recibido_terceros")


# ===========================================================================
# FORMATO 2275 — Costos y deducciones (resolución reciente DIAN)
# ===========================================================================


def build_formato_2275(
    moves: pd.DataFrame,
    chart: pd.DataFrame,
    partners: pd.DataFrame,
    year: int,
) -> pd.DataFrame:
    """
    Costos y deducciones por tercero — formato más reciente y detallado
    que 1001. Toma cuentas 5xxx, 6xxx, 7xxx con tercero y agrupa por
    (partner, código contable).

    Diferencia con 1001: este reporta el NIVEL DE CUENTA específico
    (account_code), no el concepto DIAN.
    """
    if moves is None or moves.empty:
        return pd.DataFrame()
    df = moves.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df[df["date"].dt.year == year]
    if df.empty:
        return pd.DataFrame()
    df = _enrich_moves_with_puc(df, chart)

    # Costos y deducciones: puc_group 5 (gastos), 6 (costos), 7 (costos producción)
    mask = df["puc_group"].isin(["5", "6", "7"])
    sub = df[mask & (df["partner_id"].notna())].copy()
    if sub.empty:
        return pd.DataFrame()
    sub["monto"] = sub["debit"].fillna(0) - sub["credit"].fillna(0)
    grp = sub.groupby(
        ["partner_id", "account_code", "account_name"], as_index=False,
    )["monto"].sum()
    grp = grp[grp["monto"].abs() >= 1]
    if grp.empty:
        return pd.DataFrame()
    return _enrich_with_partner(grp, partners, "monto", "valor_costo_deduccion")


# ===========================================================================
# FORMATO 2276 — Pagos laborales (rentas de trabajo)
# ===========================================================================


def build_formato_2276(
    moves: pd.DataFrame,
    chart: pd.DataFrame,
    partners: pd.DataFrame,
    year: int,
) -> pd.DataFrame:
    """
    Pagos laborales detallados por empleado. Toma cuentas 5105 (Gastos
    de personal), 7105 (Costos de personal), 2510 (Cesantías), etc.

    NOTA: en Odoo los empleados no siempre están en res.partner. Si el
    cliente usa el módulo HR, los datos están en hr.payslip — pero este
    informe usa el movimiento contable como fuente.
    """
    if moves is None or moves.empty:
        return pd.DataFrame()
    df = moves.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df[df["date"].dt.year == year]
    if df.empty:
        return pd.DataFrame()
    df = _enrich_moves_with_puc(df, chart)

    # Cuentas laborales: 5105, 5108, 5110-laborales, 7105, 2510-2530
    mask_lab = df["account_code"].str.startswith((
        "5105", "5108", "5106", "7105", "7108",
        "2510", "2515", "2520", "2525", "2530",
    ))
    sub = df[mask_lab & (df["partner_id"].notna())].copy()
    if sub.empty:
        return pd.DataFrame()
    sub["monto_laboral"] = sub["debit"].fillna(0) - sub["credit"].fillna(0)

    # Clasificar tipo: salario, prestaciones, seguridad social
    def _tipo_laboral(code: str) -> str:
        if code.startswith(("5105", "7105")):
            return "Salarios"
        if code.startswith(("5106", "7106", "2510")):
            return "Cesantías e intereses"
        if code.startswith(("5108", "7108", "2515", "2520")):
            return "Prestaciones sociales"
        if code.startswith(("2525", "2530")):
            return "Aportes seguridad social"
        return "Otros laborales"

    sub["tipo_pago"] = sub["account_code"].apply(_tipo_laboral)
    grp = sub.groupby(
        ["partner_id", "tipo_pago"], as_index=False,
    )["monto_laboral"].sum()
    grp = grp[grp["monto_laboral"] > 0]
    if grp.empty:
        return pd.DataFrame()
    return _enrich_with_partner(grp, partners, "monto_laboral", "valor_pagado")


# ===========================================================================
# Generación del Excel multi-hoja con todos los formatos
# ===========================================================================


def generar_excel_medios_magneticos(
    formatos: dict[str, pd.DataFrame],
    output_path: str,
    year: int,
) -> None:
    """
    Escribe todos los formatos en un Excel multi-hoja.

    Args:
        formatos: dict {nombre_formato: DataFrame}
        output_path: ruta del archivo de salida
        year: año fiscal reportado
    """
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        # Hoja de resumen
        resumen = pd.DataFrame([
            {"Formato": k, "Filas": len(v), "Estado": (
                "OK" if not v.empty else "VACÍO"
            )}
            for k, v in formatos.items()
        ])
        resumen.to_excel(writer, sheet_name="00_Resumen", index=False)

        # Una hoja por formato
        for nombre, df in formatos.items():
            if df is None or df.empty:
                # Hoja vacía con encabezado
                pd.DataFrame({"Sin datos": [f"Año {year}"]}).to_excel(
                    writer, sheet_name=nombre[:31], index=False,
                )
            else:
                df.to_excel(writer, sheet_name=nombre[:31], index=False)
