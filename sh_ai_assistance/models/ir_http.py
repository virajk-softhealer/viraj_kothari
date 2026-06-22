# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies.

from odoo import models
from odoo.http import request


class IrHttp(models.AbstractModel):
    _inherit = 'ir.http'

    def session_info(self):
        """
        Extend session_info to include the user's color scheme preference.

        In Odoo 17, dark mode is managed via cookies and session info
        rather than the ir.http.color_scheme() method (which is Odoo 19+).
        """
        result = super().session_info()

        # Add color scheme preference if the user has one
        try:
            if request.session.uid:
                user = self.env['res.users'].browse(request.session.uid)
                if hasattr(user, 'color_scheme'):
                    result['color_scheme'] = user.color_scheme or 'light'
                else:
                    result['color_scheme'] = 'light'
            else:
                result['color_scheme'] = 'light'
        except Exception:
            result['color_scheme'] = 'light'

        return result
