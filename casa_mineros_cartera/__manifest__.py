# -*- coding: utf-8 -*-
{
    'name': 'Cartera Inteligente - Casa de los Mineros',
    'version': '19.0.1.0.0',
    'category': 'Accounting/Accounting',
    'summary': 'Gestión inteligente de cartera: rotación, aging, scoring de clientes, alertas y recomendaciones de cobro',
    'description': """
Cartera Inteligente para Casa de los Mineros
==============================================

Módulo desarrollado a la medida para el análisis y gestión de cuentas por cobrar.

Funcionalidades principales:
----------------------------
* Cálculo automático de **rotación de cartera** (en días y veces) por período
* **Aging report** detallado por rangos (corriente, 1-30, 31-60, 61-90, +90 días)
* **Calificación automática de clientes** A/B/C/D según hábito de pago histórico
* **Sistema de alertas de riesgo** configurable (mora creciente, exceso de cupo, etc.)
* **Motor de recomendaciones de cobro** con priorización inteligente
* **Dashboard ejecutivo** con KPIs en tiempo real
* Recálculo automático diario vía cron

Desarrollado para Odoo 19.
""",
    'author': 'Casa de los Mineros',
    'website': 'https://casadelosmineros.com.co',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'account',
        'mail',
        'contacts',
    ],
    'data': [
        'security/cartera_security.xml',
        'security/ir.model.access.csv',
        'data/cartera_config_data.xml',
        'data/cron_data.xml',
        'views/res_partner_views.xml',
        'views/account_move_views.xml',
        'views/cartera_analysis_views.xml',
        'views/cartera_alert_views.xml',
        'views/cartera_recommendation_views.xml',
        'views/cartera_dashboard_views.xml',
        'views/cartera_menus.xml',
        'reports/aged_receivable_report.xml',
    ],
    'installable': True,
    'application': True,
    'auto_install': False,
}
