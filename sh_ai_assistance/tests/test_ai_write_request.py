# -*- coding: utf-8 -*-
# Part of Softhealer Technologies.

from odoo import fields
from odoo.tests.common import TransactionCase


class TestAiWriteRequest(TransactionCase):

    def setUp(self):
        super().setUp()
        self.ir_model = self.env["ir.model"]
        self.partner_model = self.ir_model.search([("model", "=", "res.partner")], limit=1)
        self.company_model = self.ir_model.search([("model", "=", "res.company")], limit=1)
        self.partner_field_model = self.env["ir.model.fields"]
        self.write_request_model = self.env["sh.ai.write.request"]

        self.partner = self.env["res.partner"].create({
            "name": "AI Target Partner",
            "email": "before@example.com",
            "is_company": True,
        })
        self.country_india = self.env.ref("base.in")
        self.category_vip = self.env["res.partner.category"].create({"name": "VIP"})

    def _create_restriction_rule(self, model, block_create=False, block_update=False, block_archive=False, blocked_fields=None):
        policy = self.env["sh.ai.write.policy"].create({
            "model_id": model.id,
            "block_create": block_create,
            "block_update": block_update,
            "block_archive": block_archive,
        })
        if blocked_fields:
            fields_to_block = self.partner_field_model.search([
                ("model_id", "=", model.id),
                ("name", "in", blocked_fields),
            ])
            for field in fields_to_block:
                self.env["sh.ai.write.policy.field"].create({
                    "policy_id": policy.id,
                    "field_id": field.id,
                })
        return policy

    def test_policy_is_restricted_when_rule_blocks_operation(self):
        self._create_restriction_rule(self.company_model, block_update=True)
        result = self.write_request_model.prepare_update_from_ai(
            model="res.company",
            domain=[["id", "=", self.env.company.id]],
            values={"name": "Blocked Rename"},
        )
        self.assertFalse(result["success"])
        self.assertTrue(result.get("policy_blocked"))

    def test_prepare_create_requires_approval_and_resolves_many2one(self):
        result = self.write_request_model.prepare_create_from_ai(
            model="res.partner",
            values={
                "name": "AI Created Contact",
                "email": "created@example.com",
                "country_id": "India",
            },
        )

        self.assertTrue(result["success"])
        request = self.write_request_model.browse(result["write_request_id"])
        self.assertEqual(request.state, "pending")
        self.assertFalse(self.env["res.partner"].search([("email", "=", "created@example.com")]))

        approval = request.action_approve_from_chat()
        self.assertTrue(approval["success"])

        created = self.env["res.partner"].search([("email", "=", "created@example.com")], limit=1)
        self.assertTrue(created)
        self.assertEqual(created.country_id, self.country_india)
        self.assertEqual(request.state, "done")
        self.assertTrue(any("AI Assistance" in (message.body or "") for message in created.message_ids))

    def test_prepare_update_does_not_write_until_approved(self):
        result = self.write_request_model.prepare_update_from_ai(
            model="res.partner",
            domain=[["id", "=", self.partner.id]],
            values={"email": "after@example.com"},
        )

        self.assertTrue(result["success"])
        request = self.write_request_model.browse(result["write_request_id"])
        self.assertEqual(self.partner.email, "before@example.com")
        self.assertEqual(request.preview_json["target_count"], 1)

        request.action_approve_from_chat()
        self.partner.invalidate_recordset(["email"])
        self.assertEqual(self.partner.email, "after@example.com")
        self.assertTrue(any("AI Assistance" in (message.body or "") for message in self.partner.message_ids))

    def test_prepare_archive_uses_archive_flow(self):
        result = self.write_request_model.prepare_archive_from_ai(
            model="res.partner",
            domain=[["id", "=", self.partner.id]],
        )

        self.assertTrue(result["success"])
        request = self.write_request_model.browse(result["write_request_id"])
        request.action_approve_from_chat()

        archived_partner = self.env["res.partner"].with_context(active_test=False).browse(self.partner.id)
        self.assertFalse(archived_partner.active)
        self.assertEqual(request.state, "done")
        self.assertTrue(any("AI Assistance" in (message.body or "") for message in archived_partner.message_ids))

    def test_binary_fields_are_rejected(self):
        result = self.write_request_model.prepare_update_from_ai(
            model="res.partner",
            domain=[["id", "=", self.partner.id]],
            values={"image_1920": "ZmFrZQ=="},
        )

        self.assertFalse(result["success"])
        self.assertTrue(result.get("needs_clarification"))

    def test_many2many_set_is_normalized_and_executed(self):
        result = self.write_request_model.prepare_update_from_ai(
            model="res.partner",
            domain=[["id", "=", self.partner.id]],
            values={"category_id": {"set": [self.category_vip.name]}},
        )

        self.assertTrue(result["success"])
        request = self.write_request_model.browse(result["write_request_id"])
        request.action_approve_from_chat()
        self.partner.invalidate_recordset(["category_id"])
        self.assertEqual(self.partner.category_id, self.category_vip)

    def test_one2many_create_is_normalized_and_executed(self):
        result = self.write_request_model.prepare_update_from_ai(
            model="res.partner",
            domain=[["id", "=", self.partner.id]],
            values={
                "child_ids": {
                    "create": [
                        {
                            "name": "AI Child Contact",
                            "email": "child@example.com",
                        }
                    ]
                }
            },
        )

        self.assertTrue(result["success"])
        request = self.write_request_model.browse(result["write_request_id"])
        request.action_approve_from_chat()

        child = self.env["res.partner"].search([
            ("parent_id", "=", self.partner.id),
            ("email", "=", "child@example.com"),
        ], limit=1)
        self.assertTrue(child)

    def test_restricted_field_is_enforced(self):
        self._create_restriction_rule(self.partner_model, blocked_fields=["phone"])
        result = self.write_request_model.prepare_update_from_ai(
            model="res.partner",
            domain=[["id", "=", self.partner.id]],
            values={"phone": "12345"},
        )

        self.assertFalse(result["success"])
        self.assertTrue(result.get("needs_clarification"))

    def test_update_permission_is_revalidated_under_requester(self):
        ai_user_group = self.env.ref("sh_ai_base.group_sh_ai_user")
        internal_group = self.env.ref("base.group_user")
        limited_user = self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "AI Limited User",
            "login": "ai_limited_user",
            "email": "ai_limited_user@example.com",
            "group_ids": [fields.Command.set([internal_group.id, ai_user_group.id])],
        })

        result = self.write_request_model.with_user(limited_user).prepare_update_from_ai(
            model="res.company",
            domain=[["id", "=", self.env.company.id]],
            values={"name": "Should Not Update"},
        )

        self.assertFalse(result["success"])
        self.assertIn("permission", (result.get("error") or "").lower())
