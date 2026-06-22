# -*- coding: utf-8 -*-

from odoo import api, fields, models
from odoo.exceptions import ValidationError


BLOCKED_TECHNICAL_MODEL_PREFIXES = ("ir.", "sh.ai.", "bus.")
BLOCKED_TECHNICAL_MODEL_NAMES = {
    "ir.model",
    "ir.model.fields",
    "ir.model.access",
    "ir.rule",
    "ir.config_parameter",
    "res.groups",
}


class ShAiWritePolicy(models.Model):
    _name = "sh.ai.write.policy"
    _description = "AI Write Restriction Rule"
    _order = "model_name"
    company_id = fields.Many2one(
        "res.company",
        string="Company",
        default=lambda self: self.env.company,
        ondelete="cascade",
    )
    model_id = fields.Many2one(
        "ir.model",
        string="Model",
        required=True,
        ondelete="cascade",
    )
    model_name = fields.Char(related="model_id.model", string="Technical Model", store=True, readonly=True)
    model_display_name = fields.Char(related="model_id.name", string="Model Label", readonly=True)
    block_create = fields.Boolean(string="Restrict Create")
    block_update = fields.Boolean(string="Restrict Update")
    block_archive = fields.Boolean(string="Restrict Archive")
    blocked_field_ids = fields.One2many(
        "sh.ai.write.policy.field",
        "policy_id",
        string="Restricted Fields",
    )

    _sql_constraints = [
        ("model_unique", "UNIQUE(model_id, company_id)", "Only one AI write rule is allowed per model and company.")
    ]

    @api.constrains("model_id")
    def _check_model_id(self):
        for policy in self:
            if not policy.model_id:
                continue
            model_name = policy.model_id.model or ""
            if getattr(policy.model_id, "transient", False):
                raise ValidationError("Transient models are always protected from AI write actions and cannot be added here.")
            if model_name in BLOCKED_TECHNICAL_MODEL_NAMES or model_name.startswith(BLOCKED_TECHNICAL_MODEL_PREFIXES):
                raise ValidationError("Technical, security, and internal AI models are always protected from AI write actions and cannot be added here.")

    @api.onchange("block_update")
    def _onchange_block_update(self):
        if self.block_update:
            self.block_archive = True
            return {
                "warning": {
                    "title": "Archive Access Restricted",
                    "message": "Restricting updates also restricts archiving records. To allow archiving, you must first allow updates.",
                }
            }

    @api.onchange("block_archive")
    def _onchange_block_archive(self):
        if not self.block_archive and self.block_update:
            self.block_archive = True
            return {
                "warning": {
                    "title": "Action Not Allowed",
                    "message": "You cannot allow archiving while updates are restricted. Archiving requires write access to record data.",
                }
            }

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            # Enforce: If Restricted Update, Restricted Archive must be True
            if vals.get("block_update"):
                vals["block_archive"] = True
        return super().create(vals_list)

    def write(self, vals):
        res = super().write(vals)
        # Post-write enforcement to ensure consistency across records
        if "block_update" in vals or "block_archive" in vals:
            for record in self:
                if record.block_update and not record.block_archive:
                    # Silently fix any inconsistency instead of blocking the user
                    record.sudo().block_archive = True
        return res


class ShAiWritePolicyField(models.Model):
    _name = "sh.ai.write.policy.field"
    _description = "AI Write Restricted Field"
    _order = "field_name"

    policy_id = fields.Many2one(
        "sh.ai.write.policy",
        string="Rule",
        required=True,
        ondelete="cascade",
    )
    model_id = fields.Many2one(related="policy_id.model_id", string="Model", store=True, readonly=True)
    field_id = fields.Many2one(
        "ir.model.fields",
        string="Field",
        required=True,
        ondelete="cascade",
    )
    field_name = fields.Char(related="field_id.name", string="Technical Field", store=True, readonly=True)
    field_label = fields.Char(related="field_id.field_description", string="Label", readonly=True)
    field_type = fields.Selection(related="field_id.ttype", string="Type", store=True, readonly=True)
    _sql_constraints = [
        ("policy_field_unique", "UNIQUE(policy_id, field_id)", "This field is already restricted on the selected AI write rule.")
    ]

    @api.constrains("field_id", "policy_id")
    def _check_field_model(self):
        for line in self:
            if line.field_id.model_id != line.policy_id.model_id:
                raise ValidationError("Restricted fields must belong to the same model as the AI write rule.")
