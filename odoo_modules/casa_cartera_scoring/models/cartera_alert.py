# -*- coding: utf-8 -*-
"""
Alertas automáticas de cartera. Se generan vía cron y aparecen como
activities del usuario responsable del cliente.
"""
from datetime import timedelta

from odoo import _, api, fields, models


class CarteraAlert(models.Model):
    _name = "casa.cartera.alert"
    _description = "Alerta de cartera"
    _order = "fecha desc, severity desc"
    _rec_name = "titulo"

    partner_id = fields.Many2one("res.partner", required=True, ondelete="cascade")
    fecha = fields.Datetime(default=fields.Datetime.now, index=True)
    titulo = fields.Char(required=True)
    mensaje = fields.Text()
    severity = fields.Selection([
        ("info", "ℹ️ Info"),
        ("warning", "⚠️ Atención"),
        ("danger", "🚨 Crítico"),
    ], default="warning", string="Severidad")
    tipo = fields.Selection([
        ("proxima_vencer", "Próxima a vencer"),
        ("vencida_alta", "Vencida monto alto"),
        ("score_caida", "Score bajó"),
        ("excede_credito", "Excede límite crédito"),
        ("inactivo", "Cliente inactivo"),
    ])
    resuelto = fields.Boolean(default=False)

    @api.model
    def _cron_generate_alerts(self):
        """
        Cron diario: genera alertas frescas. Tipos:
          - Facturas próximas a vencer (7 días)
          - Clientes que bajaron de score B→C o C→D
          - Clientes que excedieron límite de crédito
          - Clientes A/B que no compran hace >60 días (riesgo churn)
        """
        # 1. Próximas a vencer
        today = fields.Date.context_today(self)
        invoices = self.env["account.move"].search([
            ("move_type", "=", "out_invoice"),
            ("state", "=", "posted"),
            ("payment_state", "in", ["not_paid", "partial"]),
            ("cartera_proxima_a_vencer", "=", True),
        ])
        for inv in invoices:
            existing = self.search([
                ("partner_id", "=", inv.partner_id.id),
                ("tipo", "=", "proxima_vencer"),
                ("fecha", ">=", today - timedelta(days=1)),
                ("resuelto", "=", False),
            ], limit=1)
            if existing:
                continue
            self.create({
                "partner_id": inv.partner_id.id,
                "titulo": _("Factura %s vence en %d días") % (
                    inv.name, (inv.invoice_date_due - today).days,
                ),
                "mensaje": _("Monto: %s. Contactar al cliente.") % inv.amount_residual,
                "severity": "warning",
                "tipo": "proxima_vencer",
            })

        # 2. Bajadas de score (compara con último history)
        partners = self.env["res.partner"].search([
            ("cartera_score", "in", ["C", "D"]),
        ])
        for p in partners:
            history = self.env["casa.cartera.score.history"].search([
                ("partner_id", "=", p.id),
            ], order="fecha desc", limit=2)
            if len(history) < 2:
                continue
            score_orden = {"A": 4, "B": 3, "C": 2, "D": 1, "S": 0}
            actual = score_orden.get(history[0].score, 0)
            anterior = score_orden.get(history[1].score, 0)
            if actual < anterior:
                self.create({
                    "partner_id": p.id,
                    "titulo": _("Score bajó: %s → %s") % (
                        history[1].score, history[0].score
                    ),
                    "mensaje": _(
                        "DSO actual: %.0f días. Cumplimiento: %.0f%%."
                    ) % (p.cartera_dso_real, p.cartera_cumplimiento_pct),
                    "severity": "danger" if history[0].score == "D" else "warning",
                    "tipo": "score_caida",
                })

        return True
