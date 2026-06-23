# -*- coding: utf-8 -*-
# Part of Softhealer Technologies.

import logging

from odoo import api, SUPERUSER_ID
_logger = logging.getLogger(__name__)


def uninstall_hook(cr, registry):
    """
    Clean up ir.config_parameter keys created by this module.
    These are NOT automatically removed by Odoo on uninstall.
    """
    env = api.Environment(cr, SUPERUSER_ID, {})
    ICP = env['ir.config_parameter'].sudo()
    keys_to_remove = [
        'sh_ai_assistance.onboarding_state',
        'sh_ai_assistance.onboarding_provider',
    ]
    for key in keys_to_remove:
        param = ICP.search([('key', '=', key)])
        if param:
            _logger.info("Uninstall hook: Removing ir.config_parameter '%s'", key)
            param.unlink()
