# -*- coding: utf-8 -*-
"""
Lista los clientes que compran LUBRICANTES a los vendedores Felipe o Vanessa,
pero que NO tienen georreferencia (sin GPS) y NO están en ningún rutero.

Sirve para saber a quién falta geolocalizar y meter a ruta.

Criterios:
  - Tiene ventas de lubricantes (categoría que empieza con "lubricantes") en el
    período, facturadas por Felipe o Vanessa (invoice_user_id).
  - sr_has_geo = False  (sin coordenadas válidas).
  - Sin rutero: sr_route_id vacío Y sr_route_ids vacío.

USO (con el venv del proyecto activado):
    python clientes_lubricantes_sin_ruta.py
    python clientes_lubricantes_sin_ruta.py "Felipe" "Yarley" --meses 18

Salida: `lubricantes_sin_ruta.xlsx`
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
from src.extractor import extract_companies, extract_invoice_lines, extract_invoices
from src import route_module as rm
from src import lubricantes_analyzer as la

DEFAULT_SELLERS = ["felipe", "yarley", "vanessa", "vannesa"]


def _vacio_m2o(v) -> bool:
    """True si un campo Many2one viene vacío (False/None)."""
    return v is None or v is False or (isinstance(v, float) and pd.isna(v))


def _vacio_m2m(v) -> bool:
    """True si un campo Many2many viene vacío (lista vacía / False)."""
    if v is None or v is False:
        return True
    if isinstance(v, (list, tuple)):
        return len(v) == 0
    return False


def main():
    argv = [a for a in sys.argv[1:]]
    meses = 12
    if "--meses" in argv:
        i = argv.index("--meses")
        meses = int(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
    patrones = [a.lower() for a in argv] if argv else DEFAULT_SELLERS

    hoy = date.today()
    desde = hoy - timedelta(days=int(meses * 30.5))

    client = OdooClient.from_env()
    client.authenticate()
    print(f"✓ Conectado a Odoo: {client.credentials.url}")
    print(f"  Período: {desde} a {hoy} · Vendedores: {', '.join(patrones)}\n")

    companies = extract_companies(client)
    company_ids = None
    if not companies.empty:
        m = companies["name"].str.lower().str.contains("casa de los mineros", na=False)
        row = companies[m].iloc[0] if m.any() else companies.iloc[0]
        company_ids = [int(row["id"])]

    # 1) Líneas de lubricantes del período
    lines = extract_invoice_lines(client, date_from=desde, date_to=hoy,
                                  company_ids=company_ids)
    lub = la.filtrar_lubricantes(lines)
    if lub.empty:
        print("No hay ventas de lubricantes en el período.")
        return

    # 2) Vendedor que facturó (invoice_user_id) por cada factura
    invoices = extract_invoices(client, date_from=desde, date_to=hoy,
                                company_ids=company_ids)
    seller_map = {}
    if not invoices.empty and "invoice_user_id_name" in invoices.columns:
        seller_map = dict(zip(invoices["id"].astype(int),
                              invoices["invoice_user_id_name"]))
    lub = lub.copy()
    lub["vendedor"] = pd.to_numeric(lub["move_id"], errors="coerce").map(seller_map)

    # Filtrar a Felipe / Vanessa
    vmask = lub["vendedor"].fillna("").str.lower().apply(
        lambda s: any(p in s for p in patrones))
    lub_vf = lub[vmask]
    if lub_vf.empty:
        print("Ningún cliente compró lubricantes a esos vendedores en el período.")
        print("Vendedores detectados en lubricantes:")
        print(lub["vendedor"].dropna().value_counts().to_string())
        return

    # 3) Agregar por cliente
    es_fac = lub_vf["move_type"] == "out_invoice"
    agg = lub_vf.groupby("partner_id").agg(
        ventas_lub=("price_subtotal_signed", "sum"),
        vendedor=("vendedor", lambda s: s.dropna().mode().iat[0] if not s.dropna().empty else ""),
    )
    n_fac = lub_vf[es_fac].groupby("partner_id")["move_id"].nunique().rename("n_facturas")
    agg = agg.join(n_fac).reset_index()
    agg["n_facturas"] = agg["n_facturas"].fillna(0).astype(int)
    agg = agg[agg["ventas_lub"] > 0]

    # 4) Estado de ruta / GPS — leído DIRECTAMENTE por el id exacto de cada
    # comprador (no por el filtro customer_rank, que deja fuera a contactos
    # hijos y produciría falsos positivos).
    buyer_ids = [int(x) for x in agg["partner_id"].dropna().tolist()]
    recs = client.read(
        "res.partner", ids=buyer_ids,
        fields=["id", "name", "city", "sr_has_geo",
                "sr_route_id", "sr_route_ids", "phone"],
    )
    status = {int(r["id"]): r for r in recs}
    # Nombre de respaldo desde las líneas (por si el read no lo trae)
    name_lin = (lub_vf.dropna(subset=["partner_name"])
                .drop_duplicates("partner_id")
                .set_index("partner_id")["partner_name"].to_dict())

    filas, sin_estado = [], 0
    for _, r in agg.iterrows():
        pid = int(r["partner_id"])
        s = status.get(pid)
        if s is None:
            sin_estado += 1
            continue  # no pudimos leer su estado: no lo afirmamos
        sin_gps = not bool(s.get("sr_has_geo"))
        sin_rutero = _vacio_m2o(s.get("sr_route_id")) and _vacio_m2m(s.get("sr_route_ids"))
        if sin_gps and sin_rutero:
            filas.append({
                "Cliente": s.get("name") or name_lin.get(pid, ""),
                "Ciudad": s.get("city") or "",
                "Vendedor": r["vendedor"],
                "Ventas lubricantes": float(r["ventas_lub"]),
                "# Facturas": int(r["n_facturas"]),
                "Teléfono": s.get("phone") or "",
                "partner_id": pid,
            })
    out = pd.DataFrame(filas)
    if not out.empty:
        out = out.sort_values("Ventas lubricantes", ascending=False)
    if sin_estado:
        print(f"  (nota: {sin_estado} compradores sin estado legible, omitidos)\n")

    print(f"=== Clientes de lubricantes (Felipe/Vanessa) SIN GPS y SIN rutero ===")
    print(f"    Total: {len(out)}\n")
    if not out.empty:
        print(out[["Cliente", "Ciudad", "Vendedor", "Ventas lubricantes",
                   "# Facturas"]].head(40).to_string(index=False))
        out.drop(columns=["partner_id"]).to_excel(
            "lubricantes_sin_ruta.xlsx", index=False)
        print("\n✓ Excel generado: lubricantes_sin_ruta.xlsx")
    else:
        print("Todos los clientes de lubricantes de esos vendedores ya tienen "
              "GPS o rutero. 🎉")


if __name__ == "__main__":
    main()
