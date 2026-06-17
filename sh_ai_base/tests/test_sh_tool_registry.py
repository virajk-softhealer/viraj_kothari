# -*- coding: utf-8 -*-
# Part of SoftHealer Technologies PVT.LTD.

from odoo.tests.common import TransactionCase

from odoo.addons.sh_ai_base.provider.sh_tool_registry import (
    execute_sh_ai_tool,
    get_sh_ai_mcp_tool_definitions,
    get_sh_ai_tool_metadata,
    get_sh_ai_tool_names,
)


class TestShToolRegistry(TransactionCase):

    def test_remote_safe_tool_names_exclude_internal_navigation_tools(self):
        tool_names = get_sh_ai_tool_names(remote_safe_only=True)

        self.assertIn('search_records', tool_names)
        self.assertIn('aggregate_records', tool_names)
        self.assertIn('create_record', tool_names)
        self.assertIn('update_record', tool_names)
        self.assertIn('archive_record', tool_names)
        self.assertIn('delete_record', tool_names)
        self.assertIn('execute_record_action', tool_names)
        self.assertIn('submit_action_wizard', tool_names)
        self.assertIn('chart_creation', tool_names)
        self.assertIn('chart_update', tool_names)
        self.assertIn('dashboard_creation', tool_names)
        self.assertIn('dashboard_update', tool_names)
        self.assertIn('dashboard_remove_charts', tool_names)
        self.assertIn('dashboard_replace_chart', tool_names)
        self.assertIn('code_search', tool_names)
        self.assertIn('code_read', tool_names)
        self.assertNotIn('open_view', tool_names)

    def test_execute_registry_tool_uses_existing_implementation(self):
        result = execute_sh_ai_tool(self.env, 'get_current_date_info')

        self.assertTrue(result['success'])
        self.assertIn('current_date', result)
        self.assertIn('periods', result)

    def test_mcp_tool_definitions_reuse_existing_declarations(self):
        tools = get_sh_ai_mcp_tool_definitions()
        search_tool = next(tool for tool in tools if tool['name'] == 'search_records')
        create_tool = next(tool for tool in tools if tool['name'] == 'create_record')
        update_tool = next(tool for tool in tools if tool['name'] == 'update_record')
        archive_tool = next(tool for tool in tools if tool['name'] == 'archive_record')
        delete_tool = next(tool for tool in tools if tool['name'] == 'delete_record')
        execute_action_tool = next(tool for tool in tools if tool['name'] == 'execute_record_action')
        submit_wizard_tool = next(tool for tool in tools if tool['name'] == 'submit_action_wizard')
        chart_tool = next(tool for tool in tools if tool['name'] == 'chart_creation')
        chart_update_tool = next(tool for tool in tools if tool['name'] == 'chart_update')
        dashboard_create_tool = next(tool for tool in tools if tool['name'] == 'dashboard_creation')
        dashboard_update_tool = next(tool for tool in tools if tool['name'] == 'dashboard_update')
        dashboard_remove_tool = next(tool for tool in tools if tool['name'] == 'dashboard_remove_charts')
        dashboard_replace_tool = next(tool for tool in tools if tool['name'] == 'dashboard_replace_chart')
        code_search_tool = next(tool for tool in tools if tool['name'] == 'code_search')
        code_read_tool = next(tool for tool in tools if tool['name'] == 'code_read')

        self.assertEqual(search_tool['inputSchema']['type'], 'object')
        self.assertIn('model', search_tool['inputSchema']['properties'])
        self.assertIn('values', create_tool['inputSchema']['properties'])
        self.assertIn('record_id', update_tool['inputSchema']['properties'])
        self.assertIn('record_id', archive_tool['inputSchema']['properties'])
        self.assertIn('record_id', delete_tool['inputSchema']['properties'])
        self.assertIn('action_name', execute_action_tool['inputSchema']['properties'])
        self.assertIn('wizard_session_id', submit_wizard_tool['inputSchema']['properties'])
        self.assertIn('chart_types', chart_tool['inputSchema']['properties'])
        self.assertIn('chart_id', chart_update_tool['inputSchema']['properties'])
        self.assertIn('dashboard_goal', dashboard_create_tool['inputSchema']['properties'])
        self.assertIn('chart_ids', dashboard_create_tool['inputSchema']['properties'])
        self.assertIn('table_requests', dashboard_create_tool['inputSchema']['properties'])
        self.assertIn('dashboard_id', dashboard_update_tool['inputSchema']['properties'])
        self.assertIn('table_requests', dashboard_update_tool['inputSchema']['properties'])
        self.assertIn('chart_ids', dashboard_remove_tool['inputSchema']['properties'])
        self.assertIn('replacement_chart_id', dashboard_replace_tool['inputSchema']['properties'])
        self.assertIn('query', code_search_tool['inputSchema']['properties'])
        self.assertIn('path', code_read_tool['inputSchema']['properties'])
        self.assertNotIn('open_view', [tool['name'] for tool in tools])

    def test_mcp_tool_definitions_can_be_filtered_per_connector(self):
        tools = get_sh_ai_mcp_tool_definitions(tool_names=["search_records", "get_current_date_info"])

        self.assertEqual([tool["name"] for tool in tools], ["search_records", "get_current_date_info"])

    def test_tool_metadata_exposes_remote_policy_flags(self):
        metadata = get_sh_ai_tool_metadata('open_view')

        self.assertEqual(metadata['name'], 'open_view')
        self.assertTrue(metadata['read_only'])
        self.assertFalse(metadata['remote_safe'])

    def test_mutation_tool_metadata_marks_not_read_only(self):
        metadata = get_sh_ai_tool_metadata('chart_update')

        self.assertEqual(metadata['name'], 'chart_update')
        self.assertFalse(metadata['read_only'])
        self.assertTrue(metadata['remote_safe'])

        create_metadata = get_sh_ai_tool_metadata('create_record')
        self.assertFalse(create_metadata['read_only'])
        self.assertTrue(create_metadata['remote_safe'])
