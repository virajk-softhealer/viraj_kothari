# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies.

from google.genai import types
from unittest.mock import patch
from odoo.tests.common import TransactionCase
from odoo.addons.sh_ai_assistance.ai_processing.base_engine import BaseAiEngine
from odoo.addons.sh_ai_assistance.ai_processing.claude_engine import ClaudeEngine
from odoo.addons.sh_ai_assistance.ai_processing.deepseek_engine import DeepSeekEngine
from odoo.addons.sh_ai_assistance.ai_processing.engine_factory import AiEngineFactory
from odoo.addons.sh_ai_assistance.ai_processing.gemini_engine import GeminiEngine
from odoo.addons.sh_ai_assistance.ai_processing.openai_engine import OpenAiEngine
from odoo.addons.sh_ai_assistance.ai_processing.openrouter_engine import OpenRouterEngine
from odoo.addons.sh_ai_base.provider.claude_provider import ClaudeProvider
from odoo.addons.sh_ai_base.provider.gemini_provider import clone_gemini_part
from odoo.addons.sh_ai_assistance.ai_processing.utils import sanitize_for_json
from odoo.addons.sh_ai_assistance.models.sh_ai_llm import ShAiLlm as AssistantLlm
from odoo.addons.sh_ai_assistance.models.sh_ai_llm import (
    WORKFLOW_INSTRUCTION,
    TOOL_INSTRUCTION,
    LEGACY_WORKFLOW_INSTRUCTION,
    LEGACY_TOOL_INSTRUCTION,
)

class TestAiSession(TransactionCase):

    def setUp(self):
        super(TestAiSession, self).setUp()
        self.llm = self.env['sh.ai.llm'].create({
            'name': 'Test Gemini',
            'sh_company': 'Google',
            'sh_model_code': 'gemini-1.5-flash',
        })
        self.user = self.env.user

    def test_session_creation(self):
        """Test creating a new chat session"""
        session_data = self.env['sh.ai.chat.session'].create_new_session()
        self.assertTrue(session_data['id'])
        self.assertTrue(session_data['access_token'])
        
        session = self.env['sh.ai.chat.session'].browse(session_data['id'])
        self.assertEqual(session.user_id, self.user)

    def test_find_by_token(self):
        """Test finding a session by its access token"""
        session = self.env['sh.ai.chat.session'].create({
            'name': 'Token Test',
            'user_id': self.user.id
        })
        token = session.access_token
        
        found_data = self.env['sh.ai.chat.session'].find_by_token(token)
        self.assertEqual(found_data['id'], session.id)

    def test_get_model_display_names_returns_user_labels(self):
        """Chat UI fallback should resolve technical model names to readable labels."""
        labels = self.env['sh.ai.chat.message'].get_model_display_names(['res.users', 'res.partner'])
        self.assertEqual(labels['res.users'], 'User')
        self.assertEqual(labels['res.partner'], 'Contact')

    def test_message_stats_aggregation(self):
        """Test that session-level stats are aggregated from messages"""
        session = self.env['sh.ai.chat.session'].create({
            'name': 'Stats Test',
            'llm_id': self.llm.id,
            'user_id': self.user.id
        })
        
        # Create message with stats
        self.env['sh.ai.chat.message'].create({
            'session_id': session.id,
            'message_type': 'assistant',
            'content': 'Hello',
            'prompt_tokens': 100,
            'completion_tokens': 50,
            'total_tokens': 150,
            'tool_call_count': 2,
            'execution_time': 1.5
        })
        
        # Create second message
        self.env['sh.ai.chat.message'].create({
            'session_id': session.id,
            'message_type': 'assistant',
            'content': 'World',
            'prompt_tokens': 200,
            'completion_tokens': 100,
            'total_tokens': 300,
            'tool_call_count': 3,
            'execution_time': 2.5
        })
        
        # Trigger computation
        session._compute_message_stats()
        
        self.assertEqual(session.total_prompt_tokens, 300)
        self.assertEqual(session.total_completion_tokens, 150)
        self.assertEqual(session.total_session_tokens, 450)
        self.assertEqual(session.total_tool_calls, 5)
        self.assertEqual(session.total_execution_time, 4.0)

    def test_base_engine_finalize_action_data_auto_show_hide(self):
        """Ensure shared action-data finalizer works consistently for all providers."""
        engine = BaseAiEngine(self.env)

        last_search_call = {
            'model': 'res.users',
            'domain': [],
            'fields': ['name'],
            'operation': 'search',
            'group_by': None,
        }

        # AUTO should require truncation (total_count > count)
        truncated_result = {'success': True, 'count': 10, 'total_count': 42}
        action_auto = engine._finalize_action_data(None, last_search_call, truncated_result, view_preference='auto')
        self.assertTrue(action_auto)
        self.assertEqual(action_auto.get('model'), 'res.users')
        self.assertEqual(action_auto.get('model_display_name'), 'User')

        # AUTO non-truncated should not force actions
        non_truncated_result = {'success': True, 'count': 5, 'total_count': 5}
        action_auto_none = engine._finalize_action_data(None, last_search_call, non_truncated_result, view_preference='auto')
        self.assertFalse(action_auto_none)

        # SHOW should force actions even without truncation
        action_show = engine._finalize_action_data(None, last_search_call, non_truncated_result, view_preference='show')
        self.assertTrue(action_show)
        self.assertEqual(action_show.get('total_count'), 5)
        self.assertEqual(action_show.get('model_display_name'), 'User')

        # SHOW should still suppress empty-result actions
        zero_result = {'success': True, 'count': 0, 'total_count': 0, 'records': []}
        action_show_empty = engine._finalize_action_data(None, last_search_call, zero_result, view_preference='show')
        self.assertFalse(action_show_empty)

        # HIDE should always suppress actions
        action_hide = engine._finalize_action_data(None, last_search_call, truncated_result, view_preference='hide')
        self.assertFalse(action_hide)

    def test_base_engine_normalizes_tool_result_contract(self):
        """Normalized tool results should follow the provider-neutral envelope."""
        engine = BaseAiEngine(self.env)
        normalized = engine._normalize_tool_result(
            'search_records',
            {'success': True, 'count': 3, 'records': [{'id': 1}]},
            action_data={'model': 'res.users'}
        )

        self.assertTrue(normalized['success'])
        self.assertIn('data', normalized)
        self.assertIn('user_safe_summary', normalized)
        self.assertEqual(normalized['action_data'], {'model': 'res.users'})
        self.assertIn('search_records', normalized['user_safe_summary'])

    def test_sanitize_for_json_preserves_structure_with_binary_values(self):
        """Binary values should not collapse the full payload into a plain string."""
        sanitized = sanitize_for_json({
            'success': True,
            'records': [
                {'id': 1, 'image_128': b'\x89PNG', 'signature': b'abc'},
            ],
        })

        self.assertIsInstance(sanitized, dict)
        self.assertEqual(sanitized['records'][0]['image_128'], '<binary:4 bytes>')
        self.assertEqual(sanitized['records'][0]['signature'], '<binary:3 bytes>')

    def test_base_engine_normalizes_binary_tool_results_without_crashing(self):
        """Binary fields inside tool results should remain usable after sanitization."""
        engine = BaseAiEngine(self.env)
        normalized = engine._normalize_tool_result(
            'search_records',
            {
                'success': True,
                'count': 1,
                'records': [{'id': 2, 'image_128': b'\x89PNG'}],
            },
        )

        self.assertTrue(normalized['success'])
        self.assertEqual(normalized['data']['records'][0]['image_128'], '<binary:4 bytes>')

    def test_base_engine_duplicate_tool_loop_guard(self):
        """Repeated identical tool calls should be stopped deterministically."""
        engine = BaseAiEngine(self.env)
        context = engine._create_turn_context('gemini')

        first = engine._check_loop_guards(context, 'search_records', {'model': 'res.users', 'count_only': True})
        second = engine._check_loop_guards(context, 'search_records', {'model': 'res.users', 'count_only': True})
        third = engine._check_loop_guards(context, 'search_records', {'model': 'res.users', 'count_only': True})

        self.assertFalse(first)
        self.assertFalse(second)
        self.assertTrue(third)
        self.assertTrue(any(event['type'] == 'turn.failed' for event in context['turn_events']))

    def test_base_engine_uses_company_configured_loop_limits(self):
        """Agent loop guardrails should come from standard AI settings, not fixed constants."""
        self.env.company.write({
            'sh_ai_max_iterations': 14,
            'sh_ai_max_duplicate_tool_calls': 4,
        })

        engine = BaseAiEngine(self.env)

        self.assertEqual(engine.max_iterations, 14)
        self.assertEqual(engine.max_duplicate_tool_calls, 4)

    def test_base_engine_build_result_includes_turn_events_and_capabilities(self):
        """Shared result payload should expose provider-neutral lifecycle metadata."""
        engine = BaseAiEngine(self.env)
        context = engine._create_turn_context('openai', view_preference='auto')
        engine._emit_turn_event(context, 'tool.called', tool_name='search_records')
        result = engine._build_result(context, "Hi Admin, here is the answer.")

        self.assertTrue(result['success'])
        self.assertIn('turn_events', result['debug_info'])
        self.assertIn('provider_capabilities', result['debug_info'])
        self.assertIn('prompt_cache', result['debug_info'])
        self.assertTrue(any(event['type'] == 'turn.completed' for event in result['debug_info']['turn_events']))

    def test_base_engine_build_query_details_uses_model_display_name(self):
        """Query details should expose a user-facing model label from Odoo metadata."""
        engine = BaseAiEngine(self.env)
        details = engine._build_query_details({
            'model': 'res.partner',
            'domain': [],
            'fields': ['name'],
            'operation': 'search',
            'group_by': None,
        })

        self.assertEqual(details['model'], 'res.partner')
        self.assertEqual(details['model_display_name'], 'Contact')

    def test_base_engine_analyzes_relational_filters_dynamically(self):
        """Tool analysis should detect relational filters without hard-coded model logic."""
        engine = BaseAiEngine(self.env)
        context = engine._create_turn_context('openai')

        guard_error, analysis = engine._prepare_tool_call(
            context,
            'search_records',
            {
                'model': 'res.users',
                'domain': [['company_id', '=', 1]],
                'fields': ['name'],
                'count_only': False,
            },
        )

        self.assertFalse(guard_error)
        self.assertEqual(analysis['classification'], 'retrieval')
        self.assertIn('relational_filter', analysis['reasoning_tags'])
        warning_codes = {warning['code'] for warning in analysis['warnings']}
        self.assertIn('model_schema_not_inspected', warning_codes)

    def test_base_engine_flags_retrieval_variants_without_new_discovery(self):
        """Changing relation paths on the same model without new discovery should be visible in debug analysis."""
        engine = BaseAiEngine(self.env)
        context = engine._create_turn_context('openai')

        first_args = {
            'model': 'res.company',
            'domain': [['country_id', '=', 1]],
            'fields': ['name'],
            'count_only': False,
        }
        first_result = engine._normalize_tool_result(
            'search_records',
            {'success': True, 'count': 0, 'total_count': 0, 'records': []},
        )
        first_analysis = engine._analyze_tool_call(context, 'search_records', first_args)
        engine._register_tool_call(context, 'search_records', first_args, first_result, analysis=first_analysis)

        guard_error, second_analysis = engine._prepare_tool_call(
            context,
            'search_records',
            {
                'model': 'res.company',
                'domain': [['partner_id.country_id', '=', 1]],
                'fields': ['name'],
                'count_only': False,
            },
        )

        self.assertFalse(guard_error)
        self.assertIn('retrieval_variant', second_analysis['reasoning_tags'])
        warning_codes = {warning['code'] for warning in second_analysis['warnings']}
        self.assertIn('variant_without_new_discovery', warning_codes)
        self.assertIn('related_schema_not_inspected', warning_codes)

    def test_base_engine_builds_local_recovery_answer_from_verified_results(self):
        """Shared fallback should summarize verified results instead of hard-failing immediately."""
        engine = BaseAiEngine(self.env)
        context = engine._create_turn_context('openai')
        context['last_search_call'] = {
            'model': 'res.users',
            'domain': [],
            'fields': ['name'],
            'operation': 'search',
        }
        context['last_search_result'] = {
            'success': True,
            'count': 2,
            'total_count': 2,
            'records': [
                {'id': 2, 'name': 'Mitchell Admin'},
                {'id': 3, 'name': 'Marc Demo'},
            ],
        }

        fallback = engine._recover_from_iteration_limit(context)

        self.assertIn('Mitchell Admin', fallback)
        self.assertEqual(context['loop_recovery']['mode'], 'tool_summary')

    def test_sync_default_prompt_templates_updates_legacy_defaults_only(self):
        """Legacy shipped defaults should be upgraded without overwriting custom prompts."""
        legacy_llm = self.env['sh.ai.llm'].create({
            'name': 'Legacy Prompt LLM',
            'sh_company': 'OpenAI',
            'sh_model_code': 'gpt-5-mini-legacy',
            'workflow_instruction': LEGACY_WORKFLOW_INSTRUCTION,
            'tool_instruction': LEGACY_TOOL_INSTRUCTION,
        })
        custom_llm = self.env['sh.ai.llm'].create({
            'name': 'Custom Prompt LLM',
            'sh_company': 'Google',
            'sh_model_code': 'gemini-custom',
            'workflow_instruction': 'CUSTOM WORKFLOW',
            'tool_instruction': 'CUSTOM TOOL',
        })

        result = self.env['sh.ai.llm'].sync_default_prompt_templates()

        legacy_llm.invalidate_recordset()
        custom_llm.invalidate_recordset()

        self.assertIn(legacy_llm.id, result['updated_record_ids'])
        self.assertEqual(legacy_llm.workflow_instruction.strip(), WORKFLOW_INSTRUCTION.strip())
        self.assertEqual(legacy_llm.tool_instruction.strip(), TOOL_INSTRUCTION.strip())
        self.assertEqual(custom_llm.workflow_instruction, 'CUSTOM WORKFLOW')
        self.assertEqual(custom_llm.tool_instruction, 'CUSTOM TOOL')

    def test_clone_gemini_part_preserves_thought_signature_and_function_call(self):
        """Gemini 3 tool loops must preserve signed function-call parts verbatim."""
        original = types.Part(
            function_call=types.FunctionCall(name='get_model_fields', id='call-1', args={'models': ['res.partner']}),
            thought=True,
            thought_signature=b'signed-thought',
        )

        cloned = clone_gemini_part(original)

        self.assertEqual(cloned.function_call.name, 'get_model_fields')
        self.assertEqual(cloned.function_call.id, 'call-1')
        self.assertEqual(cloned.function_call.args, {'models': ['res.partner']})
        self.assertEqual(cloned.thought_signature, b'signed-thought')
        self.assertTrue(cloned.thought)

    def test_gemini_function_response_can_match_function_call_id(self):
        """Tool responses for Gemini should preserve the originating function call id."""
        response_part = types.Part(
            function_response=types.FunctionResponse(
                id='call-42',
                name='fuzzy_lookup',
                response={'result': {'success': True}},
            )
        )

        self.assertEqual(response_part.function_response.id, 'call-42')
        self.assertEqual(response_part.function_response.name, 'fuzzy_lookup')

    def test_gemini_engine_recovers_after_max_iterations(self):
        """Gemini tool-only loops should finalize from verified results instead of hard-failing."""
        engine = GeminiEngine(self.env)
        engine.max_iterations = 1

        tool_call_response = type('Response', (), {
            'candidates': [types.Candidate(content=types.Content(role='model', parts=[
                types.Part(function_call=types.FunctionCall(name='get_current_date_info', args={}))
            ]))],
            'usage_metadata': None,
            'text': '',
        })()

        final_text_response = type('Response', (), {
            'candidates': [types.Candidate(content=types.Content(role='model', parts=[
                types.Part(text='It is Friday, March 7, 2026.')
            ]))],
            'usage_metadata': None,
            'text': 'It is Friday, March 7, 2026.',
        })()

        with patch(
            'odoo.addons.sh_ai_assistance.ai_processing.gemini_engine.GeminiProvider.generate_content',
            side_effect=[tool_call_response, final_text_response],
        ):
            result = engine.execute(
                session=None,
                user_message='What time is it?',
                previous_messages=[],
                llm_config={
                    'api_key': 'fake',
                    'model_code': 'gemini-3-flash-preview',
                    'system_instruction': 'test',
                    'tool_declarations': [],
                    'stop_event': None,
                },
            )

        self.assertTrue(result['success'])
        self.assertIn('March 7, 2026', result['content'])
        self.assertEqual(result['debug_info']['recovery']['mode'], 'model_finalization')

    def test_openai_engine_recovers_after_max_iterations(self):
        """OpenAI tool-only loops should finalize from verified results instead of failing at the limit."""
        engine = OpenAiEngine(self.env)
        engine.max_iterations = 1

        function_call = type('Function', (), {
            'name': 'get_current_date_info',
            'arguments': '{}',
        })()
        tool_call = type('ToolCall', (), {
            'id': 'call_1',
            'function': function_call,
        })()
        tool_loop_response = type('Response', (), {
            'choices': [type('Choice', (), {
                'message': type('Message', (), {
                    'content': '',
                    'tool_calls': [tool_call],
                })()
            })()],
            'usage': None,
        })()
        final_text_response = type('Response', (), {
            'choices': [type('Choice', (), {
                'message': type('Message', (), {
                    'content': 'Today is March 7, 2026.',
                    'tool_calls': [],
                })()
            })()],
            'usage': None,
        })()

        with patch(
            'odoo.addons.sh_ai_assistance.ai_processing.openai_engine.OpenAIProvider.generate_content',
            side_effect=[tool_loop_response, final_text_response],
        ):
            result = engine.execute(
                session=None,
                user_message='What date is it?',
                previous_messages=[],
                llm_config={
                    'api_key': 'fake',
                    'model_code': 'gpt-5-mini',
                    'system_instruction': 'test',
                    'tool_declarations': [],
                    'stop_event': None,
                },
            )

        self.assertTrue(result['success'])
        self.assertIn('March 7, 2026', result['content'])
        self.assertEqual(result['debug_info']['recovery']['mode'], 'model_finalization')

    def test_openrouter_provider_type_routes_to_openrouter_engine(self):
        llm = self.env['sh.ai.llm'].create({
            'name': 'OpenRouter Model',
            'provider_type': 'openrouter',
            'sh_company': 'OpenRouter',
            'sh_model_code': 'openai/gpt-4o-mini',
        })

        self.assertEqual(llm._detect_provider_type(), 'openrouter')
        self.assertIsInstance(AiEngineFactory.get_engine(self.env, llm._detect_provider_type()), OpenRouterEngine)

    def test_engine_factory_returns_openrouter_engine(self):
        selected = AiEngineFactory.get_engine(self.env, 'openrouter')
        self.assertIsInstance(selected, OpenRouterEngine)

    def test_deepseek_and_claude_provider_detection_and_factory(self):
        deepseek_llm = self.env['sh.ai.llm'].create({
            'name': 'DeepSeek Demo',
            'sh_company': 'DeepSeek',
            'sh_model_code': 'deepseek-v4-flash',
        })
        claude_llm = self.env['sh.ai.llm'].create({
            'name': 'Claude Demo',
            'sh_company': 'Anthropic',
            'sh_model_code': 'claude-sonnet-4-6',
        })

        self.assertEqual(deepseek_llm._detect_provider_type(), 'deepseek')
        self.assertEqual(claude_llm._detect_provider_type(), 'claude')
        self.assertIsInstance(AiEngineFactory.get_engine(self.env, 'deepseek'), DeepSeekEngine)
        self.assertIsInstance(AiEngineFactory.get_engine(self.env, 'claude'), ClaudeEngine)

    def test_claude_engine_converts_tools_to_anthropic_schema(self):
        engine = ClaudeEngine(self.env)
        tool = engine._convert_tools([
            {
                'name': 'search_records',
                'description': 'Search records',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'model': {'type': 'string'},
                    },
                    'required': ['model'],
                },
            },
        ])[0]

        self.assertEqual(tool['name'], 'search_records')
        self.assertEqual(tool['input_schema']['type'], 'object')
        self.assertEqual(tool['input_schema']['required'], ['model'])

    def test_claude_provider_receives_system_instruction_separately(self):
        provider = ClaudeProvider('test-key')
        response = type('Response', (), {
            'choices': [type('Choice', (), {
                'message': type('Message', (), {
                    'content': 'ok',
                    'tool_calls': None,
                })()
            })()],
            'usage': None,
        })()

        with patch.object(provider, '_create_completion', return_value=response) as mocked:
            provider.generate_content(
                model='claude-sonnet-4-6',
                messages=[{'role': 'user', 'content': 'Hello'}],
                system='System prompt',
                tools=[{'name': 'search_records', 'description': 'Search', 'input_schema': {'type': 'object'}}],
                temperature=0.2,
            )

        params = mocked.call_args.args[0]
        self.assertEqual(params['system'], 'System prompt')
        self.assertEqual(params['messages'][0]['content'], 'Hello')
        self.assertEqual(params['tools'][0]['name'], 'search_records')

    def test_new_provider_key_verification_routes_to_new_helpers(self):
        deepseek_llm = self.env['sh.ai.llm'].create({
            'name': 'DeepSeek Demo',
            'sh_company': 'DeepSeek',
            'sh_model_code': 'deepseek-v4-flash',
        })
        claude_llm = self.env['sh.ai.llm'].create({
            'name': 'Claude Demo',
            'sh_company': 'Anthropic',
            'sh_model_code': 'claude-sonnet-4-6',
        })

        with patch.object(AssistantLlm, '_verify_deepseek_key', return_value={'success': True, 'message': 'ok'}) as mocked_deepseek:
            result = deepseek_llm._verify_api_key('DeepSeek', 'fake-deepseek-key')
            self.assertTrue(result['success'])
            mocked_deepseek.assert_called_once()

        with patch.object(AssistantLlm, '_verify_claude_key', return_value={'success': True, 'message': 'ok'}) as mocked_claude:
            result = claude_llm._verify_api_key('Anthropic', 'fake-claude-key')
            self.assertTrue(result['success'])
            mocked_claude.assert_called_once()

    def test_openrouter_engine_uses_openrouter_provider_contract(self):
        engine = OpenRouterEngine(self.env)
        final_text_response = type('Response', (), {
            'choices': [type('Choice', (), {
                'message': type('Message', (), {
                    'content': 'OpenRouter answer',
                    'tool_calls': [],
                })()
            })()],
            'usage': None,
        })()

        with patch(
            'odoo.addons.sh_ai_assistance.ai_processing.openrouter_engine.OpenRouterProvider.generate_content',
            return_value=final_text_response,
        ) as mocked_generate:
            result = engine.execute(
                session=None,
                user_message='Hello',
                previous_messages=[],
                llm_config={
                    'api_key': 'fake-openrouter-key',
                    'model_code': 'openai/gpt-4o-mini',
                    'provider_type': 'openrouter',
                    'system_instruction': 'test',
                    'tool_declarations': [],
                    'stop_event': None,
                },
            )

        self.assertTrue(result['success'])
        self.assertEqual(result['content'], 'OpenRouter answer')
        mocked_generate.assert_called_once()

    def test_openrouter_engine_requires_api_key(self):
        engine = OpenRouterEngine(self.env)
        result = engine.execute(
            session=None,
            user_message='Hello',
            previous_messages=[],
            llm_config={
                'api_key': '',
                'model_code': 'openai/gpt-4o-mini',
                'provider_type': 'openrouter',
                'system_instruction': 'test',
                'tool_declarations': [],
                'stop_event': None,
            },
        )

        self.assertFalse(result['success'])
        self.assertIn('API key', result['error'])
