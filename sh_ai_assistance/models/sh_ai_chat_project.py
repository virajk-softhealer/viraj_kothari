# -- coding: utf-8 --
# Copyright (C) Softhealer Technologies Pvt. Ltd.

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class ShAiChatProject(models.Model):
    _name = "sh.ai.chat.project"
    _description = "AI Chat Project"
    _order = "sequence, name, id"
    _rec_name = "display_name"

    name = fields.Char(string="Name", required=True)
    display_name = fields.Char(string="Display Name", compute="_compute_display_name", store=True)
    parent_id = fields.Many2one(
        "sh.ai.chat.project",
        string="Parent Project",
        ondelete="cascade",
        index=True,
    )
    child_ids = fields.One2many("sh.ai.chat.project", "parent_id", string="Child Projects")
    session_ids = fields.One2many("sh.ai.chat.session", "project_id", string="Chat Sessions")
    session_count = fields.Integer(string="Session Count", compute="_compute_session_count", store=False)
    sequence = fields.Integer(string="Sequence", default=10)
    user_id = fields.Many2one(
        "res.users",
        string="User",
        required=True,
        default=lambda self: self.env.user,
        ondelete="cascade",
        index=True,
    )
    active = fields.Boolean(string="Active", default=True)

    @api.depends("name", "parent_id.display_name")
    def _compute_display_name(self):
        """Compute a breadcrumb-style project label."""
        for project in self:
            if project.parent_id:
                project.display_name = "%s / %s" % (project.parent_id.display_name, project.name)
            else:
                project.display_name = project.name

    @api.depends("session_ids", "child_ids.session_count")
    def _compute_session_count(self):
        """Count direct and nested sessions for display in the sidebar."""
        for project in self:
            nested_count = sum(project.child_ids.mapped("session_count"))
            project.session_count = len(project.session_ids) + nested_count

    @api.constrains("parent_id", "user_id")
    def _check_project_tree(self):
        """Prevent recursive loops and cross-user parenting."""
        for project in self:
            current = project.parent_id
            while current:
                if current.id == project.id:
                    raise ValidationError(_("A project cannot be its own parent."))
                current = current.parent_id

            if project.parent_id and project.parent_id.user_id != project.user_id:
                raise ValidationError(_("A project can only contain subprojects owned by the same user."))

    @api.model_create_multi
    def create(self, vals_list):
        """Create projects for the current user unless explicitly provided."""
        for vals in vals_list:
            if not vals.get("user_id"):
                vals["user_id"] = self.env.user.id
        projects = super().create(vals_list)
        return projects

    def write(self, vals):
        """Keep the project tree within the same owner namespace."""
        if vals.get("parent_id"):
            parent = self.browse(vals["parent_id"])
            if parent and parent.user_id != self.env.user:
                raise ValidationError(_("You cannot move a project under another user's tree."))
        return super().write(vals)

    def unlink(self):
        """Delete descendant sessions before removing the project subtree."""
        Session = self.env["sh.ai.chat.session"]
        descendant_ids = set()
        projects_to_visit = self
        while projects_to_visit:
            descendant_ids.update(projects_to_visit.ids)
            projects_to_visit = self.search([("parent_id", "in", projects_to_visit.ids)])

        if descendant_ids:
            sessions = Session.search([("project_id", "in", list(descendant_ids))])
            if sessions:
                sessions.unlink()
        return super().unlink()
