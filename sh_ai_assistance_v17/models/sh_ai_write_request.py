# -*- coding: utf-8 -*-

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from ...sh_ai_base.provider.odoo_tools import (
    fuzzy_lookup,
    _pick_unique_lookup_result,
    _resolve_field_path,
)
from ..ai_processing.utils import sanitize_for_json
from .sh_ai_write_policy import (
    BLOCKED_TECHNICAL_MODEL_NAMES,
    BLOCKED_TECHNICAL_MODEL_PREFIXES,
)


SUPPORTED_SCALAR_FIELD_TYPES = {
    "boolean",
    "char",
    "date",
    "datetime",
    "float",
    "html",
    "integer",
    "monetary",
    "selection",
    "text",
}
RELATIONAL_FIELD_TYPES = {"many2one", "many2many", "one2many"}
UNSUPPORTED_FIELD_TYPES = {"binary", "image"}
PREVIEW_SAMPLE_LIMIT = 20
MAX_RESOLUTION_CANDIDATES = 5


class ShAiWriteRequest(models.Model):
    _name = "sh.ai.write.request"
    _description = "AI Write Request"
    _order = "create_date desc, id desc"
    _rec_name = "display_name"

    display_name = fields.Char(string="Request", compute="_compute_display_name")
    operation = fields.Selection(
        [
            ("create", "Create"),
            ("update", "Update"),
            ("archive", "Archive"),
        ],
        string="Operation",
        required=True,
    )
    state = fields.Selection(
        [
            ("pending", "Pending Approval"),
            ("approved", "Approved"),
            ("executing", "Executing"),
            ("done", "Done"),
            ("rejected", "Rejected"),
            ("failed", "Failed"),
            ("expired", "Expired"),
        ],
        string="Status",
        required=True,
        default="pending",
        index=True,
    )
    requester_id = fields.Many2one(
        "res.users",
        string="Requester",
        required=True,
        default=lambda self: self.env.user,
        ondelete="cascade",
    )
    approver_id = fields.Many2one("res.users", string="Approved / Rejected By", ondelete="set null")
    session_id = fields.Many2one("sh.ai.chat.session", string="Chat Session", ondelete="cascade")
    message_id = fields.Many2one("sh.ai.chat.message", string="Proposal Message", ondelete="set null")
    llm_id = fields.Many2one("sh.ai.llm", string="Model", ondelete="set null")
    target_model = fields.Char(string="Target Model", required=True, index=True)
    target_model_display_name = fields.Char(string="Target Model Label", required=True)
    target_domain = fields.Json(string="Target Domain")
    target_record_ids = fields.Json(string="Target Record IDs")
    requested_values = fields.Json(string="Requested Values")
    normalized_values = fields.Json(string="Normalized Values")
    preview_json = fields.Json(string="Preview Payload")
    result_json = fields.Json(string="Execution Result")
    action_data = fields.Json(string="Open View Action")
    summary = fields.Text(string="Summary")
    failure_reason = fields.Text(string="Failure Reason")
    approved_at = fields.Datetime(string="Approved At")
    executed_at = fields.Datetime(string="Executed At")
    rejected_at = fields.Datetime(string="Rejected At")
    expires_at = fields.Datetime(
        string="Expires At",
        default=lambda self: fields.Datetime.now() + relativedelta(days=1),
    )

    @api.depends("operation", "target_model_display_name", "state")
    def _compute_display_name(self):
        operation_labels = dict(self._fields["operation"].selection)
        state_labels = dict(self._fields["state"].selection)
        for request in self:
            operation = operation_labels.get(request.operation, request.operation or "Write")
            model_name = request.target_model_display_name or request.target_model or "Records"
            state = state_labels.get(request.state, request.state or "")
            request.display_name = f"{operation} {model_name} [{state}]"

    @api.model
    def prepare_create_from_ai(self, model, values, session_id=None, llm_id=None):
        return self._prepare_write_request(
            operation="create",
            model_name=model,
            raw_values=values,
            domain=[],
            session_id=session_id,
            llm_id=llm_id,
        )

    @api.model
    def prepare_update_from_ai(self, model, domain, values, session_id=None, llm_id=None):
        return self._prepare_write_request(
            operation="update",
            model_name=model,
            raw_values=values,
            domain=domain,
            session_id=session_id,
            llm_id=llm_id,
        )

    @api.model
    def prepare_archive_from_ai(self, model, domain, session_id=None, llm_id=None):
        return self._prepare_write_request(
            operation="archive",
            model_name=model,
            raw_values={},
            domain=domain,
            session_id=session_id,
            llm_id=llm_id,
        )

    @api.model
    def _prepare_write_request(self, operation, model_name, raw_values=None, domain=None, session_id=None, llm_id=None):
        raw_values = raw_values or {}
        domain = self._coerce_domain(domain)

        Model = self._get_write_target_model(model_name)
        if Model is None:
            return self._error_result(
                f"Model '{model_name}' is not available for AI writes. It may be protected, technical, transient, or inaccessible."
            )

        policy = self._get_policy_for_model(model_name)
        operation_allowed = self._is_operation_allowed(policy, operation)
        if not operation_allowed:
            return self._error_result(
                "AI write access is restricted for this model or operation.",
                policy_blocked=True,
            )

        try:
            if operation == "create":
                Model.check_access_rights("create")
            else:
                Model.check_access_rights("read")
        except AccessError:
            return self._error_result("You do not have permission to prepare this write action.")

        if operation in ("create", "update") and not isinstance(raw_values, dict):
            return self._error_result("Write values must be provided as an object of field/value pairs.")
        if operation == "update" and not raw_values:
            return self._error_result("An update request must include at least one field to change.")

        issues = []
        normalized_domain = []
        target_records = Model.browse()
        preview_action = None

        if operation in ("update", "archive"):
            normalized_domain, domain_issues = self._normalize_target_domain(Model, domain)
            issues.extend(domain_issues)
            if issues:
                return self._clarification_result(issues)

            target_records = Model.search(normalized_domain)
            if not target_records:
                return self._error_result("No matching records were found for this write request.", zero_result=True)

            try:
                target_records.check_access_rights("write")
                target_records.check_access_rule("write")
            except AccessError:
                return self._error_result("You do not have permission to modify one or more matching records.")

            preview_action = self._build_target_action_data(model_name, target_records.ids, operation=operation)

        normalized_values = {}
        value_previews = {}
        if operation in ("create", "update"):
            normalized_values, value_previews, value_issues = self._normalize_write_values(
                Model,
                raw_values,
                policy,
                operation=operation,
            )
            issues.extend(value_issues)
            if issues:
                return self._clarification_result(issues)

        if operation == "create":
            missing_fields = self._find_missing_required_fields(Model, normalized_values)
            if missing_fields:
                return self._clarification_result(
                    [
                        {
                            "code": "missing_required_fields",
                            "message": "I still need required values before I can prepare this create request.",
                            "fields": missing_fields,
                        }
                    ]
                )
            preview_json = self._build_create_preview(Model, normalized_values, value_previews)
            summary = f"Create 1 {self._get_model_display_name(model_name)} record."
        elif operation == "update":
            preview_json = self._build_update_preview(Model, target_records, normalized_values, value_previews)
            summary = "Update %s %s record(s)." % (
                len(target_records),
                self._get_model_display_name(model_name),
            )
        else:
            if not self._supports_archive(Model):
                return self._error_result("This model does not support archive actions.")
            preview_json = self._build_archive_preview(Model, target_records)
            summary = "Archive %s %s record(s)." % (
                len(target_records),
                self._get_model_display_name(model_name),
            )

        request = self.create(
            {
                "operation": operation,
                "requester_id": self.env.user.id,
                "session_id": session_id,
                "llm_id": llm_id,
                "target_model": model_name,
                "target_model_display_name": self._get_model_display_name(model_name),
                "target_domain": normalized_domain,
                "target_record_ids": target_records.ids,
                "requested_values": sanitize_for_json(raw_values),
                "normalized_values": sanitize_for_json(normalized_values),
                "preview_json": sanitize_for_json(preview_json),
                "action_data": sanitize_for_json(preview_action),
                "summary": summary,
            }
        )
        return {
            "success": True,
            "write_request_id": request.id,
            "operation": operation,
            "model": model_name,
            "model_display_name": request.target_model_display_name,
            "target_count": len(target_records) if operation != "create" else 1,
            "summary": summary,
            "preview": sanitize_for_json(preview_json),
            "action_data": sanitize_for_json(preview_action),
            "approval_required": True,
            "message": "A write request preview is ready and is waiting for explicit approval.",
        }

    def action_approve_from_chat(self):
        self.ensure_one()
        self._check_can_decide()
        if self.state != "pending":
            return {"success": False, "error": "Only pending write requests can be approved.", "request": self._serialize_for_chat()}

        now = fields.Datetime.now()
        if self.expires_at and self.expires_at < now:
            self.write({"state": "expired", "failure_reason": "The approval window expired before execution."})
            self._append_message_event("write.failed", reason="expired")
            return {"success": False, "error": "This write request has expired.", "request": self._serialize_for_chat()}

        self.write(
            {
                "state": "approved",
                "approver_id": self.env.user.id,
                "approved_at": now,
            }
        )
        self._append_message_event("write.approved", approver=self.env.user.name, request_id=self.id)

        try:
            self.write({"state": "executing"})
            self._append_message_event("write.executing", request_id=self.id)
            result_json = self._execute_business_write()
            self.write(
                {
                    "state": "done",
                    "executed_at": fields.Datetime.now(),
                    "result_json": sanitize_for_json(result_json),
                    "action_data": sanitize_for_json(result_json.get("action_data") or self.action_data),
                    "failure_reason": False,
                }
            )
            self._append_message_event(
                "write.completed",
                request_id=self.id,
                affected_count=result_json.get("affected_count"),
            )
            return {"success": True, "request": self._serialize_for_chat()}
        except Exception as error:
            self.write(
                {
                    "state": "failed",
                    "failure_reason": str(error),
                    "result_json": sanitize_for_json(
                        {
                            "success": False,
                            "message": str(error),
                        }
                    ),
                }
            )
            self._append_message_event("write.failed", request_id=self.id, error=str(error))
            return {"success": False, "error": str(error), "request": self._serialize_for_chat()}

    def action_reject_from_chat(self):
        self.ensure_one()
        self._check_can_decide()
        if self.state != "pending":
            return {"success": False, "error": "Only pending write requests can be rejected.", "request": self._serialize_for_chat()}

        self.write(
            {
                "state": "rejected",
                "approver_id": self.env.user.id,
                "rejected_at": fields.Datetime.now(),
                "result_json": sanitize_for_json(
                    {
                        "success": False,
                        "message": "The write request was rejected before execution.",
                    }
                ),
            }
        )
        self._append_message_event("write.rejected", approver=self.env.user.name, request_id=self.id)
        return {"success": True, "request": self._serialize_for_chat()}

    def action_open_target_records(self, view_type="list"):
        self.ensure_one()
        if self.operation == "create" and self.state == "done" and self.result_json:
            action_data = self.result_json.get("action_data") or self.action_data
        else:
            action_data = self.action_data or self._build_target_action_data(self.target_model, self.target_record_ids or [], operation=self.operation)

        if not action_data:
            return {"type": "ir.actions.act_window_close"}

        domain = action_data.get("domain") or [["id", "in", self.target_record_ids or []]]
        context = action_data.get("context") or {}
        return {
            "type": "ir.actions.act_window",
            "name": self.target_model_display_name,
            "res_model": self.target_model,
            "view_mode": view_type,
            "views": [(False, view_type)],
            "domain": domain,
            "context": context,
            "target": "current",
        }

    def _execute_business_write(self):
        self.ensure_one()
        requester_request = self.with_user(self.requester_id)
        Model = requester_request.env[self.target_model]
        policy = requester_request._get_policy_for_model(self.target_model)
        if not requester_request._is_operation_allowed(policy, self.operation):
            raise UserError("AI write access is now restricted for this model or operation.")

        if self.operation == "create":
            Model.check_access_rights("create")
            requester_request._validate_fields_allowed(Model, self.normalized_values or {}, policy)
            orm_values = requester_request._materialize_write_values(Model, self.normalized_values or {})
            created = Model.create(orm_values)
            requester_request._post_ai_write_audit_note(created, operation="create")
            action_data = self._build_target_action_data(self.target_model, created.ids, operation="create")
            return {
                "success": True,
                "message": "Created %s %s record(s)." % (len(created), self.target_model_display_name),
                "affected_count": len(created),
                "created_ids": created.ids,
                "action_data": action_data,
            }

        target_records = requester_request._get_revalidated_target_records()

        if self.operation == "update":
            requester_request._validate_fields_allowed(Model, self.normalized_values or {}, policy)
            orm_values = requester_request._materialize_write_values(Model, self.normalized_values or {})
            target_records.write(orm_values)
            requester_request._post_ai_write_audit_note(target_records, operation="update")
            return {
                "success": True,
                "message": "Updated %s %s record(s)." % (len(target_records), self.target_model_display_name),
                "affected_count": len(target_records),
                "updated_ids": target_records.ids,
                "action_data": self._build_target_action_data(self.target_model, target_records.ids, operation="update"),
            }

        if not requester_request._supports_archive(Model):
            raise UserError("This model does not support archive actions.")

        target_records.action_archive()
        requester_request._post_ai_write_audit_note(target_records, operation="archive")
        return {
            "success": True,
            "message": "Archived %s %s record(s)." % (len(target_records), self.target_model_display_name),
            "affected_count": len(target_records),
            "archived_ids": target_records.ids,
            "action_data": self._build_target_action_data(self.target_model, target_records.ids, operation="archive"),
        }

    def _get_revalidated_target_records(self):
        self.ensure_one()
        ids_snapshot = [int(record_id) for record_id in (self.target_record_ids or [])]
        Model = self.env[self.target_model]
        records = Model.search([("id", "in", ids_snapshot)])

        if not records:
            records = Model.with_context(active_test=False).search([("id", "in", ids_snapshot)])
            
        if sorted(records.ids) != sorted(ids_snapshot):
            raise UserError("Some target records are no longer available with your current access rights.")
        records.check_access_rights("read")
        records.check_access_rule("read")
        records.check_access_rights("write")
        records.check_access_rule("write")
        return records

    def _check_can_decide(self):
        self.ensure_one()
        if self.requester_id == self.env.user:
            return True
        if self.env.user.has_group("sh_ai_base.group_sh_ai_manager"):
            return True
        raise AccessError("You are not allowed to approve or reject this AI write request.")

    def _serialize_for_chat(self):
        self.ensure_one()
        return {
            "id": self.id,
            "operation": self.operation,
            "state": self.state,
            "summary": self.summary,
            "target_model": self.target_model,
            "target_model_display_name": self.target_model_display_name,
            "preview_json": sanitize_for_json(self.preview_json),
            "result_json": sanitize_for_json(self.result_json),
            "action_data": sanitize_for_json(self.action_data),
            "failure_reason": self.failure_reason,
        }

    def _append_message_event(self, event_type, **payload):
        self.ensure_one()
        if not self.message_id:
            return
        debug_info = dict(self.message_id.debug_info or {})
        turn_events = list(debug_info.get("turn_events") or [])
        event = {
            "type": event_type,
            "timestamp": fields.Datetime.now().isoformat(),
        }
        event.update(sanitize_for_json(payload))
        turn_events.append(event)
        debug_info["turn_events"] = turn_events
        self.message_id.write({"debug_info": sanitize_for_json(debug_info)})

    def _post_ai_write_audit_note(self, records, operation):
        self.ensure_one()
        if not records or not hasattr(records, "message_post") or "message_ids" not in records._fields:
            return

        operation_labels = {
            "create": _("created"),
            "update": _("updated"),
            "archive": _("archived"),
        }
        operation_label = operation_labels.get(operation, _("changed"))
        body = _("This record was %s via AI Assistance.", operation_label)
        if self.approver_id and self.approver_id != self.requester_id:
            body = _(
                "This record was %s via AI Assistance from an approved request by %s.",
                operation_label,
                self.approver_id.name,
            )

        for record in records:
            record.message_post(
                body=body,
                message_type="comment",
                subtype_xmlid="mail.mt_note",
            )

    @api.model
    def _get_policy_for_model(self, model_name):
        company = self.env.company
        return self.env["sh.ai.write.policy"].sudo().search(
            [
                ("model_name", "=", model_name),
                "|",
                ("company_id", "=", company.id),
                ("company_id", "=", False),
            ],
            order="company_id desc, id desc",
            limit=1,
        )

    @api.model
    def _is_operation_allowed(self, policy, operation):
        if not policy:
            return True
        return not bool(
            (operation == "create" and policy.block_create)
            or (operation == "update" and policy.block_update)
            or (operation == "archive" and policy.block_archive)
        )

    @api.model
    def _get_write_target_model(self, model_name):
        if not model_name:
            return None
        if model_name in BLOCKED_TECHNICAL_MODEL_NAMES or model_name.startswith(BLOCKED_TECHNICAL_MODEL_PREFIXES):
            return None
        try:
            Model = self.env[model_name]
        except Exception:
            return None
        if getattr(Model, "_transient", False):
            return None
        return Model

    @api.model
    def _supports_archive(self, Model):
        active_field = getattr(Model, "_active_name", "active")
        return bool(active_field and active_field in Model._fields and hasattr(Model, "action_archive"))

    @api.model
    def _coerce_domain(self, domain):
        if domain is None:
            return []
        if isinstance(domain, str):
            return []
        return domain if isinstance(domain, list) else []

    @api.model
    def _normalize_target_domain(self, Model, domain):
        issues = []
        normalized = []
        for clause in domain or []:
            if not isinstance(clause, (list, tuple)) or len(clause) != 3:
                normalized.append(clause)
                continue
            field_name, operator, value = clause
            path_info = _resolve_field_path(self.env, Model._name, field_name)
            if not path_info:
                normalized.append(list(clause))
                continue
            field_type = path_info.get("field_type")
            relation_model = path_info.get("relation_model")
            if field_type not in ("many2one", "many2many") or not relation_model:
                normalized.append(list(clause))
                continue
            resolved_value, resolution_issue = self._resolve_relational_domain_value(
                relation_model,
                field_name,
                operator,
                value,
                field_type,
            )
            if resolution_issue:
                issues.append(resolution_issue)
                continue
            normalized.append([field_name, resolved_value["operator"], resolved_value["value"]])
        return normalized, issues

    @api.model
    def _resolve_relational_domain_value(self, relation_model, field_name, operator, value, field_type):
        supported_operators = {"=", "!=", "in", "not in"}
        if operator not in supported_operators:
            return {"operator": operator, "value": value}, None

        raw_values = value if isinstance(value, list) else [value]
        resolved_ids = []
        options = []
        for raw_value in raw_values:
            if isinstance(raw_value, int):
                resolved_ids.append(raw_value)
                continue
            if not isinstance(raw_value, str):
                return {"operator": operator, "value": value}, None

            lookup = fuzzy_lookup(self.env, relation_model, raw_value, limit=MAX_RESOLUTION_CANDIDATES)
            candidates = lookup.get("results", [])
            selected, resolution_state = _pick_unique_lookup_result(candidates, raw_value)
            if not selected:
                options = [candidate.get("display_name") for candidate in candidates]
                return None, {
                    "code": "ambiguous_relation_domain" if resolution_state == "ambiguous" else "unresolved_relation_domain",
                    "message": "I need a clearer target before I can prepare this write request.",
                    "field": field_name,
                    "value": raw_value,
                    "options": options,
                }
            resolved_ids.append(selected["id"])

        if field_type == "many2one":
            if operator in ("in", "not in"):
                normalized_value = resolved_ids
            else:
                normalized_value = resolved_ids[0]
            normalized_operator = operator
        else:
            normalized_value = resolved_ids
            normalized_operator = "in" if operator in ("=", "in") else "not in"

        return {"operator": normalized_operator, "value": normalized_value}, None

    @api.model
    def _find_missing_required_fields(self, Model, normalized_values):
        candidate_fields = [
            name
            for name, field in Model._fields.items()
            if field.required and not field.readonly and not field.compute and not field.inherited
        ]
        defaults = Model.default_get(candidate_fields)
        missing = []
        for field_name in candidate_fields:
            if field_name in normalized_values:
                continue
            if field_name in defaults and defaults[field_name] not in (False, None, "", []):
                continue
            missing.append(
                {
                    "field": field_name,
                    "label": Model._fields[field_name].string or field_name,
                }
            )
        return missing

    @api.model
    def _validate_fields_allowed(self, Model, normalized_values, policy):
        blocked_fields = set((policy.blocked_field_ids if policy else self.env["sh.ai.write.policy.field"]).mapped("field_name"))
        for field_name in normalized_values:
            field = Model._fields.get(field_name)
            if not field:
                raise ValidationError("Invalid field '%s' on model '%s'." % (field_name, Model._name))
            if field_name in blocked_fields:
                raise AccessError("Field '%s' is restricted for AI write actions." % field_name)
            Model.check_field_access_rights("write", [field_name])

    @api.model
    def _normalize_write_values(self, Model, raw_values, policy, operation="update", depth=0):
        normalized = {}
        previews = {}
        issues = []

        blocked_fields = set((policy.blocked_field_ids if policy else self.env["sh.ai.write.policy.field"]).mapped("field_name"))
        for field_name, raw_value in (raw_values or {}).items():
            field = Model._fields.get(field_name)
            if not field:
                issues.append(
                    {
                        "code": "unknown_field",
                        "message": "I cannot prepare this write because one of the requested fields does not exist.",
                        "field": field_name,
                    }
                )
                continue
            if field_name in blocked_fields:
                issues.append(
                    {
                        "code": "field_restricted",
                        "message": "This field is restricted from AI write actions.",
                        "field": field_name,
                        "label": field.string or field_name,
                    }
                )
                continue
            if field.type in UNSUPPORTED_FIELD_TYPES:
                issues.append(
                    {
                        "code": "unsupported_field_type",
                        "message": "Binary and file-style fields are intentionally blocked from AI write actions.",
                        "field": field_name,
                        "label": field.string or field_name,
                    }
                )
                continue
            try:
                Model.check_field_access_rights("write", [field_name])
            except AccessError:
                issues.append(
                    {
                        "code": "field_access_denied",
                        "message": "You do not have permission to modify this field.",
                        "field": field_name,
                        "label": field.string or field_name,
                    }
                )
                continue

            try:
                normalized_value, preview_value = self._normalize_single_field_value(
                    Model,
                    field_name,
                    raw_value,
                    policy,
                    operation=operation,
                    depth=depth,
                )
                normalized[field_name] = normalized_value
                previews[field_name] = preview_value
            except ValidationError as error:
                issues.append(
                    {
                        "code": "field_validation_error",
                        "message": str(error),
                        "field": field_name,
                        "label": field.string or field_name,
                    }
                )

        return normalized, previews, issues

    @api.model
    def _normalize_single_field_value(self, Model, field_name, raw_value, policy, operation="update", depth=0):
        field = Model._fields[field_name]
        if field.type in SUPPORTED_SCALAR_FIELD_TYPES:
            normalized = self._normalize_scalar_value(field, raw_value)
            return normalized, self._render_scalar_preview(field, normalized)

        if field.type == "many2one":
            resolved_id, resolved_label = self._resolve_relational_value(field.comodel_name, raw_value)
            return resolved_id, resolved_label

        if field.type == "many2many":
            commands, preview = self._normalize_many2many_value(field, raw_value)
            return {"__commands__": commands}, preview

        if field.type == "one2many":
            commands, preview = self._normalize_one2many_value(field, raw_value, depth=depth + 1)
            return {"__commands__": commands}, preview

        raise ValidationError("Field '%s' has unsupported type '%s' for AI write actions." % (field.string or field_name, field.type))

    @api.model
    def _normalize_scalar_value(self, field, raw_value):
        if field.type in ("char", "text", "html"):
            if raw_value is None:
                return False
            return str(raw_value)
        if field.type == "boolean":
            if isinstance(raw_value, bool):
                return raw_value
            if isinstance(raw_value, str):
                lowered = raw_value.strip().lower()
                if lowered in {"true", "1", "yes", "y", "active"}:
                    return True
                if lowered in {"false", "0", "no", "n", "inactive"}:
                    return False
            raise ValidationError("Boolean fields must use true/false style values.")
        if field.type in ("integer",):
            try:
                return int(raw_value)
            except Exception as error:
                raise ValidationError("Integer fields require a whole number.") from error
        if field.type in ("float", "monetary"):
            try:
                return float(raw_value)
            except Exception as error:
                raise ValidationError("Numeric fields require a number.") from error
        if field.type == "date":
            try:
                return fields.Date.to_string(fields.Date.to_date(raw_value))
            except Exception as error:
                raise ValidationError("Date fields require an ISO date like 2026-03-07.") from error
        if field.type == "datetime":
            try:
                return fields.Datetime.to_string(fields.Datetime.to_datetime(raw_value))
            except Exception as error:
                raise ValidationError("Datetime fields require an ISO datetime value.") from error
        if field.type == "selection":
            selection = dict(field.selection)
            if raw_value in selection:
                return raw_value
            lowered = str(raw_value).strip().lower()
            for key, label in selection.items():
                if str(label).strip().lower() == lowered:
                    return key
            raise ValidationError("Selection fields must use one of the allowed values or labels.")
        raise ValidationError("Field type '%s' is not supported." % field.type)

    @api.model
    def _render_scalar_preview(self, field, value):
        if value in (False, None):
            return "Empty"
        if field.type == "boolean":
            return "Yes" if value else "No"
        if field.type == "selection":
            return dict(field.selection).get(value, value)
        return str(value)

    @api.model
    def _resolve_relational_value(self, relation_model, raw_value):
        if isinstance(raw_value, dict):
            if raw_value.get("id"):
                record = self.env[relation_model].search([("id", "=", int(raw_value["id"]))], limit=1)
                if not record:
                    raise ValidationError("The related record ID '%s' does not exist or is not accessible." % raw_value["id"])
                record.check_access_rights("read")
                record.check_access_rule("read")
                return record.id, record.display_name
            if raw_value.get("name"):
                raw_value = raw_value["name"]
        if isinstance(raw_value, int):
            record = self.env[relation_model].search([("id", "=", raw_value)], limit=1)
            if not record:
                raise ValidationError("The related record ID '%s' does not exist or is not accessible." % raw_value)
            record.check_access_rights("read")
            record.check_access_rule("read")
            return record.id, record.display_name
        if isinstance(raw_value, str):
            lookup = fuzzy_lookup(self.env, relation_model, raw_value, limit=MAX_RESOLUTION_CANDIDATES)
            candidates = lookup.get("results", [])
            selected, resolution_state = _pick_unique_lookup_result(candidates, raw_value)
            if not selected:
                raise ValidationError(
                    "I need a more specific value for '%s'. Options found: %s"
                    % (
                        raw_value,
                        ", ".join(candidate.get("display_name") for candidate in candidates) or "no matching records",
                    )
                )
            return selected["id"], selected["display_name"]
        raise ValidationError("Relational fields must use a record name or ID.")

    @api.model
    def _normalize_many2many_value(self, field, raw_value):
        commands = []
        preview_labels = []
        if isinstance(raw_value, dict):
            if raw_value.get("set") is not None:
                ids, labels = self._resolve_relational_list(field.comodel_name, raw_value.get("set"))
                commands.append({"op": "set", "ids": ids})
                preview_labels.append("Set to: %s" % ", ".join(labels))
            for key in ("add", "link"):
                if raw_value.get(key):
                    ids, labels = self._resolve_relational_list(field.comodel_name, raw_value.get(key))
                    commands.extend({"op": "link", "id": record_id} for record_id in ids)
                    preview_labels.append("Add: %s" % ", ".join(labels))
            blocked_keys = {"remove", "unlink", "delete", "clear"} & set(raw_value.keys())
            if blocked_keys:
                raise ValidationError("Destructive many2many operations are blocked for AI write actions.")
        else:
            ids, labels = self._resolve_relational_list(field.comodel_name, raw_value)
            commands.append({"op": "set", "ids": ids})
            preview_labels.append(", ".join(labels))
        if not commands:
            raise ValidationError("Many2many values must contain set/add/link instructions.")
        return commands, "; ".join(preview_labels)

    @api.model
    def _normalize_one2many_value(self, field, raw_value, depth=1):
        if depth > 2:
            raise ValidationError("Nested one2many writes are too deep for a safe AI proposal.")

        relation_model = self.env[field.comodel_name]
        commands = []
        preview_parts = []

        def normalize_child_values(values, child_operation):
            child_policy = self._get_policy_for_model(relation_model._name)
            if not self._is_operation_allowed(child_policy, child_operation):
                raise ValidationError("The related model '%s' is restricted for AI %s actions." % (relation_model._description, child_operation))
            normalized_child_values, child_previews, child_issues = self._normalize_write_values(
                relation_model,
                values,
                child_policy,
                operation=child_operation,
                depth=depth,
            )
            if child_issues:
                raise ValidationError(child_issues[0]["message"])
            return normalized_child_values, child_previews

        if isinstance(raw_value, list):
            if all(isinstance(item, dict) for item in raw_value):
                for item in raw_value:
                    normalized_child, child_previews = normalize_child_values(item, "create")
                    commands.append({"op": "create", "values": normalized_child})
                    preview_parts.append("Create line: %s" % self._format_child_preview(child_previews))
            else:
                ids, labels = self._resolve_relational_list(field.comodel_name, raw_value)
                commands.append({"op": "set", "ids": ids})
                preview_parts.append("Set lines: %s" % ", ".join(labels))
            return commands, "; ".join(preview_parts)

        if not isinstance(raw_value, dict):
            raise ValidationError("One2many values must use create/update/link/set instructions.")

        if raw_value.get("set") is not None:
            ids, labels = self._resolve_relational_list(field.comodel_name, raw_value.get("set"))
            commands.append({"op": "set", "ids": ids})
            preview_parts.append("Set lines: %s" % ", ".join(labels))

        if raw_value.get("link"):
            ids, labels = self._resolve_relational_list(field.comodel_name, raw_value.get("link"))
            commands.extend({"op": "link", "id": record_id} for record_id in ids)
            preview_parts.append("Link lines: %s" % ", ".join(labels))

        for item in raw_value.get("create", []) or []:
            if not isinstance(item, dict):
                raise ValidationError("One2many create instructions must be objects.")
            normalized_child, child_previews = normalize_child_values(item, "create")
            commands.append({"op": "create", "values": normalized_child})
            preview_parts.append("Create line: %s" % self._format_child_preview(child_previews))

        for item in raw_value.get("update", []) or []:
            if not isinstance(item, dict) or not item.get("id") or not isinstance(item.get("values"), dict):
                raise ValidationError("One2many update instructions must include both 'id' and 'values'.")
            line_id, _label = self._resolve_relational_value(field.comodel_name, item["id"])
            normalized_child, child_previews = normalize_child_values(item["values"], "update")
            commands.append({"op": "update", "id": line_id, "values": normalized_child})
            preview_parts.append("Update line %s: %s" % (line_id, self._format_child_preview(child_previews)))

        blocked_keys = {"remove", "unlink", "delete", "clear"} & set(raw_value.keys())
        if blocked_keys:
            raise ValidationError("Destructive one2many operations are blocked for AI write actions.")

        if not commands:
            raise ValidationError("One2many values must contain set/link/create/update instructions.")
        return commands, "; ".join(preview_parts)

    @api.model
    def _format_child_preview(self, previews):
        return ", ".join("%s=%s" % (key, value) for key, value in previews.items()) if previews else "No line values"

    @api.model
    def _resolve_relational_list(self, relation_model, raw_value):
        values = raw_value if isinstance(raw_value, list) else [raw_value]
        ids = []
        labels = []
        for item in values:
            record_id, display_name = self._resolve_relational_value(relation_model, item)
            ids.append(record_id)
            labels.append(display_name)
        return ids, labels

    @api.model
    def _materialize_write_values(self, Model, serialized_values):
        orm_values = {}
        for field_name, serialized_value in (serialized_values or {}).items():
            field = Model._fields[field_name]
            if field.type in SUPPORTED_SCALAR_FIELD_TYPES or field.type == "many2one":
                orm_values[field_name] = serialized_value
                continue
            if field.type in ("many2many", "one2many"):
                orm_values[field_name] = self._materialize_commands(serialized_value)
                continue
            raise ValidationError("Field '%s' cannot be materialized for write execution." % field_name)
        return orm_values

    @api.model
    def _materialize_commands(self, serialized_value):
        commands = []
        for command in (serialized_value or {}).get("__commands__", []):
            operation = command.get("op")
            if operation == "set":
                commands.append(fields.Command.set(command.get("ids", [])))
            elif operation == "link":
                commands.append(fields.Command.link(command["id"]))
            elif operation == "create":
                commands.append(fields.Command.create(self._materialize_nested_dict(command.get("values", {}))))
            elif operation == "update":
                commands.append(
                    fields.Command.update(
                        command["id"],
                        self._materialize_nested_dict(command.get("values", {})),
                    )
                )
            else:
                raise ValidationError("Unsupported write command '%s'." % operation)
        return commands

    @api.model
    def _materialize_nested_dict(self, values):
        result = {}
        for key, value in (values or {}).items():
            if isinstance(value, dict) and "__commands__" in value:
                result[key] = self._materialize_commands(value)
            else:
                result[key] = value
        return result

    @api.model
    def _build_create_preview(self, Model, normalized_values, value_previews):
        return {
            "operation": "create",
            "operation_label": "Create",
            "model": Model._name,
            "model_display_name": self._get_model_display_name(Model._name),
            "target_count": 1,
            "risk_badges": self._build_risk_badges(Model, normalized_values, target_count=1, operation="create"),
            "field_diffs": self._build_create_field_diffs(Model, value_previews),
            "sample_records": [
                {
                    "display_name": "New record preview",
                    "after": value_previews,
                }
            ],
        }

    @api.model
    def _build_update_preview(self, Model, records, normalized_values, value_previews):
        sample_records = []
        sample = records[:PREVIEW_SAMPLE_LIMIT]
        for record in sample:
            sample_records.append(
                {
                    "id": record.id,
                    "display_name": record.display_name,
                    "before": {
                        field_name: self._serialize_record_field_value(record, field_name)
                        for field_name in normalized_values
                    },
                    "after": value_previews,
                }
            )
        return {
            "operation": "update",
            "operation_label": "Update",
            "model": Model._name,
            "model_display_name": self._get_model_display_name(Model._name),
            "target_count": len(records),
            "risk_badges": self._build_risk_badges(Model, normalized_values, target_count=len(records), operation="update"),
            "field_diffs": self._build_update_field_diffs(Model, records, normalized_values, value_previews),
            "sample_records": sample_records,
        }

    @api.model
    def _build_archive_preview(self, Model, records):
        sample = records[:PREVIEW_SAMPLE_LIMIT]
        return {
            "operation": "archive",
            "operation_label": "Archive",
            "model": Model._name,
            "model_display_name": self._get_model_display_name(Model._name),
            "target_count": len(records),
            "risk_badges": self._build_risk_badges(Model, {}, target_count=len(records), operation="archive"),
            "field_diffs": [
                {
                    "field": getattr(Model, "_active_name", "active"),
                    "label": "Archive Status",
                    "type": "boolean",
                    "before_summary": "Active",
                    "after_summary": "Archived",
                }
            ],
            "sample_records": [
                {
                    "id": record.id,
                    "display_name": record.display_name,
                    "before": {"status": "Active"},
                    "after": {"status": "Archived"},
                }
                for record in sample
            ],
        }

    @api.model
    def _build_create_field_diffs(self, Model, value_previews):
        return [
            {
                "field": field_name,
                "label": Model._fields[field_name].string or field_name,
                "type": Model._fields[field_name].type,
                "before_summary": "Not set",
                "after_summary": preview,
            }
            for field_name, preview in value_previews.items()
        ]

    @api.model
    def _build_update_field_diffs(self, Model, records, normalized_values, value_previews):
        diffs = []
        for field_name in normalized_values:
            field = Model._fields[field_name]
            diffs.append(
                {
                    "field": field_name,
                    "label": field.string or field_name,
                    "type": field.type,
                    "before_summary": self._summarize_before_values(records, field_name),
                    "after_summary": value_previews.get(field_name),
                }
            )
        return diffs

    @api.model
    def _build_risk_badges(self, Model, normalized_values, target_count=1, operation="update"):
        badges = []
        if target_count > 1:
            badges.append({"code": "multi_record", "label": "Multiple records"})
        if operation == "archive":
            badges.append({"code": "archive", "label": "Archive"})
        if any(Model._fields[field_name].type in RELATIONAL_FIELD_TYPES for field_name in normalized_values):
            badges.append({"code": "relational_write", "label": "Relational write"})
        if any(Model._fields[field_name].type == "one2many" for field_name in normalized_values):
            badges.append({"code": "line_update", "label": "Line update"})
        return badges

    @api.model
    def _summarize_before_values(self, records, field_name):
        values = [self._serialize_record_field_value(record, field_name) for record in records[:PREVIEW_SAMPLE_LIMIT]]
        unique_values = []
        for value in values:
            if value not in unique_values:
                unique_values.append(value)
        if not unique_values:
            return "Empty"
        if len(unique_values) == 1:
            return unique_values[0]
        preview = ", ".join(str(value) for value in unique_values[:3])
        suffix = "" if len(unique_values) <= 3 else " ..."
        return "%s value(s): %s%s" % (len(unique_values), preview, suffix)

    @api.model
    def _serialize_record_field_value(self, record, field_name):
        field = record._fields[field_name]
        value = record[field_name]
        if field.type == "many2one":
            return value.display_name if value else "Empty"
        if field.type in ("many2many", "one2many"):
            names = value[:5].mapped("display_name")
            if len(value) > 5:
                return "%s (+%s more)" % (", ".join(names), len(value) - 5)
            return ", ".join(names) if names else "Empty"
        if field.type == "selection":
            return dict(field.selection).get(value, value or "Empty")
        if field.type == "boolean":
            return "Yes" if value else "No"
        return str(value) if value not in (False, None, "") else "Empty"

    @api.model
    def _get_model_display_name(self, model_name):
        try:
            Model = self.env[model_name]
            return Model._description or model_name
        except Exception:
            ir_model = self.env["ir.model"].sudo().search([("model", "=", model_name)], limit=1)
            return ir_model.display_name or model_name

    @api.model
    def _build_target_action_data(self, model_name, record_ids, operation="update"):
        record_ids = [int(record_id) for record_id in (record_ids or [])]
        if not record_ids:
            return None
        context = {}
        if operation == "archive":
            context["active_test"] = False
        return {
            "model": model_name,
            "model_display_name": self._get_model_display_name(model_name),
            "domain": [["id", "in", record_ids]],
            "total_count": len(record_ids),
            "context": context,
        }

    @api.model
    def _clarification_result(self, issues):
        primary_issue = issues[0] if issues else {}
        return {
            "success": False,
            "needs_clarification": True,
            "message": primary_issue.get("message") or "I need more detail before I can prepare this write request.",
            "issues": sanitize_for_json(issues),
        }

    @api.model
    def _error_result(self, message, **extra):
        payload = {"success": False, "error": message}
        payload.update(extra)
        return payload
