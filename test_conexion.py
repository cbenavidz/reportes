# -*- coding: utf-8 -*-
"""
Script para probar la conexión con Odoo 19 (Odoo.sh).

Uso:
    python3 test_conexion.py

Lee credenciales del archivo .env en la misma carpeta.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Cargar .env manualmente (sin depender de python-dotenv en este test)
def load_env(env_path: Path) -> None:
    if not env_path.exists():
        print(f"❌ No se encontró el archivo .env en: {env_path}")
        sys.exit(1)
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def main() -> None:
    env_path = Path(__file__).parent / ".env"
    load_env(env_path)

    url = os.getenv("ODOO_URL", "").rstrip("/")
    db = os.getenv("ODOO_DB", "")
    username = os.getenv("ODOO_USERNAME", "")
    api_key = os.getenv("ODOO_API_KEY", "")

    print("=" * 60)
    print("PRUEBA DE CONEXIÓN - Cartera Casa de los Mineros")
    print("=" * 60)
    print(f"URL:      {url}")
    print(f"DB:       {db}")
    print(f"Usuario:  {username}")
    print(f"API Key:  {api_key[:8]}...{api_key[-4:]} ({len(api_key)} chars)")
    print("-" * 60)

    import xmlrpc.client

    # 1. Test de versión
    try:
        common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common", allow_none=True)
        version = common.version()
        print(f"✓ Versión de Odoo detectada: {version.get('server_version')}")
    except Exception as exc:
        print(f"❌ Error conectando a {url}: {exc}")
        sys.exit(1)

    # 2. Autenticación
    try:
        uid = common.authenticate(db, username, api_key, {})
        if not uid:
            print("❌ Autenticación falló. Posibles causas:")
            print("   - Nombre de DB incorrecto (revisa Odoo.sh > Settings > Database)")
            print("   - Usuario no existe en esa DB")
            print("   - API key inválida o revocada")
            sys.exit(1)
        print(f"✓ Autenticado como UID = {uid}")
    except Exception as exc:
        print(f"❌ Error en autenticación: {exc}")
        sys.exit(1)

    # 3. Pruebas de lectura
    models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object", allow_none=True)

    try:
        n_partners = models.execute_kw(
            db, uid, api_key, "res.partner", "search_count",
            [[("customer_rank", ">", 0)]],
        )
        print(f"✓ Clientes activos (customer_rank>0): {n_partners}")
    except Exception as exc:
        print(f"⚠ No se pudo leer res.partner: {exc}")

    try:
        n_invoices = models.execute_kw(
            db, uid, api_key, "account.move", "search_count",
            [[("move_type", "=", "out_invoice"), ("state", "=", "posted")]],
        )
        print(f"✓ Facturas de venta posted: {n_invoices}")
    except Exception as exc:
        print(f"⚠ No se pudo leer account.move: {exc}")

    try:
        n_open = models.execute_kw(
            db, uid, api_key, "account.move", "search_count",
            [[
                ("move_type", "=", "out_invoice"),
                ("state", "=", "posted"),
                # `in_payment` ya está pagada (pendiente conciliación), saldo 0.
                ("payment_state", "in", ["not_paid", "partial"]),
            ]],
        )
        print(f"✓ Facturas de venta abiertas (con saldo): {n_open}")
    except Exception as exc:
        print(f"⚠ No se pudo leer facturas abiertas: {exc}")

    try:
        n_payments = models.execute_kw(
            db, uid, api_key, "account.payment", "search_count",
            [[("payment_type", "=", "inbound"), ("state", "in", ["posted", "paid"])]],
        )
        print(f"✓ Pagos de clientes (inbound): {n_payments}")
    except Exception as exc:
        print(f"⚠ No se pudo leer account.payment: {exc}")

    print("-" * 60)
    print("✅ ¡Conexión exitosa! Ya podemos extraer datos reales.")
    print("=" * 60)


if __name__ == "__main__":
    main()
