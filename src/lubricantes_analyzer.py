# -*- coding: utf-8 -*-
"""
Analizador de la LÍNEA DE LUBRICANTES (mayoreo, vendedores externos
puerta a puerta).

Trabaja sobre las líneas de factura (`account.move.line` con producto) ya
descargadas por `data_loader.load_invoice_lines`, filtrando a las categorías
cuyo nombre de hoja empieza con "lubricantes" (p.ej. "CMIN / LUBRICANTES
INCOLMOTOS", "CMIN / LUBRICANTES EDUARDOÑO").

Todas las funciones son pandas puro (sin Streamlit ni Odoo), para poder
probarlas de forma aislada. El volumen físico se calcula como
`quantity × product.volume × signo` — para Casa de los Mineros el campo
`volume` está en galones.

Dimensiones que se cruzan:
  - Vendedor  = `res.partner.user_id` (comercial asignado al cliente).
  - Ciudad / departamento = `res.partner.city` / `state_name`.
  - Referencia = producto (product_id / product_name / código).
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

SIN_ASIGNAR = "Sin vendedor"
SIN_CIUDAD = "Sin ciudad"


# ---------------------------------------------------------------------------
# 1) Filtrado y enriquecimiento
# ---------------------------------------------------------------------------
def filtrar_lubricantes(lines: pd.DataFrame) -> pd.DataFrame:
    """Deja solo líneas cuya categoría (hoja) empieza con 'lubricantes'."""
    if lines is None or lines.empty or "product_categ_name" not in lines.columns:
        return pd.DataFrame(columns=lines.columns if lines is not None else None)
    cat = lines["product_categ_name"].fillna("").astype(str)
    hoja = cat.str.split("/").str[-1].str.strip().str.lower()
    mask = hoja.str.startswith("lubricantes")
    return lines[mask].copy()


def enriquecer(
    lub: pd.DataFrame,
    partners: pd.DataFrame | None,
) -> pd.DataFrame:
    """
    Agrega ventas, volumen, costo/margen y datos del cliente (ciudad,
    departamento, vendedor) a las líneas de lubricantes.
    """
    if lub is None or lub.empty:
        return pd.DataFrame()
    df = lub.copy()

    sign = df.get("move_type", pd.Series(index=df.index)).map(
        {"out_invoice": 1, "out_refund": -1}
    ).fillna(1)
    # `quantity_base` = cantidad convertida a unidades base (respeta embalajes
    # como "Caja x 24"). Si no viene, se usa la cantidad de la línea.
    if "quantity_base" in df.columns:
        qty = pd.to_numeric(df["quantity_base"], errors="coerce").fillna(0)
    else:
        qty = pd.to_numeric(df.get("quantity", 0), errors="coerce").fillna(0)
    vol_unit = pd.to_numeric(df.get("product_volume", 0), errors="coerce").fillna(0)

    df["ventas"] = pd.to_numeric(
        df.get("price_subtotal_signed", 0), errors="coerce"
    ).fillna(0)
    df["volumen"] = qty * vol_unit * sign
    df["cantidad"] = qty * sign
    df["costo"] = pd.to_numeric(df.get("line_cost", 0), errors="coerce").fillna(0)
    df["margen"] = df["ventas"] - df["costo"]

    # Fecha
    if "invoice_date" in df.columns:
        df["fecha"] = pd.to_datetime(df["invoice_date"], errors="coerce")
    elif "date" in df.columns:
        df["fecha"] = pd.to_datetime(df["date"], errors="coerce")
    else:
        df["fecha"] = pd.NaT

    # Datos del cliente (ciudad / departamento / vendedor)
    if partners is not None and not partners.empty and "id" in partners.columns:
        cols = [c for c in ["id", "city", "state_name", "user_id", "user_name"]
                if c in partners.columns]
        geo = partners[cols].rename(columns={"id": "partner_id"}).drop_duplicates(
            "partner_id"
        )
        df = df.merge(geo, on="partner_id", how="left")

    df["ciudad"] = (
        df.get("city", pd.Series(index=df.index)).fillna(SIN_CIUDAD)
        .replace("", SIN_CIUDAD)
    )
    if "state_name" not in df.columns:
        df["state_name"] = ""
    df["departamento"] = df["state_name"].fillna("").replace("", "—")
    if "user_name" not in df.columns:
        df["user_name"] = None
    df["vendedor"] = df["user_name"].fillna(SIN_ASIGNAR).replace("", SIN_ASIGNAR)
    return df


def filtrar_fechas(
    df: pd.DataFrame,
    date_from: date | pd.Timestamp | None,
    date_to: date | pd.Timestamp | None,
) -> pd.DataFrame:
    if df is None or df.empty or "fecha" not in df.columns:
        return df
    out = df.dropna(subset=["fecha"])
    if date_from is not None:
        out = out[out["fecha"] >= pd.Timestamp(date_from)]
    if date_to is not None:
        out = out[out["fecha"] <= pd.Timestamp(date_to)]
    return out.copy()


# ---------------------------------------------------------------------------
# 2) KPIs y agregados
# ---------------------------------------------------------------------------
def _pct(a, b):
    return (a / b * 100.0) if b else 0.0


def kpis_generales(df: pd.DataFrame) -> dict:
    if df is None or df.empty:
        return dict(ventas=0.0, volumen=0.0, costo=0.0, margen=0.0,
                   margen_pct=0.0, n_clientes=0, n_referencias=0,
                   n_facturas=0, ticket=0.0)
    ventas = float(df["ventas"].sum())
    costo = float(df["costo"].sum())
    margen = ventas - costo
    n_fac = int(df.loc[df["move_type"] == "out_invoice", "move_id"].nunique()) \
        if "move_id" in df.columns else 0
    return dict(
        ventas=ventas,
        volumen=float(df["volumen"].sum()),
        costo=costo,
        margen=margen,
        margen_pct=_pct(margen, ventas),
        n_clientes=int(df["partner_id"].nunique()),
        n_referencias=int(df["product_id"].nunique()) if "product_id" in df.columns else 0,
        n_facturas=n_fac,
        ticket=(ventas / n_fac) if n_fac else 0.0,
    )


def _agg_group(df: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    g = df.groupby(keys, dropna=False)
    is_fac = df["move_type"] == "out_invoice"
    res = pd.DataFrame({
        "ventas": g["ventas"].sum(),
        "volumen": g["volumen"].sum(),
        "costo": g["costo"].sum(),
        "n_clientes": g["partner_id"].nunique(),
        "n_referencias": g["product_id"].nunique() if "product_id" in df.columns else 0,
        "n_facturas": df.loc[is_fac].groupby(keys)["move_id"].nunique(),
    })
    res = res.fillna(0)
    res["margen"] = res["ventas"] - res["costo"]
    res["margen_pct"] = np.where(
        res["ventas"] != 0, res["margen"] / res["ventas"].replace(0, np.nan) * 100.0, 0.0
    )
    total = float(res["ventas"].sum())
    res["participacion_pct"] = res["ventas"] / total * 100.0 if total else 0.0
    res["n_facturas"] = res["n_facturas"].fillna(0).astype(int)
    return res.reset_index().sort_values("ventas", ascending=False).reset_index(drop=True)


def por_categoria(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    return _agg_group(df, ["product_categ_name"]).rename(
        columns={"product_categ_name": "categoria"}
    )


def por_producto(df: pd.DataFrame, top: int | None = None) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    keys = ["product_name"] if "product_name" in df.columns else ["product_id"]
    res = _agg_group(df, keys)
    res = res.rename(columns={keys[0]: "producto"}).sort_values(
        "volumen", ascending=False
    ).reset_index(drop=True)
    return res.head(top) if top else res


def por_vendedor(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    res = _agg_group(df, ["vendedor"])
    res["ticket"] = np.where(
        res["n_facturas"] > 0, res["ventas"] / res["n_facturas"].replace(0, np.nan), 0.0
    )
    return res


def por_ciudad(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    return _agg_group(df, ["departamento", "ciudad"])


def zonificacion(df: pd.DataFrame, valor: str = "ventas") -> pd.DataFrame:
    """Matriz ciudad × vendedor (pivote) del valor indicado (ventas/volumen)."""
    if df is None or df.empty:
        return pd.DataFrame()
    piv = pd.pivot_table(
        df, index="ciudad", columns="vendedor", values=valor,
        aggfunc="sum", fill_value=0.0, margins=True, margins_name="TOTAL",
    )
    return piv.sort_values("TOTAL", ascending=False)


# ---------------------------------------------------------------------------
# 3) Oportunidades y recomendaciones
# ---------------------------------------------------------------------------
def resumen_clientes(df: pd.DataFrame) -> pd.DataFrame:
    """Una fila por cliente: ventas, volumen, # referencias, última compra."""
    if df is None or df.empty:
        return pd.DataFrame()
    g = df.groupby("partner_id", dropna=False)
    res = pd.DataFrame({
        "partner_name": g["partner_name"].first() if "partner_name" in df.columns else "",
        "ciudad": g["ciudad"].first(),
        "vendedor": g["vendedor"].first(),
        "ventas": g["ventas"].sum(),
        "volumen": g["volumen"].sum(),
        "n_referencias": g["product_id"].nunique() if "product_id" in df.columns else 0,
        "ultima_compra": g["fecha"].max(),
        "n_facturas": df[df["move_type"] == "out_invoice"].groupby("partner_id")["move_id"].nunique(),
    }).reset_index()
    res["n_facturas"] = res["n_facturas"].fillna(0).astype(int)
    return res.sort_values("ventas", ascending=False).reset_index(drop=True)


def oportunidades_referencias(df: pd.DataFrame) -> pd.DataFrame:
    """
    Clientes con POTENCIAL de comprar más referencias: compran volumen por
    encima de la mediana pero manejan pocas referencias (por debajo de la
    mediana). Son quienes concentran en pocos SKUs y pueden ampliar.
    """
    rc = resumen_clientes(df)
    if rc.empty:
        return rc
    rc = rc[rc["ventas"] > 0]
    if rc.empty:
        return rc
    med_ref = rc["n_referencias"].median()
    med_vol = rc["volumen"].median()
    opp = rc[(rc["n_referencias"] <= med_ref) & (rc["volumen"] >= med_vol)].copy()
    opp["ref_potencial"] = (med_ref - opp["n_referencias"]).clip(lower=0).round(0).astype(int) + 1
    return opp.sort_values("ventas", ascending=False).reset_index(drop=True)


def cross_sell(df: pd.DataFrame, top_refs: int = 15, sugerencias: int = 3) -> pd.DataFrame:
    """
    Para cada cliente, sugiere referencias populares (compradas por muchos
    clientes) que ese cliente NO compra. Market-basket simplificado.
    """
    if df is None or df.empty or "product_id" not in df.columns:
        return pd.DataFrame()
    # Popularidad = # clientes distintos que compran cada referencia.
    pop = (
        df.groupby(["product_id"])
        .agg(producto=("product_name", "first") if "product_name" in df.columns else ("product_id", "first"),
             n_clientes=("partner_id", "nunique"),
             volumen=("volumen", "sum"))
        .reset_index()
        .sort_values("n_clientes", ascending=False)
    )
    top = pop.head(top_refs)
    top_ids = list(top["product_id"])

    # Referencias que ya compra cada cliente
    compradas = df.groupby("partner_id")["product_id"].agg(set).to_dict()
    rc = resumen_clientes(df)
    rc = rc[rc["ventas"] > 0]

    filas = []
    prod_name = dict(zip(top["product_id"], top["producto"]))
    prod_pop = dict(zip(top["product_id"], top["n_clientes"]))
    for _, cli in rc.iterrows():
        pid = cli["partner_id"]
        ya = compradas.get(pid, set())
        faltantes = [p for p in top_ids if p not in ya]
        for p in faltantes[:sugerencias]:
            filas.append({
                "partner_id": pid,
                "partner_name": cli["partner_name"],
                "ciudad": cli["ciudad"],
                "vendedor": cli["vendedor"],
                "ventas_cliente": cli["ventas"],
                "referencia_sugerida": prod_name.get(p, str(p)),
                "clientes_que_la_compran": prod_pop.get(p, 0),
            })
    out = pd.DataFrame(filas)
    if out.empty:
        return out
    return out.sort_values(
        ["ventas_cliente", "clientes_que_la_compran"], ascending=[False, False]
    ).reset_index(drop=True)


def clientes_inactivos(
    df: pd.DataFrame,
    cutoff: date | pd.Timestamp,
    min_days: int = 45,
) -> pd.DataFrame:
    """Clientes de lubricantes que no compran desde hace >= min_days."""
    rc = resumen_clientes(df)
    if rc.empty:
        return rc
    cutoff_ts = pd.Timestamp(cutoff)
    rc = rc.dropna(subset=["ultima_compra"])
    rc["dias_sin_comprar"] = (cutoff_ts - rc["ultima_compra"]).dt.days
    inact = rc[rc["dias_sin_comprar"] >= min_days].copy()
    return inact.sort_values("ventas", ascending=False).reset_index(drop=True)


def ciudades_baja_cobertura(df: pd.DataFrame, min_clientes: int = 3) -> pd.DataFrame:
    """
    Ciudades con pocos clientes pero ticket/volumen alto por cliente:
    candidatas a sumar más clientes (baja cobertura, buen potencial).
    """
    ciu = por_ciudad(df)
    if ciu.empty:
        return ciu
    ciu = ciu.copy()
    ciu["venta_x_cliente"] = np.where(
        ciu["n_clientes"] > 0, ciu["ventas"] / ciu["n_clientes"], 0.0
    )
    med_vxc = ciu["venta_x_cliente"].median()
    cand = ciu[(ciu["n_clientes"] <= min_clientes) & (ciu["venta_x_cliente"] >= med_vxc)]
    return cand.sort_values("venta_x_cliente", ascending=False).reset_index(drop=True)
