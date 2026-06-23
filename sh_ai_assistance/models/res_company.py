# -*- coding: utf-8 -*-
# Part of Softhealer Technologies.

from odoo import fields, models


class ResCompany(models.Model):
    _inherit = "res.company"

    sh_ai_max_iterations = fields.Integer(
        string="AI Max Tool Rounds",
        default=20,
        help="Maximum number of agent loop rounds allowed in a single AI turn before the assistant must stop or recover.",
    )
    sh_ai_max_duplicate_tool_calls = fields.Integer(
        string="AI Max Duplicate Tool Calls",
        default=5,
        help="Maximum number of identical repeated tool calls allowed in one AI turn before the assistant stops the loop.",
    )
    sh_ai_write_policy_ids = fields.One2many(
        "sh.ai.write.policy",
        "company_id",
        string="AI Write Restrictions",
    )
    sh_ai_write_policy_count = fields.Integer(
        compute="_compute_sh_ai_write_policy_count",
        string="AI Write Restrictions Count",
    )

    def _compute_sh_ai_write_policy_count(self):
        """Compute the number of write policies for the company."""
        for company in self:
            company.sh_ai_write_policy_count = len(company.sh_ai_write_policy_ids)
