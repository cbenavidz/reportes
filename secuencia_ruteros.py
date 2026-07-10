# -*- coding: utf-8 -*-
"""
Recalcula el ORDEN DE VISITA (secuencia) de cada rutero tal como está HOY en
Odoo, sin mover clientes de día ni de rutero. Solo optimiza la secuencia
dentro de cada rutero por vecino más cercano (ruta más corta), arrancando por
el cliente más al oeste para que el recorrido lea de oeste a este.

Los clientes sin GPS de un rutero se dejan al final (no se pueden ordenar).

USO (con el venv del proyecto activado):
    python secuencia_ruteros.py

Salida: `secuencia_ruteros.xlsx`
  - Una hoja por rutero con el nuevo orden.
  - Hoja «Importar Odoo» con .id y sr_route_sequence para cargar.
"""
from __future__ import annotations

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import numpy as np
import pandas as pd

from src.odoo_client import OdooClient
from src import route_module as rm
from src.rutero_planner import order_nearest_neighbor, haversine


def _dias_ruta(r) -> str:
    return ", ".join(rm.DAY_LABELS[c[4:]] for c in rm.DAY_COLS if r.get(c)) or "—"


def main():
    client = OdooClient.from_env()
    client.authenticate()
    print(f"✓ Conectado a Odoo: {client.credentials.url}\n")

    routes = rm.extract_sr_routes(client)
    partners = rm.extract_route_partners(client, solo_activos=True)
    if routes.empty:
        print("No se leyeron ruteros. Revisa permisos de sr.route.")
        return

    p = partners.copy()
    p["lat"] = pd.to_numeric(p["partner_latitude"], errors="coerce")
    p["lon"] = pd.to_numeric(p["partner_longitude"], errors="coerce")
    p["route_id"] = pd.to_numeric(p["sr_route_id"], errors="coerce")
    p["has_geo"] = p.get("sr_has_geo", False)
    p["has_geo"] = p["has_geo"].fillna(False).astype(bool)

    detalle_por_ruta = {}
    imp_rows = []
    print("=== Orden de visita recalculado ===")
    for _, r in routes.iterrows():
        rid = int(r["id"])
        cli = p[p["route_id"] == rid].copy()
        if cli.empty:
            continue
        con = cli[cli["has_geo"] & cli["lat"].notna() & cli["lon"].notna()].copy()
        sin = cli[~cli.index.isin(con.index)].copy()

        if not con.empty:
            lat = con["lat"].to_numpy(dtype=float)
            lon = con["lon"].to_numpy(dtype=float)
            start = int(np.argmin(lon))  # el más al oeste
            orden = order_nearest_neighbor(lat, lon, start=start)
            con = con.iloc[orden].reset_index(drop=True)
            # km del recorrido
            km = sum(haversine(con["lat"][i - 1], con["lon"][i - 1],
                               con["lat"][i], con["lon"][i])
                     for i in range(1, len(con)))
        else:
            km = 0.0

        con["secuencia"] = [(i + 1) * 10 for i in range(len(con))]
        # los sin GPS al final
        base = len(con) * 10
        sin = sin.reset_index(drop=True)
        sin["secuencia"] = [base + (i + 1) * 10 for i in range(len(sin))]

        full = pd.concat([con, sin], ignore_index=True)
        detalle_por_ruta[f"{r['name']}"] = (full, r, km, len(sin))
        for _, c in full.iterrows():
            imp_rows.append({".id": int(c["id"]),
                             "sr_route_sequence": int(c["secuencia"]),
                             "Cliente (ref)": c.get("name"),
                             "Rutero (ref)": r["name"]})

        print(f"\n▸ {r['name']} — {r['user_name']} [{_dias_ruta(r)}]  "
              f"{len(full)} clientes, {len(sin)} sin GPS, ~{km:.0f} km")
        print(full[["secuencia", "name", "city"]].head(12).to_string(index=False))

    # Excel
    salida = "secuencia_ruteros.xlsx"
    with pd.ExcelWriter(salida, engine="xlsxwriter") as xw:
        for nombre, (full, r, km, n_sin) in detalle_por_ruta.items():
            sh = "".join(ch for ch in nombre if ch.isalnum() or ch == " ")[:31]
            full[["secuencia", "name", "city"]].rename(
                columns={"secuencia": "Secuencia", "name": "Cliente", "city": "Ciudad"}
            ).to_excel(xw, sheet_name=sh or "Rutero", index=False)
        pd.DataFrame(imp_rows).to_excel(xw, sheet_name="Importar Odoo", index=False)
    print(f"\n✓ Excel generado: {salida}")
    print("Para cargar: Contactos → Importar. Mapea .id → Database ID y "
          "sr_route_sequence → Secuencia en rutero. Borra las columnas (ref).")


if __name__ == "__main__":
    main()
