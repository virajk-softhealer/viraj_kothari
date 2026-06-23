# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies.

from unittest.mock import patch

from odoo.tests.common import TransactionCase


class TestAiOpenrouterModel(TransactionCase):

    def setUp(self):
        super().setUp()
        self.llm = self.env["sh.ai.llm"].create({
            "name": "Catalog Test",
            "sh_company": "OpenAI",
            "sh_model_code": "gpt-4o-mini",
        })
        self.catalog_model = self.env["sh.ai.openrouter.model"]

    def test_sync_keeps_only_function_calling_models(self):
        self.catalog_model.sync_from_openrouter_payload([
            {
                "id": "openai/gpt-4o-mini",
                "name": "OpenAI: GPT-4o Mini",
                "supported_parameters": ["temperature", "tools"],
                "context_length": 128000,
            },
            {
                "id": "vendor/no-tools-model",
                "name": "Vendor: No Tools Model",
                "supported_parameters": ["temperature"],
            },
        ])

        synced_model = self.catalog_model.search([("openrouter_id", "=", "openai/gpt-4o-mini")], limit=1)
        self.assertTrue(synced_model)
        self.assertEqual(synced_model.provider_name, "OpenAI")
        self.assertFalse(self.catalog_model.search([("openrouter_id", "=", "vendor/no-tools-model")], limit=1))

    def test_sync_upserts_existing_model_without_duplicates(self):
        self.catalog_model.sync_from_openrouter_payload([
            {
                "id": "openai/gpt-4o-mini",
                "name": "OpenAI: GPT-4o Mini",
                "supported_parameters": ["tools"],
                "context_length": 128000,
            },
        ])
        self.catalog_model.sync_from_openrouter_payload([
            {
                "id": "openai/gpt-4o-mini",
                "name": "OpenAI: GPT-4o Mini Updated",
                "supported_parameters": ["tools", "temperature"],
                "context_length": 256000,
            },
        ])

        records = self.catalog_model.search([("openrouter_id", "=", "openai/gpt-4o-mini")])
        self.assertEqual(len(records), 1)
        self.assertEqual(records.name, "OpenAI: GPT-4o Mini")
        self.assertEqual(records.context_length, 128000)

    def test_sync_creates_only_new_models_on_second_fetch(self):
        first_result = self.catalog_model.sync_from_openrouter_payload([
            {
                "id": "openai/gpt-4o-mini",
                "name": "OpenAI: GPT-4o Mini",
                "supported_parameters": ["tools"],
            },
            {
                "id": "google/gemini-2.5-flash",
                "name": "Google: Gemini 2.5 Flash",
                "supported_parameters": ["tools"],
            },
        ])
        second_result = self.catalog_model.sync_from_openrouter_payload([
            {
                "id": "openai/gpt-4o-mini",
                "name": "OpenAI: GPT-4o Mini Updated",
                "supported_parameters": ["tools"],
            },
            {
                "id": "google/gemini-2.5-flash",
                "name": "Google: Gemini 2.5 Flash Updated",
                "supported_parameters": ["tools"],
            },
            {
                "id": "anthropic/claude-sonnet-4",
                "name": "Anthropic: Claude Sonnet 4",
                "supported_parameters": ["tools"],
            },
            {
                "id": "meta/llama-4",
                "name": "Meta: Llama 4",
                "supported_parameters": ["tools"],
            },
        ])

        self.assertEqual(first_result["created_count"], 2)
        self.assertEqual(second_result["created_count"], 2)
        self.assertEqual(second_result["existing_count"], 2)
        self.assertEqual(self.catalog_model.search_count([]), 4)

        openai_record = self.catalog_model.search([("model_code", "=", "openai/gpt-4o-mini")], limit=1)
        self.assertEqual(openai_record.name, "OpenAI: GPT-4o Mini")

    def test_openrouter_model_onchange_updates_provider_and_model_code(self):
        openrouter_model = self.catalog_model.create({
            "name": "Anthropic: Claude Sonnet",
            "openrouter_id": "anthropic/claude-sonnet-4",
            "provider_name": "Anthropic",
            "model_code": "anthropic/claude-sonnet-4",
        })

        self.llm.provider_type = "openrouter"
        self.llm.openrouter_model_id = openrouter_model
        self.llm._onchange_openrouter_model_id()

        self.assertEqual(self.llm.sh_company, "Anthropic")
        self.assertEqual(self.llm.sh_model_code, "anthropic/claude-sonnet-4")

    def test_direct_provider_type_keeps_existing_flow_defaults(self):
        llm = self.env["sh.ai.llm"].create({
            "name": "Direct Model",
            "provider_type": "direct",
            "sh_company": "Google",
            "sh_model_code": "gemini-2.5-flash",
        })

        self.assertEqual(llm.provider_type, "direct")
        self.assertFalse(llm.openrouter_model_id)
        self.assertEqual(llm.sh_company, "Google")
        self.assertEqual(llm.sh_model_code, "gemini-2.5-flash")

    def test_verify_api_key_routes_deepseek_and_claude_providers(self):
        deepseek_llm = self.env["sh.ai.llm"].create({
            "name": "DeepSeek Model",
            "sh_company": "DeepSeek",
            "sh_model_code": "deepseek-v4-flash",
        })
        claude_llm = self.env["sh.ai.llm"].create({
            "name": "Claude Model",
            "sh_company": "Anthropic",
            "sh_model_code": "claude-sonnet-4-6",
        })

        with patch.object(type(deepseek_llm), "_verify_deepseek_key", return_value={"success": True, "message": "ok"}) as mocked_deepseek:
            result = deepseek_llm._verify_api_key("DeepSeek", "fake-deepseek-key")
            self.assertTrue(result["success"])
            mocked_deepseek.assert_called_once()

        with patch.object(type(claude_llm), "_verify_claude_key", return_value={"success": True, "message": "ok"}) as mocked_claude:
            result = claude_llm._verify_api_key("Anthropic", "fake-claude-key")
            self.assertTrue(result["success"])
            mocked_claude.assert_called_once()

    def test_action_fetch_openrouter_models_returns_success_notification(self):
        with patch.object(type(self.llm), "_fetch_openrouter_models_payload", return_value=[
            {
                "id": "openai/gpt-4o-mini",
                "name": "OpenAI: GPT-4o Mini",
                "supported_parameters": ["tools"],
            },
        ]):
            result = self.llm.action_fetch_openrouter_models()

        self.assertEqual(result["type"], "ir.actions.client")
        self.assertEqual(result["tag"], "display_notification")
        self.assertEqual(result["params"]["type"], "success")
        self.assertIn("Added 1", result["params"]["message"])

    def test_get_onboarding_providers_includes_openrouter(self):
        self.env["sh.ai.llm"].create({
            "name": "OpenRouter",
            "provider_type": "openrouter",
            "sh_company": "OpenRouter",
        })

        providers = self.env["sh.ai.llm"].get_onboarding_providers()
        companies = {provider["company"] for provider in providers}

        self.assertIn("OpenRouter", companies)

    def test_save_onboarding_api_key_fetches_openrouter_models(self):
        openrouter_llm = self.env["sh.ai.llm"].create({
            "name": "OpenRouter",
            "provider_type": "openrouter",
            "sh_company": "OpenRouter",
        })

        with patch.object(type(openrouter_llm), "_verify_api_key", return_value={"success": True, "message": "ok"}), \
             patch.object(type(openrouter_llm), "_fetch_openrouter_models_payload", return_value=[
                 {
                     "id": "openai/gpt-4o-mini",
                     "name": "OpenAI: GPT-4o Mini",
                     "supported_parameters": ["tools"],
                 },
             ]):
            result = self.env["sh.ai.llm"].save_onboarding_api_key("OpenRouter", "test-openrouter-key")

        openrouter_llm.invalidate_recordset()
        self.assertTrue(result["success"])
        self.assertTrue(result["models"])
        self.assertEqual(result["models"][0]["model_code"], "openai/gpt-4o-mini")
        self.assertEqual(openrouter_llm.sh_api_key, "test-openrouter-key")

    def test_save_onboarding_default_model_assigns_openrouter_catalog_model(self):
        openrouter_llm = self.env["sh.ai.llm"].create({
            "name": "OpenRouter",
            "provider_type": "openrouter",
            "sh_company": "OpenRouter",
        })
        openrouter_model = self.catalog_model.create({
            "name": "OpenAI: GPT-4o Mini",
            "openrouter_id": "openai/gpt-4o-mini",
            "provider_name": "OpenAI",
            "model_code": "openai/gpt-4o-mini",
        })
        self.env["ir.config_parameter"].sudo().set_param("sh_ai_assistance.onboarding_provider", "OpenRouter")

        result = self.env["sh.ai.llm"].save_onboarding_default_model(openrouter_model.id)

        openrouter_llm.invalidate_recordset()
        self.assertTrue(result["success"])
        self.assertEqual(openrouter_llm.openrouter_model_id, openrouter_model)
        self.assertTrue(openrouter_llm.is_default)
