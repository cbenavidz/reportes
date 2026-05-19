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


def _infer_tipo_doc(partner: dict) -> str:
    """Infiere el tipo de documento DIAN desde info del partner."""
    is_company = partner.get("is_company", False)
    vat = (partner.get("vat") or "").strip()
    if is_company:
        return "31"  # NIT
    if vat:
        # Si tiene VAT pero no es empresa, probablemente cédula
        if len(vat.replace("-", "").replace(".", "")) >= 10:
            return "13"  # CC
        return "13"
    return "13"  # Default


def _clean_vat(vat: str) -> tuple[str, str]:
    """
    Limpia un NIT/cédula. Devuelve (número_sin_dv, dv).
    Si tiene formato '900123456-7' separa el DV.
    """
    if not vat:
        return "", ""
    v = str(vat).replace(".", "").replace(" ", "").strip()
    if "-" in v:
        parts = v.split("-")
        return parts[0], parts[1] if len(parts) > 1 else ""
    return v, ""


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
    numero_doc, dv = _clean_vat(partner.get("vat", ""))
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
) -> pd.DataFrame:
    """
    Construye Formato 1001: pagos a terceros por concepto.

    Toma movimientos de cuentas 5xxx (gastos) y 6xxx (costos), agrupados
    por (tercero, concepto). El "pago" es el débito a esas cuentas.

    Retenciones: suma de movimientos a cuentas 2365 (Retención en la
    fuente) y 2367 (IVA retenido) del mismo período por cada tercero.
    """
    if moves is None or moves.empty:
        return pd.DataFrame()

    df = moves.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df[df["date"].dt.year == year]
    if df.empty:
        return pd.DataFrame()

    # Join con plan de cuentas para obtener código
    if "account_id" in df.columns and chart is not None and "id" in chart.columns:
        chart_min = chart[["id", "code", "name"]].rename(
            columns={"id": "account_id", "code": "account_code", "name": "account_name"}
        )
        df = df.merge(chart_min, on="account_id", how="left")

    df["account_code"] = df.get("account_code", "").astype(str)

    # Filtrar pagos a terceros: gastos (5xxx, 6xxx) y compras (143x)
    pagos_mask = df["account_code"].str.startswith(("5", "6")) | (
        df["account_code"].str.startswith("143")
    )
    pagos = df[pagos_mask & (df["partner_id"].notna())].copy()
    if pagos.empty:
        return pd.DataFrame()

    pagos["monto"] = pagos["debit"].fillna(0) - pagos["credit"].fillna(0)
    pagos["concepto"] = pagos["account_code"].apply(
        lambda c: _get_concepto(c, CONCEPTOS_1001_POR_PUC)
    )

    # Retenciones por tercero (cuentas 2365 retención renta, 2367 reteIVA, 2368 reteICA)
    ret_mask = df["account_code"].str.startswith(("2365", "2367", "2368"))
    rets = df[ret_mask & (df["partner_id"].notna())].copy()
    rets["monto_ret"] = rets["credit"].fillna(0) - rets["debit"].fillna(0)
    rets_by_partner = rets.groupby(
        ["partner_id", df.loc[rets.index, "account_code"].str[:4]],
        as_index=False,
    )["monto_ret"].sum()
    # Construir tabla pivotada de retenciones por tercero
    if not rets_by_partner.empty:
        rets_pivot = rets_by_partner.pivot_table(
            index="partner_id", columns="account_code",
            values="monto_ret", aggfunc="sum", fill_value=0,
        )
        rets_pivot = rets_pivot.rename(columns={
            "2365": "ret_fuente_renta",
            "2367": "ret_iva",
            "2368": "ret_ica",
        })
    else:
        rets_pivot = pd.DataFrame()

    # Agrupar pagos por tercero + concepto
    grp = pagos.groupby(["partner_id", "concepto"], as_index=False).agg(
        pago_deducible=("monto", "sum"),
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
                "concepto": r["concepto"],
                "pago_deducible": round(float(r["pago_deducible"]), 0),
                "pago_no_deducible": 0,
                "ret_fuente_renta": 0,
                "ret_iva": 0,
                "ret_ica": 0,
            }
            # Sumar retenciones del tercero
            if not rets_pivot.empty and pid in rets_pivot.index:
                for col in ("ret_fuente_renta", "ret_iva", "ret_ica"):
                    if col in rets_pivot.columns:
                        row[col] = round(float(rets_pivot.loc[pid, col] or 0), 0)
            out_rows.append(row)
        out = pd.DataFrame(out_rows)
    else:
        out = grp

    # Filtrar montos < $100k (umbral DIAN típico — el real es ~$1M pero
    # esto se ajusta según resolución del año)
    if "pago_deducible" in out.columns:
        out = out[out["pago_deducible"].abs() >= 100_000].reset_index(drop=True)
    return out


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

    if "account_id" in df.columns and chart is not None and "id" in chart.columns:
        chart_min = chart[["id", "code"]].rename(
            columns={"id": "account_id", "code": "account_code"},
        )
        df = df.merge(chart_min, on="account_id", how="left")
    df["account_code"] = df.get("account_code", "").astype(str)

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
    if "account_id" in df.columns and chart is not None and "id" in chart.columns:
        chart_min = chart[["id", "code"]].rename(
            columns={"id": "account_id", "code": "account_code"},
        )
        df = df.merge(chart_min, on="account_id", how="left")
    df["account_code"] = df.get("account_code", "").astype(str)

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
    if "account_id" in df.columns and chart is not None and "id" in chart.columns:
        chart_min = chart[["id", "code"]].rename(
            columns={"id": "account_id", "code": "account_code"},
        )
        df = df.merge(chart_min, on="account_id", how="left")
    df["account_code"] = df.get("account_code", "").astype(str)

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
    if "account_id" in df.columns and chart is not None and "id" in chart.columns:
        chart_min = chart[["id", "code"]].rename(
            columns={"id": "account_id", "code": "account_code"},
        )
        df = df.merge(chart_min, on="account_id", how="left")
    df["account_code"] = df.get("account_code", "").astype(str)

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
    Ingresos por tercero (cliente). Suma los créditos a cuentas 4xxx
    del año fiscal, agrupado por (tercero, concepto).
    """
    if moves is None or moves.empty:
        return pd.DataFrame()
    df = moves.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df[df["date"].dt.year == year]
    if df.empty:
        return pd.DataFrame()
    if "account_id" in df.columns and chart is not None and "id" in chart.columns:
        chart_min = chart[["id", "code"]].rename(
            columns={"id": "account_id", "code": "account_code"},
        )
        df = df.merge(chart_min, on="account_id", how="left")
    df["account_code"] = df.get("account_code", "").astype(str)

    mask = df["account_code"].str.startswith("4")
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
    Saldo de cuentas por cobrar (1305, 1330, 1355, etc.) al 31 dic del año.
    """
    if moves is None or moves.empty:
        return pd.DataFrame()
    df = moves.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df[df["date"].dt.date <= date(year, 12, 31)]
    if df.empty:
        return pd.DataFrame()
    if "account_id" in df.columns and chart is not None and "id" in chart.columns:
        chart_min = chart[["id", "code"]].rename(
            columns={"id": "account_id", "code": "account_code"},
        )
        df = df.merge(chart_min, on="account_id", how="left")
    df["account_code"] = df.get("account_code", "").astype(str)

    # CxC: 1305 deudores nacionales, 1310 cliente nacionales, 1325 cuentas por cobrar
    mask = df["account_code"].str.startswith(("1305", "1310", "1325", "1330"))
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
    """Saldos de CxP (22xx, 2335, etc.) al 31 dic."""
    if moves is None or moves.empty:
        return pd.DataFrame()
    df = moves.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df[df["date"].dt.date <= date(year, 12, 31)]
    if df.empty:
        return pd.DataFrame()
    if "account_id" in df.columns and chart is not None and "id" in chart.columns:
        chart_min = chart[["id", "code"]].rename(
            columns={"id": "account_id", "code": "account_code"},
        )
        df = df.merge(chart_min, on="account_id", how="left")
    df["account_code"] = df.get("account_code", "").astype(str)

    # CxP: 22xx (proveedores), 2335 (otros costos)
    mask = df["account_code"].str.startswith(("22", "2335", "2380"))
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
    if "account_id" in df.columns and chart is not None and "id" in chart.columns:
        chart_min = chart[["id", "code"]].rename(
            columns={"id": "account_id", "code": "account_code"},
        )
        df = df.merge(chart_min, on="account_id", how="left")
    df["account_code"] = df.get("account_code", "").astype(str)

    def _suma(prefix: str | tuple) -> float:
        m = df["account_code"].str.startswith(prefix)
        sub = df[m]
        return float(sub["credit"].sum() - sub["debit"].sum())

    rows = [
        ("Concepto", "Valor"),
        ("Ingresos brutos operacionales (41)", _suma("41")),
        ("Ingresos no operacionales (42)", _suma("42")),
        ("Costo de ventas (6)", -_suma("6")),  # invertir signo (es débito)
        ("Gastos administración (51)", -_suma("51")),
        ("Gastos ventas (52)", -_suma("52")),
        ("Gastos no operacionales (53)", -_suma("53")),
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
    if "account_id" in df.columns and chart is not None and "id" in chart.columns:
        chart_min = chart[["id", "code", "name"]].rename(
            columns={"id": "account_id", "code": "account_code", "name": "account_name"},
        )
        df = df.merge(chart_min, on="account_id", how="left")
    df["account_code"] = df.get("account_code", "").astype(str)

    # 11xx Disponible, 12xx Inversiones
    mask = df["account_code"].str.startswith(("11", "12"))
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
    if "account_id" in df.columns and chart is not None and "id" in chart.columns:
        chart_min = chart[["id", "code", "name"]].rename(
            columns={"id": "account_id", "code": "account_code", "name": "account_name"},
        )
        df = df.merge(chart_min, on="account_id", how="left")
    df["account_code"] = df.get("account_code", "").astype(str)

    # Pasivos: 21, 22, 23, 24, 25
    mask = df["account_code"].str.startswith(("21", "22", "23", "24", "25"))
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
    if "account_id" in df.columns and chart is not None and "id" in chart.columns:
        chart_min = chart[["id", "code"]].rename(
            columns={"id": "account_id", "code": "account_code"},
        )
        df = df.merge(chart_min, on="account_id", how="left")
    df["account_code"] = df.get("account_code", "").astype(str)

    # Devoluciones en ventas: cuentas 4175 o débitos a ingresos 4xxx
    mask_dev_v = df["account_code"].str.startswith("4175") | (
        df["account_code"].str.startswith("4") & (df["debit"] > 0)
    )
    # Devoluciones en compras: cuentas 6225 o créditos a costos 6xxx
    mask_dev_c = df["account_code"].str.startswith("6225") | (
        df["account_code"].str.startswith("6") & (df["credit"] > 0)
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
    if "account_id" in df.columns and chart is not None and "id" in chart.columns:
        chart_min = chart[["id", "code"]].rename(
            columns={"id": "account_id", "code": "account_code"},
        )
        df = df.merge(chart_min, on="account_id", how="left")
    df["account_code"] = df.get("account_code", "").astype(str)

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
    if "account_id" in df.columns and chart is not None and "id" in chart.columns:
        chart_min = chart[["id", "code", "name"]].rename(
            columns={"id": "account_id", "code": "account_code", "name": "account_name"},
        )
        df = df.merge(chart_min, on="account_id", how="left")
    df["account_code"] = df.get("account_code", "").astype(str)

    mask = df["account_code"].str.startswith(("5", "6", "7"))
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
    if "account_id" in df.columns and chart is not None and "id" in chart.columns:
        chart_min = chart[["id", "code", "name"]].rename(
            columns={"id": "account_id", "code": "account_code", "name": "account_name"},
        )
        df = df.merge(chart_min, on="account_id", how="left")
    df["account_code"] = df.get("account_code", "").astype(str)

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
