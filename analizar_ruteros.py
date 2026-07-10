# -*- coding: utf-8 -*-
"""
Re-lee los ruteros de Odoo (después de tus cambios) y:

  1. Analiza cada rutero: días (incluye multi-día como Istmina martes+miércoles),
     nº de clientes, cuántos con GPS, ventas 12m y carga de visitas/mes.
  2. Detecta los clientes NUEVOS de Quibdó (activos en ruta, con GPS y sin
     rutero) y sugiere a cuál rutero de Quibdó agregarlos, por cercanía y
     balanceando la carga.
  3. Escribe `analisis_ruteros.xlsx` con todo y una hoja «Importar Odoo» para
     asignar esos clientes al rutero sugerido.

USO (con el venv del proyecto activado):
    python analizar_ruteros.py
    python analizar_ruteros.py --radio 30     # km alrededor de Quibdó
"""
from __future__ import annotations

import sys
from datetime import date, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import numpy as np
import pandas as pd

from src.odoo_client import OdooClient
from src.extractor import extract_companies, extract_invoice_lines
from src import route_module as rm
from src import rutero_optimizer as ro

QUIBDO = ro.CIUDAD_COORDS["QUIBDO"]


def _dias_ruta(r) -> str:
    return ", ".join(rm.DAY_LABELS[c[4:]] for c in rm.DAY_COLS if r.get(c)) or "—"


def main():
    radio = 25.0
    if "--radio" in sys.argv:
        radio = float(sys.argv[sys.argv.index("--radio") + 1])

    hoy = date.today()
    desde = hoy - timedelta(days=365)

    client = OdooClient.from_env()
    client.authenticate()
    print(f"✓ Conectado a Odoo: {client.credentials.url}\n")

    routes = rm.extract_sr_routes(client)
    partners = rm.extract_route_partners(client, solo_activos=True)
    companies = extract_companies(client)
    company_ids = None
    if not companies.empty:
        mask = companies["name"].str.lower().str.contains("casa de los mineros", na=False)
        row = companies[mask].iloc[0] if mask.any() else companies.iloc[0]
        company_ids = [int(row["id"])]
    lines = extract_invoice_lines(client, date_from=desde, date_to=hoy,
                                  company_ids=company_ids)

    # Frecuencia/carga por cliente (derivada de ventas + facturas/mes)
    met = ro.metricas_clientes(lines, meses=12)
    sug = ro.sugerir_frecuencias(met)
    carga_de = {int(r["partner_id"]): ro.visitas_mes(r["frecuencia_code"])
                for _, r in sug.iterrows()}
    ventas_de = dict(zip(met["partner_id"].astype(int), met["ventas"]))

    # Preparar clientes
    p = partners.copy()
    for c in ["sr_has_geo"]:
        p[c] = p[c].fillna(False).astype(bool) if c in p.columns else False
    p["lat"] = pd.to_numeric(p["partner_latitude"], errors="coerce")
    p["lon"] = pd.to_numeric(p["partner_longitude"], errors="coerce")
    p["route_id"] = pd.to_numeric(p["sr_route_id"], errors="coerce")
    p["carga"] = p["id"].map(carga_de).fillna(0.5)
    p["ventas"] = p["id"].map(ventas_de).fillna(0.0)

    # ---- 1) Análisis por rutero ----
    print("=== 1) Ruteros (después de tus cambios) ===")
    filas = []
    centroides = {}
    for _, r in routes.iterrows():
        rid = int(r["id"])
        cli = p[p["route_id"] == rid]
        con_gps = cli[cli["sr_has_geo"] & cli["lat"].notna()]
        lat_c = float(con_gps["lat"].mean()) if not con_gps.empty else np.nan
        lon_c = float(con_gps["lon"].mean()) if not con_gps.empty else np.nan
        centroides[rid] = (lat_c, lon_c)
        dist_q = (ro.haversine(lat_c, lon_c, *QUIBDO)
                  if not np.isnan(lat_c) else np.nan)
        filas.append({
            "route_id": rid, "rutero": r["name"], "vendedor": r["user_name"],
            "dias": _dias_ruta(r), "n_clientes": len(cli),
            "con_gps": len(con_gps), "sin_gps": len(cli) - len(con_gps),
            "ventas_12m": float(cli["ventas"].sum()),
            "carga_visitas_mes": round(float(cli["carga"].sum()), 1),
            "km_a_quibdo": round(dist_q, 1) if not np.isnan(dist_q) else None,
        })
    an = pd.DataFrame(filas)
    print(an[["rutero", "vendedor", "dias", "n_clientes", "con_gps",
              "carga_visitas_mes", "km_a_quibdo"]].to_string(index=False))

    multidia = an[an["dias"].str.contains(",")]
    vacios = an[an["n_clientes"] == 0]
    print()
    if not multidia.empty:
        print(f"  · Ruteros multi-día: {', '.join(multidia['rutero'])}")
    if not vacios.empty:
        print(f"  · Ruteros sin clientes: {', '.join(vacios['rutero'])}")

    # ---- 2) Clientes nuevos de Quibdó sin rutero ----
    print(f"\n=== 2) Clientes nuevos de Quibdó sin rutero (radio {radio:.0f} km) ===")
    activos_geo = p[p["sr_has_geo"] & p["lat"].notna() & p["lon"].notna()]
    sin_rutero = activos_geo[activos_geo["route_id"].isna()]
    sin_rutero = sin_rutero.assign(
        _dq=sin_rutero.apply(lambda r: ro.haversine(r["lat"], r["lon"], *QUIBDO), axis=1)
    )
    ciudad_q = sin_rutero["city"].fillna("").astype(str).str.lower().str.contains("quibd")
    nuevos = sin_rutero[(sin_rutero["_dq"] <= radio) | ciudad_q].copy()
    print(f"   Encontrados: {len(nuevos)}")

    asignados = pd.DataFrame()
    if not nuevos.empty:
        # Ruteros de Quibdó = centroide dentro del radio y con GPS
        rq = an[(an["km_a_quibdo"].notna()) & (an["km_a_quibdo"] <= radio)
                & (an["con_gps"] > 0)].copy()
        rq["lat_c"] = rq["route_id"].map(lambda i: centroides[i][0])
        rq["lon_c"] = rq["route_id"].map(lambda i: centroides[i][1])
        rq["route_name"] = rq["rutero"]
        rq["carga_actual"] = rq["carga_visitas_mes"]
        if rq.empty:
            print("   ⚠️ No hay ruteros de Quibdó con GPS para asignar.")
        else:
            asignados = ro.asignar_a_ruteros(
                nuevos.rename(columns={"id": "partner_id"}),
                rq[["route_id", "route_name", "lat_c", "lon_c", "carga_actual"]],
            )
            print("\n   Sugerencia de asignación:")
            print(asignados[["name", "city", "route_name_sugerido", "dist_km", "carga"]]
                  .to_string(index=False))
            # Secuencia: después del máximo actual de cada rutero
            maxseq = (p.dropna(subset=["route_id"]).groupby("route_id")["sr_route_sequence"]
                      .max().to_dict())
            seqs, cont = [], {}
            for _, r in asignados.iterrows():
                rid = r["route_id_sugerido"]
                base = int(maxseq.get(rid, 0) or 0)
                cont[rid] = cont.get(rid, 0) + 1
                seqs.append(base + cont[rid] * 10)
            asignados["sr_route_sequence"] = seqs

    # ---- 3) Excel ----
    salida = "analisis_ruteros.xlsx"
    with pd.ExcelWriter(salida, engine="xlsxwriter") as xw:
        an.drop(columns=["route_id"]).to_excel(xw, sheet_name="Ruteros", index=False)
        if not asignados.empty:
            asignados[["name", "city", "route_name_sugerido", "dist_km",
                       "carga", "ventas"]].rename(columns={
                "name": "Cliente", "city": "Ciudad",
                "route_name_sugerido": "Rutero sugerido", "dist_km": "Dist. (km)",
                "carga": "Visitas/mes", "ventas": "Ventas 12m"}
            ).to_excel(xw, sheet_name="Nuevos Quibdó", index=False)
            imp = asignados.assign(**{
                ".id": asignados["partner_id"].astype(int),
                "sr_route_id/.id": asignados["route_id_sugerido"].astype("Int64"),
            })[[".id", "sr_route_id/.id", "sr_route_sequence", "name",
                "route_name_sugerido"]].rename(columns={
                "name": "Cliente (ref)", "route_name_sugerido": "Rutero (ref)"})
            imp.to_excel(xw, sheet_name="Importar Odoo", index=False)
    print(f"\n✓ Excel generado: {salida}")


if __name__ == "__main__":
    main()
