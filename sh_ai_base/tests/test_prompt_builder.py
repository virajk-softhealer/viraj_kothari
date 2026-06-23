# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies.

from odoo.tests.common import TransactionCase
from odoo.addons.sh_ai_base.provider.prompt_builder import PromptBuilder

class TestPromptBuilder(TransactionCase):

    def setUp(self):
        super(TestPromptBuilder, self).setUp()
        self.builder = PromptBuilder(self.env)

    def test_build_complete_prompt_structure(self):
        """Test that the prompt builder correctly assembles instructions and catalog"""
        catalog = [{'model': 'sale.order', 'display_name': 'Sales Order'}]
        instructions = {
            'context': 'USE USER TIMEZONE',
            'workflow': 'DO THIS FIRST',
            'tool': 'USE TOOLS',
            'critical': 'DANGER RULES',
        }
        
        prompt = self.builder.build_complete_prompt(
            system_prompt="YOU ARE AI",
            tool_declarations=[{'name': 'test_tool', 'description': 'testing tool'}],
            model_catalog=catalog,
            instructions=instructions
        )
        
        # Check for core sections
        self.assertIn("YOU ARE AI", prompt)
        self.assertIn("<model_catalog>", prompt)
        self.assertIn("sale.order", prompt)
        self.assertIn("DO THIS FIRST", prompt)
        self.assertIn("DANGER RULES", prompt)
        self.assertIn("USE USER TIMEZONE", prompt)
        self.assertIn("<available_tools>", prompt)
        self.assertIn("test_tool", prompt)
        self.assertIn("<shared_execution_policy>", prompt)

    def test_prompt_xml_escaping(self):
        """Test that XML tags in instructions are handled (if applicable)"""
        instructions = {'workflow': '<test>content</test>'}
        prompt = self.builder.build_complete_prompt(
            system_prompt="AI",
            tool_declarations=[],
            model_catalog=[],
            instructions=instructions
        )
        self.assertIn("<test>content</test>", prompt)

    def test_build_prompt_package_exposes_cache_metadata(self):
        """Prompt packages should separate stable prefix and dynamic suffix for cache-aware providers."""
        package = self.builder.build_prompt_package(
            system_prompt="AI SYSTEM",
            tool_declarations=[{'name': 'test_tool', 'description': 'testing tool', 'parameters': {'type': 'object', 'properties': {}}}],
            model_catalog=[{'model': 'res.users', 'display_name': 'Users'}],
            instructions={
                'workflow': 'WORKFLOW',
                'context': 'USE TIMEZONE',
                'examples': 'EXAMPLES',
            },
            is_followup=False,
        )

        self.assertIn('full_prompt', package)
        self.assertIn('stable_prefix', package)
        self.assertIn('dynamic_suffix', package)
        self.assertIn('cache_metadata', package)
        self.assertIn('AI SYSTEM', package['stable_prefix'])
        self.assertIn('USE TIMEZONE', package['stable_prefix'])
        self.assertIn('system_prompt', package['cache_metadata']['stable_section_names'])
        self.assertIn('context_instruction', package['cache_metadata']['stable_section_names'])
        self.assertIn('user_context', package['cache_metadata']['dynamic_section_names'])
        self.assertEqual(len(package['cache_metadata']['stable_prefix_hash']), 64)

    def test_build_prompt_package_accepts_extra_stable_sections(self):
        package = self.builder.build_prompt_package(
            system_prompt="AI SYSTEM",
            tool_declarations=[],
            model_catalog=[],
            instructions={},
            extra_stable_sections=[{
                'name': 'custom_guardrail',
                'content': '<custom_guardrail>USE APPROVALS</custom_guardrail>',
            }],
        )

        self.assertIn('<custom_guardrail>USE APPROVALS</custom_guardrail>', package['stable_prefix'])
        self.assertIn('custom_guardrail', package['cache_metadata']['stable_section_names'])
