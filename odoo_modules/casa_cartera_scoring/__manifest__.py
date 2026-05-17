# -*- coding: utf-8 -*-
{
    "name": "Casa de los Mineros — Cartera & Scoring",
    "version": "19.0.1.0.0",
    "category": "Accounting/Accounting",
    "summary": (
        "Análisis avanzado de cartera con DSO real, scoring A/B/C/D, "
        "hábito de pago y alertas de riesgo. Vistas kanban + pivot + dashboard."
    ),
    "description": """
Cartera & Scoring para Casa de los Mineros
==========================================

Extiende `res.partner` y `account.move` con campos computados:
  * DSO (Days Sales Outstanding) real, basado en FIFO matching factura-pago
  * Score A/B/C/D según rotación + cumplimiento + concentración
  * Hábito de pago (categoría: "Excelente", "Bueno", "Lento", "Crítico")
  * Plazo otorgado nominal vs días reales al pago
  * Saldo abierto + vencido + aging buckets

Incluye:
  * Vistas kanban con código de color por score
  * Pivot por vendedor + por categoría
  * Dashboard ejecutivo de cartera
  * Cron job nocturno que recalcula scoring
  * Alertas: facturas próximas a vencer, clientes que pasaron de B→C, etc.
""",
    "author": "Casa de los Mineros",
    "website": "https://casadelosmineros.com.co",
    "depends": [
        "base",
        "account",
        "account_accountant",  # Enterprise: para account.move avanzado
        "sale",
        "mail",  # para activities/alerts
    ],
    "data": [
        "security/ir.model.access.csv",
        "data/cartera_config_data.xml",
        "data/cartera_cron.xml",
        "views/res_partner_views.xml",
        "views/account_move_views.xml",
        "views/cartera_dashboard.xml",
        "views/cartera_menu.xml",
        "report/cartera_reports.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "casa_cartera_scoring/static/src/js/cartera_dashboard.js",
            "casa_cartera_scoring/static/src/xml/cartera_dashboard.xml",
            "casa_cartera_scoring/static/src/scss/cartera_dashboard.scss",
        ],
    },
    "installable": True,
    "application": True,
    "auto_install": False,
    "license": "LGPL-3",
}
