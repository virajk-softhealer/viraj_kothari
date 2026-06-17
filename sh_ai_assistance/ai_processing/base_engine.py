# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies.

import logging
from collections import Counter
from datetime import datetime
from .utils import sanitize_for_json
from ...sh_ai_base.provider.odoo_tools import (
    search_records, get_current_date_info, get_models_list,
    get_model_fields, aggregate_records, get_selection_values, open_view,
    fuzzy_lookup
)
from .write_tools import (
    prepare_archive_records,
    prepare_create_record,
    prepare_update_records,
)

_logger = logging.getLogger(__name__)

RELATIONAL_FIELD_TYPES = {'many2one', 'many2many', 'one2many'}

class BaseAiEngine:
    """
    Base class for AI Processing Engines.
    Provides common methods for tool execution and context management.
    """

    def __init__(self, env):
        self.env = env
        self.max_iterations = self._get_configured_limit('sh_ai_max_iterations', default=20, minimum=1)
        self.max_duplicate_tool_calls = self._get_configured_limit('sh_ai_max_duplicate_tool_calls', default=2, minimum=1)
        self.provider_capabilities = {
            'openai': {
                'tool_calling': True,
                'structured_output': 'json_schema',
                'streaming': True,
                'usage_fields': True,
                'cancellation': 'stream_close',
            },
            'openrouter': {
                'tool_calling': True,
                'structured_output': 'json_schema',
                'streaming': True,
                'usage_fields': True,
                'cancellation': 'stream_close',
            },
            'gemini': {
                'tool_calling': True,
                'structured_output': 'function_declarations',
                'streaming': True,
                'usage_fields': True,
                'cancellation': 'stop_event',
            },
        }

    def _get_configured_limit(self, field_name, default, minimum=1):
        try:
            value = getattr(self.env.company, field_name, default)
            value = int(value or default)
        except Exception:
            value = default
        return max(minimum, value)

    def _get_provider_capabilities(self, provider_type):
        return self.provider_capabilities.get(provider_type, {})

    def _create_turn_context(self, provider_type, view_preference='auto', session_id=None, model_name=None, llm_id=None):
        context = {
            'provider_type': provider_type,
            'view_preference': view_preference,
            'session_id': session_id,
            'model_name': model_name,
            'llm_id': llm_id,
            'iteration': 0,
            'turn_events': [],
            'debug_tool_calls': [],
            'tool_signatures': [],
            'last_search_call': None,
            'last_search_result': None,
            'action_data': None,
            'write_request_id': None,
            'write_request_preview': None,
            'schema_epoch': 0,
            'schema_discoveries': {},
            'retrieval_history': [],
            'usage': {
                'prompt_tokens': 0,
                'completion_tokens': 0,
                'cached_tokens': 0,
                'reasoning_tokens': 0,
                'total_tokens': 0,
            },
            'assistant_response': None,
            'loop_recovery': None,
            'last_successful_tool_name': None,
            'last_successful_tool_result': None,
        }
        self._emit_turn_event(context, 'turn.started', provider=provider_type)
        return context

    def _tool_status_detail(self, tool_name, payload):
        args = payload.get('args') or {}
        model = args.get('model')
        operation = args.get('operation')

        if tool_name == 'search_records':
            return "Searching matching records."
        if tool_name == 'aggregate_records':
            if operation == 'count':
                return "Calculating a verified count."
            return "Building a grouped summary from live data."
        if tool_name == 'get_model_fields':
            return "Checking available fields before querying."
        if tool_name == 'get_models_list':
            return "Checking which Odoo models are available."
        if tool_name == 'get_selection_values':
            return "Loading allowed field values."
        if tool_name == 'fuzzy_lookup':
            return "Looking up the requested record."
        if tool_name == 'open_view':
            return "Preparing an Odoo view action."
        if tool_name == 'get_current_date_info':
            return "Checking the current date and time context."
        if tool_name == 'prepare_create_record':
            return "Preparing a create preview for approval."
        if tool_name == 'prepare_update_records':
            return "Preparing an update preview for approval."
        if tool_name == 'prepare_archive_records':
            return "Preparing an archive preview for approval."
        return "Processing the next backend step."

    def _tool_status_title(self, tool_name):
        labels = {
            'search_records': 'Searching records',
            'aggregate_records': 'Building summary',
            'get_model_fields': 'Checking fields',
            'get_models_list': 'Checking models',
            'get_selection_values': 'Loading options',
            'fuzzy_lookup': 'Finding record',
            'open_view': 'Preparing view',
            'get_current_date_info': 'Checking date',
            'prepare_create_record': 'Preparing create preview',
            'prepare_update_records': 'Preparing update preview',
            'prepare_archive_records': 'Preparing archive preview',
        }
        return labels.get(tool_name, 'Processing')

    def _processing_status_for_event(self, context, event_type, payload):
        model_name = context.get('model_name') or 'AI'
        statuses = {
            'turn.started': {
                'active': True,
                'phase': 'starting',
                'model_name': model_name,
                'title': 'Understanding request',
                'detail': 'The model is starting to process your question.',
            },
            'assistant.completed': {
                'active': True,
                'phase': 'finalizing',
                'model_name': model_name,
                'title': 'Preparing answer',
                'detail': 'The final response has been generated and is being packaged for the chat.',
            },
            'turn.completed': {
                'active': False,
                'phase': 'completed',
                'model_name': model_name,
                'title': 'Completed',
                'detail': 'The answer is ready.',
            },
            'turn.cancelled': {
                'active': False,
                'phase': 'cancelled',
                'model_name': model_name,
                'title': 'Request cancelled',
                'detail': 'Generation was stopped before completion.',
            },
            'turn.failed': {
                'active': False,
                'phase': 'failed',
                'model_name': model_name,
                'title': 'Request failed',
                'detail': payload.get('error') or payload.get('reason') or 'The request could not be completed.',
            },
        }
        if event_type == 'tool.called':
            tool_name = payload.get('tool_name')
            return {
                'active': True,
                'phase': 'tool',
                'model_name': model_name,
                'title': self._tool_status_title(tool_name),
                'detail': self._tool_status_detail(tool_name, payload),
                'tool_name': tool_name,
            }
        if event_type == 'tool.failed':
            tool_name = payload.get('tool_name')
            return {
                'active': True,
                'phase': 'tool_failed',
                'model_name': model_name,
                'title': f"{self._tool_status_title(tool_name)} failed",
                'detail': payload.get('result', {}).get('error') or f"{tool_name} did not complete successfully.",
                'tool_name': tool_name,
            }
        return statuses.get(event_type)

    def _sync_processing_status(self, context, event_type, payload):
        session_id = context.get('session_id')
        if not session_id:
            return
        status = self._processing_status_for_event(context, event_type, payload)
        if not status:
            return
        try:
            self.env['sh.ai.chat.message']._set_processing_status(session_id, status)
        except Exception:
            _logger.debug("Failed to sync processing status", exc_info=True)

    def _emit_turn_event(self, context, event_type, **payload):
        event = {
            'type': event_type,
            'timestamp': datetime.now().isoformat(),
        }
        if payload:
            event.update(sanitize_for_json(payload))
        context['turn_events'].append(event)
        self._sync_processing_status(context, event_type, event)
        return event

    def _accumulate_usage(self, context, prompt_tokens=0, completion_tokens=0, total_tokens=0, cached_tokens=0, reasoning_tokens=0):
        usage = context['usage']
        usage['prompt_tokens'] += prompt_tokens or 0
        usage['completion_tokens'] += completion_tokens or 0
        usage['cached_tokens'] += cached_tokens or 0
        usage['reasoning_tokens'] += reasoning_tokens or 0
        usage['total_tokens'] += total_tokens or 0

    def _tool_signature(self, tool_name, tool_args):
        serialized_args = sanitize_for_json(tool_args) or {}
        return f"{tool_name}:{serialized_args}"

    def _check_loop_guards(self, context, tool_name=None, tool_args=None):
        if tool_name is None:
            return None

        signature = self._tool_signature(tool_name, tool_args)
        context['tool_signatures'].append(signature)
        duplicate_count = Counter(context['tool_signatures'])[signature]
        if duplicate_count > self.max_duplicate_tool_calls:
            self._emit_turn_event(
                context,
                'turn.failed',
                reason='duplicate_tool_call',
                tool_name=tool_name,
                duplicate_count=duplicate_count,
            )
            return (
                "The assistant stopped because the same tool call repeated too many times. "
                "The max tool call limit was reached. You can increase the 'Max Tool Rounds' "
                "setting for better results."
            )
        return None

    def _normalize_tool_result(self, tool_name, raw_result, action_data=None):
        raw_result = sanitize_for_json(raw_result) or {}
        if not isinstance(raw_result, dict):
            raw_result = {
                'success': False,
                'error': f"{tool_name} returned an unsupported payload type.",
                'raw_value': raw_result,
            }
        success = bool(raw_result.get('success', 'error' not in raw_result))
        user_safe_summary = None
        if success:
            if 'count' in raw_result and raw_result.get('count') is not None:
                user_safe_summary = f"{tool_name} returned {raw_result.get('count')} result(s)."
            elif 'total_count' in raw_result and raw_result.get('total_count') is not None:
                user_safe_summary = f"{tool_name} matched {raw_result.get('total_count')} record(s)."
            else:
                user_safe_summary = f"{tool_name} completed successfully."
        else:
            user_safe_summary = raw_result.get('error') or f"{tool_name} failed."

        return {
            'success': success,
            'data': raw_result,
            'user_safe_summary': user_safe_summary,
            'debug': {
                'tool_name': tool_name,
                'raw_result': raw_result,
            },
            'retryable': not success and ('timeout' in (raw_result.get('error') or '').lower()),
            'action_data': action_data,
        }

    def _safe_fields_get(self, model):
        try:
            return self.env[model].fields_get()
        except Exception:
            return {}

    def _extract_domain_fields(self, domain):
        domain_fields = []
        for clause in domain or []:
            if isinstance(clause, (list, tuple)) and len(clause) == 3 and isinstance(clause[0], str):
                domain_fields.append(clause[0])
        return domain_fields

    def _describe_field_path(self, model, field_path):
        current_model = model
        chain = []
        path_parts = (field_path or '').split('.')

        for index, part in enumerate(path_parts):
            fields_info = self._safe_fields_get(current_model)
            field_info = fields_info.get(part)
            if not field_info:
                return {
                    'field_path': field_path,
                    'valid': False,
                    'chain': chain,
                    'missing_field': part,
                }

            field_type = field_info.get('type')
            relation_model = field_info.get('relation')
            chain.append({
                'model': current_model,
                'field': part,
                'type': field_type,
                'relation': relation_model,
            })

            is_last_part = index == len(path_parts) - 1
            if not is_last_part:
                if field_type not in RELATIONAL_FIELD_TYPES or not relation_model:
                    return {
                        'field_path': field_path,
                        'valid': False,
                        'chain': chain,
                        'missing_relation_hop': part,
                    }
                current_model = relation_model

        return {
            'field_path': field_path,
            'valid': True,
            'chain': chain,
        }

    def _classify_tool_call(self, tool_name, tool_args):
        if tool_name in ('get_models_list', 'get_model_fields', 'get_selection_values', 'get_current_date_info'):
            return 'discovery'
        if tool_name == 'fuzzy_lookup':
            return 'resolution'
        if tool_name in ('search_records', 'aggregate_records'):
            return 'retrieval'
        if tool_name in ('prepare_create_record', 'prepare_update_records', 'prepare_archive_records'):
            return 'write_proposal'
        if tool_name == 'open_view':
            return 'action'
        return 'other'

    def _analyze_tool_call(self, context, tool_name, tool_args):
        classification = self._classify_tool_call(tool_name, tool_args)
        analysis = {
            'classification': classification,
            'schema_epoch': context.get('schema_epoch', 0),
            'reasoning_tags': [],
            'warnings': [],
        }

        if tool_name == 'get_model_fields':
            analysis['target_models'] = tool_args.get('models', [])
            if analysis['target_models']:
                analysis['reasoning_tags'].append('schema_discovery')
            return analysis

        if tool_name == 'fuzzy_lookup':
            analysis['target_model'] = tool_args.get('model')
            analysis['search_term'] = tool_args.get('search_term')
            analysis['reasoning_tags'].append('id_resolution')
            return analysis

        if tool_name in ('prepare_create_record', 'prepare_update_records', 'prepare_archive_records'):
            analysis.update({
                'model': tool_args.get('model'),
                'operation': tool_name.replace('prepare_', '').replace('_records', '').replace('_record', ''),
                'reasoning_tags': ['approval_required', 'write_preview'],
            })
            return analysis

        if tool_name not in ('search_records', 'aggregate_records', 'open_view'):
            return analysis

        model = tool_args.get('model')
        domain = tool_args.get('domain') or []
        domain_fields = self._extract_domain_fields(domain)
        path_descriptions = [self._describe_field_path(model, field_path) for field_path in domain_fields if model]
        dotted_paths = [item['field_path'] for item in path_descriptions if '.' in item['field_path']]
        related_models = sorted({
            step.get('relation')
            for item in path_descriptions
            for step in item.get('chain', [])[:-1]
            if step.get('relation')
        })

        analysis.update({
            'model': model,
            'domain_fields': domain_fields,
            'has_domain': bool(domain_fields),
            'path_descriptions': path_descriptions,
            'related_models': related_models,
        })

        if domain_fields:
            analysis['reasoning_tags'].append('domain_filter')
        if dotted_paths:
            analysis['reasoning_tags'].append('relational_path')
        if any(
            step.get('type') in RELATIONAL_FIELD_TYPES
            for item in path_descriptions
            for step in item.get('chain', [])
        ):
            analysis['reasoning_tags'].append('relational_filter')

        if model in context.get('schema_discoveries', {}):
            analysis['reasoning_tags'].append('schema_known')
        else:
            analysis['warnings'].append({
                'code': 'model_schema_not_inspected',
                'model': model,
            })

        missing_related_models = [
            rel_model for rel_model in related_models
            if rel_model not in context.get('schema_discoveries', {})
        ]
        if missing_related_models:
            analysis['warnings'].append({
                'code': 'related_schema_not_inspected',
                'models': missing_related_models,
            })

        invalid_paths = [item['field_path'] for item in path_descriptions if not item.get('valid')]
        if invalid_paths:
            analysis['warnings'].append({
                'code': 'invalid_domain_path',
                'paths': invalid_paths,
            })

        previous_retrieval = context.get('retrieval_history', [])[-1] if context.get('retrieval_history') else None
        if previous_retrieval and previous_retrieval.get('model') == model:
            same_schema_epoch = previous_retrieval.get('schema_epoch') == context.get('schema_epoch', 0)
            same_domain_fields = sorted(previous_retrieval.get('domain_fields', [])) == sorted(domain_fields)
            same_domain = sanitize_for_json(previous_retrieval.get('domain', [])) == sanitize_for_json(domain)

            if same_domain:
                analysis['reasoning_tags'].append('verification_followup')
            elif same_schema_epoch and not same_domain_fields:
                analysis['reasoning_tags'].append('retrieval_variant')
                analysis['warnings'].append({
                    'code': 'variant_without_new_discovery',
                    'previous_domain_fields': previous_retrieval.get('domain_fields', []),
                })

        return analysis

    def _record_schema_discovery(self, context, tool_args, normalized_result):
        if not normalized_result.get('success'):
            return

        for model in tool_args.get('models', []) or []:
            context['schema_discoveries'][model] = {
                'epoch': context.get('schema_epoch', 0),
                'timestamp': datetime.now().isoformat(),
            }

    def _record_retrieval_context(self, context, tool_name, tool_args, normalized_result, analysis):
        if tool_name not in ('search_records', 'aggregate_records', 'open_view'):
            return
        if not normalized_result.get('success'):
            return

        result_data = normalized_result.get('data', {})
        context['retrieval_history'].append({
            'tool_name': tool_name,
            'model': tool_args.get('model'),
            'domain': sanitize_for_json(tool_args.get('domain') or []),
            'domain_fields': analysis.get('domain_fields', []),
            'schema_epoch': context.get('schema_epoch', 0),
            'count_only': tool_args.get('count_only', False),
            'operation': tool_args.get('operation'),
            'result_count': result_data.get('count'),
            'result_total_count': result_data.get('total_count'),
        })

    def _execute_tool_call(self, context, tool_name, tool_args, iteration):
        """Execute Odoo tool and return results"""
        result = None
        last_search_call = None
        last_search_result = None
        action_data = None

        # Sanitize tool_args: Remove None values to allow Odoo defaults to take over
        # This is critical for OpenAI Strict Mode where ALL params are sent as null if unused.
        tool_args = {k: v for k, v in tool_args.items() if v is not None}

        _logger.info(f"🔧 AI Tool Called: {tool_name} with args: {tool_args}")

        try:
            if tool_name == "search_records":
                if not tool_args.get('count_only', False):
                    last_search_call = tool_args
                result = search_records(self.env, **tool_args)
                last_search_result = result
            elif tool_name == "get_current_date_info":
                result = get_current_date_info(self.env)
            elif tool_name == "get_models_list":
                result = get_models_list(self.env)
            elif tool_name == "get_model_fields":
                result = get_model_fields(self.env, **tool_args)
            elif tool_name == "aggregate_records":
                last_search_call = {
                    'model': tool_args.get('model'),
                    'domain': tool_args.get('domain', []),
                    'fields': [tool_args.get('field')] if tool_args.get('field') else [],
                    'group_by': tool_args.get('group_by'),
                    'operation': tool_args.get('operation'),
                }
                result = aggregate_records(self.env, **tool_args)
                last_search_result = result
            elif tool_name == "get_selection_values":
                result = get_selection_values(self.env, **tool_args)
            elif tool_name == "open_view":
                result = open_view(self.env, **tool_args)
                if result.get('success') and result.get('action_data'):
                    action_data = result['action_data']
            elif tool_name == "fuzzy_lookup":
                result = fuzzy_lookup(self.env, **tool_args)
            elif tool_name == "prepare_create_record":
                result = prepare_create_record(
                    self.env,
                    session_id=context.get('session_id'),
                    llm_id=context.get('llm_id'),
                    **tool_args,
                )
            elif tool_name == "prepare_update_records":
                result = prepare_update_records(
                    self.env,
                    session_id=context.get('session_id'),
                    llm_id=context.get('llm_id'),
                    **tool_args,
                )
            elif tool_name == "prepare_archive_records":
                result = prepare_archive_records(
                    self.env,
                    session_id=context.get('session_id'),
                    llm_id=context.get('llm_id'),
                    **tool_args,
                )
            else:
                result = {"error": f"Unknown function: {tool_name}"}
        except Exception as e:
            _logger.error(f"Error executing tool {tool_name}: {str(e)}", exc_info=True)
            result = {"error": str(e), "success": False}

        normalized_result = self._normalize_tool_result(tool_name, result, action_data=action_data)

        # Sanitize everything for JSON storage (converts datetimes to strings etc.)
        return (
            sanitize_for_json(normalized_result),
            sanitize_for_json(last_search_call), 
            sanitize_for_json(last_search_result), 
            sanitize_for_json(action_data)
        )

    def _register_tool_call(self, context, tool_name, tool_args, normalized_result, analysis=None):
        analysis = sanitize_for_json(analysis) or {}
        payload = {
            'tool': tool_name,
            'args': sanitize_for_json(tool_args),
            'iteration': context['iteration'] + 1,
            'analysis': analysis,
            'result': {
                'success': normalized_result.get('success'),
                'retryable': normalized_result.get('retryable'),
                'user_safe_summary': normalized_result.get('user_safe_summary'),
                'error': normalized_result.get('data', {}).get('error'),
                'count': normalized_result.get('data', {}).get('count'),
            },
            'raw_result': normalized_result.get('data'),
        }
        context['debug_tool_calls'].append(payload)
        if normalized_result.get('success'):
            context['last_successful_tool_name'] = tool_name
            context['last_successful_tool_result'] = normalized_result

        if tool_name == 'get_model_fields':
            context['schema_epoch'] += 1
            self._record_schema_discovery(context, tool_args, normalized_result)
        else:
            self._record_retrieval_context(context, tool_name, tool_args, normalized_result, analysis)

        raw_data = normalized_result.get('data') or {}
        write_request_id = raw_data.get('write_request_id')
        if write_request_id and normalized_result.get('success'):
            context['write_request_id'] = write_request_id
            context['write_request_preview'] = raw_data.get('preview')
            self._emit_turn_event(
                context,
                'write.proposed',
                request_id=write_request_id,
                operation=raw_data.get('operation'),
                model=raw_data.get('model'),
                target_count=raw_data.get('target_count'),
            )

        self._emit_turn_event(
            context,
            'tool.completed' if normalized_result.get('success') else 'tool.failed',
            tool_name=tool_name,
            args=tool_args,
            result=payload['result'],
            analysis=analysis,
        )

    def _prepare_tool_call(self, context, tool_name, tool_args):
        analysis = self._analyze_tool_call(context, tool_name, tool_args)
        self._emit_turn_event(
            context,
            'tool.called',
            tool_name=tool_name,
            args=sanitize_for_json(tool_args),
            iteration=context['iteration'] + 1,
            analysis=analysis,
        )
        guard_error = self._check_loop_guards(context, tool_name=tool_name, tool_args=tool_args)
        return guard_error, analysis

    def _mark_assistant_delta(self, context, content):
        if not content:
            return
        self._emit_turn_event(
            context,
            'assistant.delta',
            content_preview=content[:160],
        )

    def _build_forced_finalization_instruction(self, context):
        model_name = self._get_model_display_name(
            (context.get('last_search_call') or {}).get('model')
        ) or "records"
        return (
            "Answer the user's request now using only the verified tool results already in this conversation. "
            "Do not call or request more tools. "
            "If the verified result is empty, say that clearly. "
            "If the result is grouped, summarize the most important groups. "
            "If a write approval preview was prepared, explain that approval is required before any data changes. "
            f"Prefer a direct business answer based on the verified {model_name} data."
        )

    def _summarize_record_examples(self, records, limit=3):
        examples = []
        for record in (records or [])[:limit]:
            if not isinstance(record, dict):
                continue
            label = (
                record.get('display_name')
                or record.get('name')
                or record.get('email')
                or record.get('login')
                or record.get('reference')
                or record.get('id')
            )
            if label not in (None, ''):
                examples.append(str(label))
        return examples

    def _build_local_recovery_answer(self, context):
        write_preview = context.get('write_request_preview') or {}
        if context.get('write_request_id') and write_preview:
            operation = (write_preview.get('operation_label') or write_preview.get('operation') or 'write').lower()
            model_name = write_preview.get('model_display_name') or "record"
            target_count = write_preview.get('target_count') or 1
            return (
                f"I prepared a {operation} request for {target_count} {model_name} record(s). "
                "Review the approval card below and approve or reject it."
            )

        last_search_call = context.get('last_search_call') or {}
        last_search_result = context.get('last_search_result') or {}
        model_name = self._get_model_display_name(last_search_call.get('model')) or "records"
        action_hint = ""
        if context.get('action_data') or self._finalize_action_data(
            None,
            last_search_call,
            last_search_result,
            context.get('view_preference', 'auto'),
        ):
            action_hint = " Use the open-view buttons below to inspect the result in Odoo."

        if last_search_result.get('success'):
            groups = last_search_result.get('groups') or []
            if groups:
                lines = []
                for group in groups[:5]:
                    label = group.get('group') or "Unknown"
                    value = group.get('value')
                    lines.append(f"- {label}: {value}")
                return (
                    f"I grouped the matching {model_name} data and found:\n" +
                    "\n".join(lines) +
                    action_hint
                )

            total_count = last_search_result.get('total_count')
            count = last_search_result.get('count')
            records = last_search_result.get('records') or []
            effective_count = total_count if total_count is not None else count

            if effective_count == 0:
                return f"I checked {model_name} and found no matching records."

            if records:
                examples = self._summarize_record_examples(records)
                if examples:
                    return (
                        f"I found {len(records)} matching {model_name} record(s). "
                        f"Examples: {', '.join(examples)}." +
                        action_hint
                    )

            if effective_count is not None:
                return f"I found {effective_count} matching {model_name} record(s)." + action_hint

        last_tool_name = context.get('last_successful_tool_name')
        last_tool_result = context.get('last_successful_tool_result') or {}
        if last_tool_name and last_tool_result.get('success'):
            summary = last_tool_result.get('user_safe_summary')
            if summary:
                return (
                    "I completed the backend verification step, but the model did not finish composing the reply. "
                    f"Verified result: {summary}"
                )

        return None

    def _recover_from_iteration_limit(self, context, provider_finalizer=None):
        recovery = {
            'reason': 'max_iterations_exceeded',
            'iteration_limit': self.max_iterations,
            'mode': None,
        }
        context['loop_recovery'] = recovery

        if provider_finalizer and context.get('debug_tool_calls'):
            try:
                final_content = provider_finalizer()
                if final_content:
                    recovery['mode'] = 'model_finalization'
                    return final_content
            except Exception:
                _logger.warning("Forced finalization after tool loop exhaustion failed", exc_info=True)

        final_content = self._build_local_recovery_answer(context)
        if final_content:
            recovery['mode'] = 'tool_summary'
            return final_content

        return None

    def _build_result(self, context, final_content, is_stopped=False, error=None):
        last_search_call = context.get('last_search_call')
        last_search_result = context.get('last_search_result')
        action_data = self._finalize_action_data(
            context.get('action_data'),
            last_search_call,
            last_search_result,
            context.get('view_preference', 'auto'),
        )
        query_details = self._build_query_details(last_search_call)
        usage = context['usage']
        total_tokens = usage['total_tokens'] or (usage['prompt_tokens'] + usage['completion_tokens'])
        recovery = context.get('loop_recovery')

        if error:
            self._emit_turn_event(context, 'turn.failed', error=error)
            return {
                'success': False,
                'error': error,
                'write_request_id': context.get('write_request_id'),
                'debug_info': {
                    'tool_calls': context['debug_tool_calls'],
                    'turn_events': context['turn_events'],
                    'provider_capabilities': self._get_provider_capabilities(context.get('provider_type')),
                    'prompt_cache': {
                        'key': context.get('prompt_cache_key'),
                        'metadata': context.get('prompt_cache_metadata'),
                    },
                    'recovery': recovery,
                    'total_iterations': context['iteration'] + 1,
                    'final_query': last_search_call,
                    'timestamp': datetime.now().isoformat(),
                }
            }

        self._emit_turn_event(
            context,
            'assistant.completed',
            content_preview=(final_content or '')[:160],
            recovery=recovery,
        )
        self._emit_turn_event(
            context,
            'turn.cancelled' if is_stopped else 'turn.completed',
            tool_call_count=len(context['debug_tool_calls']),
            recovery=recovery,
        )

        return {
            'success': True,
            'content': final_content,
            'action_data': action_data,
            'write_request_id': context.get('write_request_id'),
            'write_request_preview': context.get('write_request_preview'),
            'query_details': query_details,
            'usage': {
                'prompt_tokens': usage['prompt_tokens'],
                'completion_tokens': usage['completion_tokens'],
                'cached_tokens': usage['cached_tokens'],
                'reasoning_tokens': usage['reasoning_tokens'],
                'total_tokens': total_tokens,
            },
            'tool_call_count': len(context['debug_tool_calls']),
            'stopped': is_stopped,
            'debug_info': {
                'tool_calls': context['debug_tool_calls'],
                'turn_events': context['turn_events'],
                'provider_capabilities': self._get_provider_capabilities(context.get('provider_type')),
                'prompt_cache': {
                    'key': context.get('prompt_cache_key'),
                    'metadata': context.get('prompt_cache_metadata'),
                },
                'recovery': recovery,
                'total_iterations': context['iteration'] + 1,
                'final_query': last_search_call,
                'timestamp': datetime.now().isoformat()
            }
        }

    def _build_action_data(self, last_search_call, last_search_result):
        """Construct action data for 'View All' buttons based on search results"""
        if not last_search_call:
            return None

        total_count = (last_search_result or {}).get('total_count')
        count = (last_search_result or {}).get('count')
        success = (last_search_result or {}).get('success', True)

        if not success:
            return None

        action = {
            'model': last_search_call.get('model'),
            'model_display_name': self._get_model_display_name(last_search_call.get('model')),
            'domain': last_search_call.get('domain', []),
            'total_count': total_count if total_count is not None else (count or 0),
        }
        group_by = last_search_call.get('group_by') or (last_search_result or {}).get('group_by')
        if group_by:
            action['group_by'] = group_by

        # Show button if records were truncated or there are many records
        if total_count is not None and count is not None and total_count > count and total_count > 0:
            return action

        return None

    def _build_forced_action_data(self, last_search_call, last_search_result):
        """Build action data even when results are not truncated (for explicit UX preference)."""
        if not last_search_call:
            return None

        success = (last_search_result or {}).get('success', True)
        if not success:
            return None

        count = (last_search_result or {}).get('count')
        total_count = (last_search_result or {}).get('total_count')

        action = {
            'model': last_search_call.get('model'),
            'model_display_name': self._get_model_display_name(last_search_call.get('model')),
            'domain': last_search_call.get('domain', []),
            'total_count': total_count if total_count is not None else (count or 0),
        }

        group_by = last_search_call.get('group_by') or (last_search_result or {}).get('group_by')
        if group_by:
            action['group_by'] = group_by

        has_matching_records = bool(
            (isinstance(total_count, int) and total_count > 0)
            or (isinstance(count, int) and count > 0)
            or (last_search_result or {}).get('records')
            or (last_search_result or {}).get('groups')
        )
        if has_matching_records:
            return action

        return None

    def _finalize_action_data(self, action_data, last_search_call, last_search_result, view_preference='auto'):
        """Finalize view action metadata consistently for all providers."""
        if view_preference == 'hide':
            return None

        if action_data:
            return action_data

        if view_preference == 'show':
            return self._build_forced_action_data(last_search_call, last_search_result)

        return self._build_action_data(last_search_call, last_search_result)

    def _build_query_details(self, last_search_call):
        """Build normalized query details payload for debug/UI."""
        if not last_search_call:
            return None

        return {
            'model': last_search_call.get('model'),
            'model_display_name': self._get_model_display_name(last_search_call.get('model')),
            'domain': last_search_call.get('domain', []),
            'operation': last_search_call.get('operation', 'search'),
            'fields': last_search_call.get('fields', []),
            'group_by': last_search_call.get('group_by'),
        }

    def _get_model_display_name(self, model_name):
        """Resolve a user-facing model label dynamically from Odoo metadata."""
        if not model_name:
            return None
        try:
            model = self.env[model_name]
            if getattr(model, "_description", None):
                return model._description
        except Exception:
            pass
        try:
            ir_model = self.env["ir.model"].sudo().search([("model", "=", model_name)], limit=1)
            if ir_model and ir_model.display_name:
                return ir_model.display_name
        except Exception:
            pass
        return model_name

    def execute(self, session, user_message, previous_messages, llm_config):
        """Must be implemented by child classes"""
        raise NotImplementedError("Subclasses must implement execute()")
