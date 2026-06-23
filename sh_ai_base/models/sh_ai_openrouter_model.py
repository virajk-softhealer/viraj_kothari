# -*- coding: utf-8 -*-
# Part of SoftHealer Technologies PVT.LTD.

from odoo import fields, models


class ShAiOpenrouterModel(models.Model):
    _name = "sh.ai.openrouter.model"
    _description = "OpenRouter Model Catalog"
    _order = "provider_name, name, id"

    name = fields.Char(string="Model Name", required=True)
    openrouter_id = fields.Char(string="OpenRouter ID", required=True, index=True)
    provider_name = fields.Char(string="Provider", required=True)
    model_code = fields.Char(string="Model Code", required=True)
    supported_parameters = fields.Json(string="Supported Parameters")
    context_length = fields.Integer(string="Context Length")
    active = fields.Boolean(string="Active", default=True)

    _sql_constraints = [
        ("openrouter_id_unique", "UNIQUE(openrouter_id)", "This OpenRouter model already exists."),
    ]

    def _extract_provider_name(self, payload):
        name = (payload.get("name") or "").strip()
        if ":" in name:
            return name.split(":", 1)[0].strip()

        identifier = (payload.get("canonical_slug") or payload.get("id") or "").strip()
        provider_slug = identifier.split("/", 1)[0] if "/" in identifier else identifier
        provider_slug = (provider_slug or "OpenRouter").replace("-", " ").replace("_", " ").strip()
        return " ".join(word.capitalize() for word in provider_slug.split())

    def _prepare_openrouter_model_values(self, payload):
        supported_parameters = payload.get("supported_parameters") or []
        if not isinstance(supported_parameters, list):
            supported_parameters = []

        model_code = (payload.get("id") or payload.get("canonical_slug") or "").strip()
        model_name = (payload.get("name") or model_code or "OpenRouter Model").strip()

        return {
            "name": model_name,
            "openrouter_id": model_code,
            "provider_name": self._extract_provider_name(payload),
            "model_code": model_code,
            "supported_parameters": supported_parameters,
            "context_length": int(payload.get("context_length") or 0),
            "active": True,
        }

    def sync_from_openrouter_payload(self, payloads):
        payloads = payloads or []
        created_count = 0
        existing_count = 0
        skipped_count = 0

        for payload in payloads:
            if not isinstance(payload, dict):
                skipped_count += 1
                continue

            supported_parameters = payload.get("supported_parameters") or []
            normalized_parameters = {
                str(parameter).strip().lower()
                for parameter in supported_parameters
                if parameter
            }
            if "tools" not in normalized_parameters:
                skipped_count += 1
                continue

            values = self._prepare_openrouter_model_values(payload)
            model_code = values["model_code"]
            if not model_code:
                skipped_count += 1
                continue

            existing = self.search([("model_code", "=", model_code)], limit=1)
            if existing:
                existing_count += 1
                continue

            self.create(values)
            created_count += 1

        return {
            "created_count": created_count,
            "existing_count": existing_count,
            "skipped_count": skipped_count,
        }
