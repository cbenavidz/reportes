# -*- coding: utf-8 -*-
"""
Diagnóstico del módulo de Ventas en Ruta (sales_route_mobile).

Confirma que el usuario de API puede leer sr.route, sr.visit y los campos
sr_* de res.partner, y muestra una muestra de los datos reales.

USO (con el venv activado):
    python diagnostico_ruta.py
    python diagnostico_ruta.py --dias 60     # visitas de los últimos 60 días
"""
from __future__ import annotations

import sys
from datetime import date, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import pandas as pd

from src.odoo_client import OdooClient
from src import route_module as rm

pd.set_option("display.max_columns", None)
pd.set_option("display.width", 220)


def main():
    dias = 30
    if "--dias" in sys.argv:
        i = sys.argv.index("--dias")
        if i + 1 < len(sys.argv):
            dias = int(sys.argv[i + 1])

    client = OdooClient.from_env()
    client.authenticate()
    print(f"✓ Conectado a Odoo: {client.credentials.url}\n")

    print("=== 1) ¿El módulo de ruta es legible por el usuario de API? ===")
    ok = rm.route_module_disponible(client)
    print(f"   sr.route legible: {'SÍ ✅' if ok else 'NO ❌ (revisar permisos)'}\n")

    print("=== 2) Ruteros (sr.route) ===")
    routes = rm.extract_sr_routes(client)
    print(f"   Ruteros activos: {len(routes)}")
    if not routes.empty:
        print(routes[["name", "user_name", "dias", "partner_count"]]
              .head(15).to_string(index=False))
    print()

    print("=== 3) Clientes con config de ruta (res.partner sr_*) ===")
    partners = rm.extract_route_partners(client)
    if partners.empty:
        print("   (sin datos o sin permisos)")
    else:
        con_geo = int(partners["sr_has_geo"].fillna(False).astype(bool).sum()) \
            if "sr_has_geo" in partners.columns else 0
        activos = int(partners["sr_active_in_route"].fillna(False).astype(bool).sum()) \
            if "sr_active_in_route" in partners.columns else 0
        print(f"   Clientes: {len(partners)} | con GPS válido: {con_geo} | "
              f"activos en ruta: {activos}")
        if "sr_visit_frequency" in partners.columns:
            print("   Frecuencia configurada:")
            print(partners["sr_visit_frequency"].value_counts().to_string())
        cols = [c for c in ["name", "city", "user_name", "sr_route_name",
                            "sr_route_sequence", "sr_visit_frequency",
                            "sr_has_geo", "sr_active_in_route"]
                if c in partners.columns]
        print("\n   Muestra:")
        print(partners[cols].head(10).to_string(index=False))
    print()

    print(f"=== 4) Visitas reales (sr.visit) — últimos {dias} días ===")
    hasta = date.today()
    desde = hasta - timedelta(days=dias)
    visits = rm.extract_sr_visits(client, desde, hasta)
    if visits.empty:
        print("   (sin visitas en el período o sin permisos)")
    else:
        efectivas = int(visits["is_effective"].fillna(False).astype(bool).sum())
        fuera = int(visits["outside_geofence"].fillna(False).astype(bool).sum())
        print(f"   Visitas: {len(visits)} | efectivas: {efectivas} | "
              f"fuera de geocerca: {fuera} | vendedores: "
              f"{visits['user_name'].nunique()}")
        cols = [c for c in ["fecha", "partner_name", "user_name", "route_name",
                            "distance_m", "is_effective", "outside_geofence",
                            "reason_name"] if c in visits.columns]
        print("\n   Muestra (últimas 10):")
        print(visits[cols].head(10).to_string(index=False))
    print("\n✓ Diagnóstico terminado.")


if __name__ == "__main__":
    main()
