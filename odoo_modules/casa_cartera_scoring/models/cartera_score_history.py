# -*- coding: utf-8 -*-
"""
Histórico de scoring por cliente. Permite ver evolución del DSO,
cumplimiento, score a lo largo del tiempo. Snapshot mensual + manual.
"""
from odoo import fields, models


class CarteraScoreHistory(models.Model):
    _name = "casa.cartera.score.history"
    _description = "Histórico de scoring de cartera"
    _order = "fecha desc, id desc"
    _rec_name = "fecha"

    partner_id = fields.Many2one(
        "res.partner", required=True, ondelete="cascade", index=True,
        string="Cliente",
    )
    fecha = fields.Date(required=True, default=fields.Date.context_today, index=True)
    score = fields.Selection([
        ("A", "A"), ("B", "B"), ("C", "C"), ("D", "D"), ("S", "S"),
    ], string="Score")
    dso = fields.Float(string="DSO real")
    cumplimiento_pct = fields.Float(string="Cumplimiento %")
    saldo_abierto = fields.Monetary(string="Saldo abierto")
    currency_id = fields.Many2one(
        "res.currency", related="partner_id.currency_id", store=True,
    )
    notas = fields.Text(string="Notas")
