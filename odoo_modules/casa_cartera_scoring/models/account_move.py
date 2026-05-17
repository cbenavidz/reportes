# -*- coding: utf-8 -*-
"""
Extensión de account.move con métricas de cartera por factura.
"""
from __future__ import annotations

from datetime import timedelta

from odoo import _, api, fields, models


class AccountMove(models.Model):
    _inherit = "account.move"

    # Días al pago (si ya está pagada) o días desde emisión (si abierta)
    cartera_dias_al_pago = fields.Integer(
        string="Días al pago",
        compute="_compute_cartera_dias",
        store=True,
        help=(
            "Si está pagada: días entre fecha factura y fecha del último pago. "
            "Si abierta: días desde emisión hasta hoy."
        ),
    )
    cartera_dias_vencido = fields.Integer(
        string="Días vencidos",
        compute="_compute_cartera_dias",
        store=True,
    )
    cartera_aging_bucket = fields.Selection(
        [
            ("al_dia", "🟢 Al día"),
            ("0-30", "🟡 0-30 días"),
            ("31-60", "🟠 31-60 días"),
            ("61-90", "🔴 61-90 días"),
            ("91-120", "🟣 91-120 días"),
            ("+120", "⚫ +120 días"),
        ],
        string="Aging bucket",
        compute="_compute_cartera_dias",
        store=True,
    )
    cartera_proxima_a_vencer = fields.Boolean(
        string="⚠️ Próxima a vencer",
        compute="_compute_cartera_dias",
        store=True,
        help="Vence dentro de los próximos 7 días y aún no está pagada.",
    )

    @api.depends(
        "invoice_date", "invoice_date_due", "payment_state",
        "amount_residual", "state",
    )
    def _compute_cartera_dias(self):
        today = fields.Date.context_today(self)
        for move in self:
            if move.move_type not in ("out_invoice", "out_refund"):
                move.cartera_dias_al_pago = 0
                move.cartera_dias_vencido = 0
                move.cartera_aging_bucket = "al_dia"
                move.cartera_proxima_a_vencer = False
                continue

            if not move.invoice_date:
                move.cartera_dias_al_pago = 0
                move.cartera_dias_vencido = 0
                move.cartera_aging_bucket = "al_dia"
                move.cartera_proxima_a_vencer = False
                continue

            # Días al pago
            if move.payment_state in ("paid", "in_payment"):
                # Buscar último pago reconciliado
                payments = move._get_reconciled_payments()
                if payments:
                    ult_fecha = max(p.date for p in payments)
                    move.cartera_dias_al_pago = (ult_fecha - move.invoice_date).days
                else:
                    move.cartera_dias_al_pago = (today - move.invoice_date).days
            else:
                move.cartera_dias_al_pago = (today - move.invoice_date).days

            # Días vencidos
            if move.invoice_date_due and move.invoice_date_due < today and \
               move.payment_state in ("not_paid", "partial"):
                dias = (today - move.invoice_date_due).days
                move.cartera_dias_vencido = dias
                if dias <= 30:
                    move.cartera_aging_bucket = "0-30"
                elif dias <= 60:
                    move.cartera_aging_bucket = "31-60"
                elif dias <= 90:
                    move.cartera_aging_bucket = "61-90"
                elif dias <= 120:
                    move.cartera_aging_bucket = "91-120"
                else:
                    move.cartera_aging_bucket = "+120"
            else:
                move.cartera_dias_vencido = 0
                move.cartera_aging_bucket = "al_dia"

            # Próxima a vencer (próximos 7 días)
            if (
                move.invoice_date_due
                and move.payment_state in ("not_paid", "partial")
                and today <= move.invoice_date_due <= today + timedelta(days=7)
            ):
                move.cartera_proxima_a_vencer = True
            else:
                move.cartera_proxima_a_vencer = False

    def _get_reconciled_payments(self):
        """Helper: devuelve account.payment reconciliados con esta factura."""
        return self.env["account.payment"].search([
            ("reconciled_invoice_ids", "in", [self.id]),
        ])
