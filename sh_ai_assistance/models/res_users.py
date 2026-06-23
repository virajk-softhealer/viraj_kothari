# -*- coding: utf-8 -*-
# Part of Softhealer Technologies.

from odoo import fields, models


class ResUsers(models.Model):
    _inherit = 'res.users'

    sh_ai_onboarding_seen = fields.Boolean(
        string="AI Onboarding Seen",
        default=False,
        help="Whether this user has seen the AI assistant welcome tour.",
    )
