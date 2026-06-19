# -*- coding: utf-8 -*-

from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    sh_ai_max_iterations = fields.Integer(
        related="company_id.sh_ai_max_iterations",
        string="Max Tool Rounds",
        readonly=False,
    )
    sh_ai_max_duplicate_tool_calls = fields.Integer(
        related="company_id.sh_ai_max_duplicate_tool_calls",
        string="Max Duplicate Tool Calls",
        readonly=False,
    )
    sh_ai_write_policy_ids = fields.One2many(
        related="company_id.sh_ai_write_policy_ids",
        string="AI Write Restrictions",
        readonly=False,
    )
    sh_ai_write_policy_count = fields.Integer(
        related="company_id.sh_ai_write_policy_count",
        string="Configured Models",
    )
