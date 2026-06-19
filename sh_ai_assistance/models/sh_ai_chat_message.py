# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies.

from odoo import api, fields, models
import logging
from datetime import datetime, timedelta
import re

from ...sh_ai_base.provider.odoo_tools import (
    get_search_records_declaration, search_records,
    get_models_list_declaration, get_models_list,
    get_model_fields_declaration, get_model_fields,
    get_aggregate_records_declaration, aggregate_records,
    get_selection_values_declaration, get_selection_values,
    get_current_date_info_declaration, get_current_date_info,
    get_open_view_declaration, open_view,
    fuzzy_lookup_declaration
)
from ...sh_ai_base.provider.prompt_builder import PromptBuilder
from ..ai_processing import AiEngineFactory
from ..ai_processing.write_tools import (
    get_prepare_archive_records_declaration,
    get_prepare_create_record_declaration,
    get_prepare_update_records_declaration,
    get_write_prompt_sections,
)
from ..ai_processing.utils import sanitize_for_json
import os
import signal
import threading

_logger = logging.getLogger(__name__)

# SIMPLE: This is like a "Switchboard". It keeps track of which AI is currently talking.
# If we set an event to 'ON', the AI stops talking immediately.
# TECHNICAL: Global registry of active events to allow cross-worker signaling via OS signals
ACTIVE_AI_EVENTS = {} # { session_id (int): threading.Event() }
ACTIVE_AI_STATUS = {} # { session_id (int): dict(status payload) }

def handle_ai_stop_signal(signum, frame):
    """Signal handler to set stop events for all active AI requests in this process"""
    _logger.info(f"⚡ Received stop signal ({signum}) in PID {os.getpid()}, stopping {len(ACTIVE_AI_EVENTS)} active requests")
    for event in list(ACTIVE_AI_EVENTS.values()):
        event.set()

# SIMPLE: This is the "Emergency Stop" button listener. 
# It waits for a signal and then tells all active AI threads in this worker to stop.
# TECHNICAL: Register the signal handler for SIGUSR1. Python will trigger 
# handle_ai_stop_signal when this process receives the kill signal.
if os.name != 'nt': # Signals don't work the same on Windows
    try:
        signal.signal(signal.SIGUSR1, handle_ai_stop_signal)
    except ValueError:
        # In some contexts (like some threads), signal registration might fail
        _logger.warning("Could not register SIGUSR1 handler")

class ShAiChatMessage(models.Model):
    _name = 'sh.ai.chat.message'
    _description = 'AI Chat Message'
    _order = 'create_date asc'

    session_id = fields.Many2one('sh.ai.chat.session', string="Chat Session", required=True, ondelete='cascade')
    message_type = fields.Selection([
        ('user', 'User Message'),
        ('assistant', 'Assistant Response'),
        ('system', 'System Message'),
        ('error', 'Error Message')
    ], string="Message Type", required=True, default='user')
    content = fields.Text(string="Message Content", required=True)
    llm_id = fields.Many2one('sh.ai.llm', string="LLM Provider", help="The LLM provider that generated this message")
    write_request_id = fields.Many2one('sh.ai.write.request', string="Write Request", ondelete='set null')
    action_data = fields.Json(string="Action Data", help="Store action data for 'View All' button")
    query_details = fields.Json(string="Query Details", help="The primary database operation performed (for UI display)")
    debug_info = fields.Json(string="Debug Information")
    debug_info_formatted = fields.Text(string="Debug Info (Formatted)", compute="_compute_debug_info_formatted")
    
    # Usage Statistics
    prompt_tokens = fields.Integer(string="Prompt Tokens")
    completion_tokens = fields.Integer(string="Completion Tokens")
    cached_tokens = fields.Integer(string="Cached Tokens", help="Tokens served from OpenAI's prompt cache")
    sh_reasoning_tokens = fields.Integer(string="Reasoning Tokens", help="Tokens used for reasoning in o1/o3 models")
    total_tokens = fields.Integer(string="Total Tokens")
    execution_time = fields.Float(string="Execution Time (s)")
    tool_call_count = fields.Integer(string="Tool Calls")
    sh_is_cancelled = fields.Boolean(string="Is Cancelled", default=False)
    sh_reasoning_effort = fields.Selection([
        ('minimal', 'Minimal'),
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
    ], string="Reasoning Effort")

    # Compatibility
    user_query = fields.Text(string="User Query")
    ai_response = fields.Text(string="AI Response")

    @api.depends('debug_info')
    def _compute_debug_info_formatted(self):
        import json
        for record in self:
            if record.debug_info:
                try:
                    record.debug_info_formatted = json.dumps(record.debug_info, indent=2, ensure_ascii=False)
                except Exception:
                    record.debug_info_formatted = str(record.debug_info)
            else:
                record.debug_info_formatted = "{}"

    @api.model
    def get_model_display_names(self, model_names):
        """Resolve user-facing model labels for chat UI fallbacks."""
        result = {}
        for model_name in model_names or []:
            if not model_name or model_name in result:
                continue
            display_name = model_name
            try:
                model = self.env[model_name]
                if getattr(model, "_description", None):
                    display_name = model._description
                else:
                    ir_model = self.env["ir.model"].sudo().search([("model", "=", model_name)], limit=1)
                    if ir_model and ir_model.display_name:
                        display_name = ir_model.display_name
            except Exception:
                ir_model = self.env["ir.model"].sudo().search([("model", "=", model_name)], limit=1)
                if ir_model and ir_model.display_name:
                    display_name = ir_model.display_name
            result[model_name] = display_name
        return result

    @api.model
    def cancel_last_user_message(self, session_id):
        """Mark the most recent user message in a session as cancelled and signal the worker"""
        last_user_msg = self.search([
            ('session_id', '=', session_id),
            ('message_type', '=', 'user')
        ], order='id desc', limit=1)
        if last_user_msg:
            # 1. Mark user message as cancelled
            last_user_msg.write({'sh_is_cancelled': True})
            
            # 2. Check if an assistant/error message was already created for this turn and DELETE it
            last_ai_msg = self.search([
                ('session_id', '=', session_id),
                ('message_type', 'in', ['assistant', 'error']),
                ('id', '>', last_user_msg.id)
            ], order='id desc', limit=1)
            if last_ai_msg:
                _logger.info(f"🗑️ Unlinking cancelled AI response for session {session_id}")
                last_ai_msg.unlink()

            # 3. Direct Event Set (for threads within the same worker process)
            if session_id in ACTIVE_AI_EVENTS:
                _logger.info(f"🛑 Setting local stop event for session {session_id}")
                ACTIVE_AI_EVENTS[session_id].set()

            # 4. BIG MOMENT: Send an "Emergency Signal" to the background worker.
            # SIMPLE: Odoo has many workers. We find the person actually doing the work 
            # and send them a "Poke" (SIGUSR1) to tell them to stop.
            # TECHNICAL: Uses os.kill(pid, signal) to trigger the signal handler 
            # in the specific Odoo worker process that owns the AI request.
            session = self.env['sh.ai.chat.session'].browse(session_id)
            if session.sh_active_worker_pid:
                _logger.info(f"📣 Sending SIGUSR1 to PID {session.sh_active_worker_pid} to stop session {session_id}")
                try:
                    os.kill(session.sh_active_worker_pid, signal.SIGUSR1)
                except (ProcessLookupError, AttributeError):
                    _logger.warning(f"Process {session.sh_active_worker_pid} not found or OS doesn't support kill")
                except Exception as e:
                    _logger.error(f"Failed to signal process: {e}")
            self._set_processing_status(session_id, {
                'active': False,
                'phase': 'cancelled',
                'title': 'Request cancelled',
                'detail': 'Generation was stopped before a final answer was saved.',
            })
            return True
        return False

    @api.model
    def _set_processing_status(self, session_id, status):
        """Expose lightweight real-time processing status for the active chat session."""
        if not session_id:
            return False
        session = self.env['sh.ai.chat.session'].browse(session_id)
        if not session.exists():
            return False
        status_payload = sanitize_for_json(status) or {}
        status_payload['session_id'] = session_id
        status_payload['updated_at'] = fields.Datetime.now().isoformat()
        ACTIVE_AI_STATUS[session_id] = status_payload
        try:
            self.env['bus.bus']._sendone(session.user_id.partner_id, "sh_ai_assistance/processing_status", status_payload)
        except Exception:
            _logger.debug("Failed to push processing status on bus", exc_info=True)
        return True

    @api.model
    def _clear_processing_status(self, session_id):
        session = self.env['sh.ai.chat.session'].browse(session_id)
        ACTIVE_AI_STATUS.pop(session_id, None)
        if session.exists():
            try:
                self.env['bus.bus']._sendone(session.user_id.partner_id, "sh_ai_assistance/processing_status", {
                    'active': False,
                    'session_id': session_id,
                    'updated_at': fields.Datetime.now().isoformat(),
                })
            except Exception:
                _logger.debug("Failed to push processing clear on bus", exc_info=True)
        return True

    @api.model
    def get_processing_status(self, session_id):
        """Return current backend processing status for a visible session."""
        session = self.env['sh.ai.chat.session'].search([('id', '=', session_id)], limit=1)
        if not session:
            return {'active': False}
        return sanitize_for_json(ACTIVE_AI_STATUS.get(session_id, {'active': False}))

    @api.model
    def _check_cancellation(self, session_id, message_id=None):
        """Check if the initial user message was cancelled"""
        check_msg = False
        if message_id:
            check_msg = self.browse(message_id)
        else:
            # Fallback: find the last user message for this session
            check_msg = self.search([
                ('session_id', '=', session_id),
                ('message_type', '=', 'user')
            ], order='id desc', limit=1)

        return check_msg

    def action_view_debug_info(self):
        """Open debug information popup"""
        self.ensure_one()

        if not self.debug_info:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'message': 'No debug information available for this message.',
                    'type': 'warning',
                    'sticky': False,
                }
            }

        return {
            'name': 'AI Response Debug Info',
            'type': 'ir.actions.act_window',
            'res_model': 'sh.ai.chat.message',
            'res_id': self.id,
            'view_mode': 'form',
            'view_id': self.env.ref('sh_ai_assistance.view_sh_ai_chat_message_debug_form').id,
            'target': 'new',
            'context': {'debug_mode': True}
        }

    def open_records_action(self, view_type='list'):
        """Open records in specified view type"""
        self.ensure_one()

        if not self.action_data:
            return {'type': 'ir.actions.act_window_close'}

        action_data = self.action_data
        model_name = action_data.get('model')
        domain = action_data.get('domain', [])
        group_by = action_data.get('group_by')

        _logger.info(f"Opening {view_type} view: model={model_name}, domain={domain}, group_by={group_by}")

        # Ensure domain is a proper list format
        if not isinstance(domain, list):
            domain = []

        # Get model display name
        try:
            model_obj = self.env[model_name]
            model_display_name = model_obj._description or model_name
        except Exception:
            model_display_name = model_name

        # Build context with group_by if provided
        context = {'create': False}
        if group_by:
            context['group_by'] = [group_by]
            _logger.info(f"Applying group_by: {group_by}")

        # Action with specified view type
        action = {
            'type': 'ir.actions.act_window',
            'name': model_display_name,
            'res_model': model_name,
            'view_mode': view_type,
            'views': [(False, view_type)],
            'domain': domain,
            'target': 'new',  # Open in main view, not popup
            'context': context,
        }

        _logger.info(f"Returning {view_type} action: {action}")
        return action

    @api.model
    def process_user_message(self, session_id, user_message, message_id=None, view_preference='auto'):
        """Process user message using modular AI processing engines"""
        from datetime import datetime
        session = self.env['sh.ai.chat.session'].browse(session_id)
        if not session.exists() or not session.llm_id:
            return {'error': 'Invalid session or LLM configuration'}

        # Register this session/worker pair globally for signal-based stop
        session.write({'sh_active_worker_pid': os.getpid()})
        stop_event = threading.Event()
        ACTIVE_AI_EVENTS[session_id] = stop_event
        self._set_processing_status(session_id, {
            'active': True,
            'phase': 'starting',
            'model_name': session.llm_id.name,
            'title': 'Starting request',
            'detail': 'Sending your question to the selected model.',
        })

        try:
            # 0. Check for initial cancellation
            check_msg = self._check_cancellation(session_id, message_id)

            if check_msg and check_msg.exists() and check_msg.sh_is_cancelled:
                _logger.info(f"🚫 Processing was cancelled before starting for session {session_id}")
                self._set_processing_status(session_id, {
                    'active': False,
                    'phase': 'cancelled',
                    'model_name': session.llm_id.name,
                    'title': 'Request cancelled',
                    'detail': 'Generation was stopped before processing began.',
                })
                return {'error': 'Processing was cancelled'}

            llm = session.llm_id
            provider_type = llm._detect_provider_type()
            start_time = datetime.now()

            # 1. Discovery & Session Maintenance
            user_message_count = self.search_count([('session_id', '=', session_id), ('message_type', '=', 'user')])
            if user_message_count == 1:
                session.auto_rename_from_message(user_message)

            # Refresh model catalog if expired or missing
            catalog = session.model_catalog
            if not catalog or (session.catalog_timestamp and (datetime.now() - session.catalog_timestamp) > timedelta(hours=1)):
                catalog_result = get_models_list(self.env)
                if catalog_result.get('success'):
                    catalog = catalog_result.get('models', [])
                    session.write({'model_catalog': catalog, 'catalog_timestamp': datetime.now()})

            # 2. Tool & Prompt Setup
            tool_declarations = [
                get_search_records_declaration(),
                get_models_list_declaration(),
                get_model_fields_declaration(),
                get_aggregate_records_declaration(),
                get_selection_values_declaration(),
                get_current_date_info_declaration(),
                get_open_view_declaration(),
                fuzzy_lookup_declaration(),
                get_prepare_create_record_declaration(),
                get_prepare_update_records_declaration(),
                get_prepare_archive_records_declaration(),
            ]

            # Get history for context
            history_domain = [
                ('session_id', '=', session_id),
                ('message_type', 'in', ['user', 'assistant', 'error']),
                ('sh_is_cancelled', '=', False)
            ]
            if message_id:
                history_domain.append(('id', '<', message_id))
            
            previous_messages = self.search(history_domain, order='id asc')

            # Build dynamic prompt
            prompt_builder = PromptBuilder(self.env)
            prompt_package = prompt_builder.build_prompt_package(
                system_prompt=llm.system_prompt,
                tool_declarations=tool_declarations,
                model_catalog=catalog,
                is_followup=session.is_context_initialized,
                extra_stable_sections=get_write_prompt_sections(),
                instructions={
                    'context': llm.context_instruction,
                    'workflow': llm.workflow_instruction,
                    'tool': llm.tool_instruction,
                    'formatting': llm.formatting_instruction,
                    'security': llm.security_instruction,
                    'critical': llm.critical_instruction,
                    'examples': llm.examples_instruction,
                    'previous_messages': previous_messages
                }
            )
            complete_system_instruction = prompt_package['full_prompt']
            cache_metadata = prompt_package['cache_metadata']
            prompt_cache_key = "odoo:{db}:{llm}:{prefix}".format(
                db=self.env.cr.dbname,
                llm=llm.id,
                prefix=cache_metadata['stable_prefix_hash'][:32],
            )

            # 3. Get Engine & Execute
            normalized_view_preference = (view_preference or 'auto').lower()
            if normalized_view_preference not in ('auto', 'show', 'hide'):
                normalized_view_preference = 'auto'

            llm_config = {
                'api_key': llm.sudo().sh_api_key,
                'model_code': llm.sh_model_code,
                'provider_type': provider_type,
                'openrouter_model_id': llm.openrouter_model_id.id if hasattr(llm, 'openrouter_model_id') and llm.openrouter_model_id else False,
                'system_instruction': complete_system_instruction,
                'temperature': {'precise': 0.2, 'balanced': 0.5, 'creative': 0.8}.get(llm.temperature, 0.2),
                'reasoning_effort': llm.sh_reasoning_effort if llm.is_reasoning_model else None,
                'tool_declarations': tool_declarations,
                'view_preference': normalized_view_preference,
                'message_id': message_id,
                'session_id': session_id,
                'stop_event': stop_event,
                'prompt_cache_key': prompt_cache_key,
                'prompt_cache_metadata': cache_metadata,
            }

            engine = AiEngineFactory.get_engine(self.env, provider_type)
            result = engine.execute(session, user_message, previous_messages, llm_config)
            
            execution_duration = (datetime.now() - start_time).total_seconds()

            # 4. FINAL CANCELLATION CHECK: Skip storage entirely if cancelled or stopped
            check_msg = self._check_cancellation(session_id, message_id)
            is_stopped = result.get('stopped', False) or (stop_event and stop_event.is_set())
            
            if (check_msg and check_msg.exists() and check_msg.sh_is_cancelled) or is_stopped:
                _logger.info(f"🚫 AI Processing was cancelled for session {session_id}, skipping storage as requested.")
                self._set_processing_status(session_id, {
                    'active': False,
                    'phase': 'cancelled',
                    'model_name': session.llm_id.name,
                    'title': 'Request cancelled',
                    'detail': 'Generation was stopped before a final answer was stored.',
                })
                return {'error': 'Processing was cancelled'}

            # 5. Persistence
            if result.get('success'):
                # Mark context as initialized after first successful data query
                if not session.is_context_initialized:
                    session.write({'is_context_initialized': True})

                msg_vals = {
                    'session_id': session_id,
                    'message_type': 'assistant',
                    'content': result['content'],
                    'llm_id': llm.id,
                    'write_request_id': result.get('write_request_id'),
                    'action_data': result.get('action_data'),
                    'debug_info': result.get('debug_info'),
                    'query_details': result.get('query_details'),
                    'prompt_tokens': result.get('usage', {}).get('prompt_tokens', 0),
                    'completion_tokens': result.get('usage', {}).get('completion_tokens', 0),
                    'cached_tokens': result.get('usage', {}).get('cached_tokens', 0),
                    'sh_reasoning_tokens': result.get('usage', {}).get('reasoning_tokens', 0),
                    'total_tokens': result.get('usage', {}).get('total_tokens', 0),
                    'tool_call_count': result.get('tool_call_count', 0),
                    'execution_time': execution_duration,
                    'sh_reasoning_effort': llm.sh_reasoning_effort if llm.is_reasoning_model else None,
                }
                 # Add metadata to debug info
                if 'debug_info' in msg_vals and msg_vals['debug_info']:
                    msg_vals['debug_info']['model_catalog_cached'] = True
                    msg_vals['debug_info']['execution_time'] = execution_duration
                    msg_vals['debug_info']['was_stopped'] = is_stopped
                
                # FINAL SAFETY: Sanitize all JSON fields to prevent serialization errors
                for field in ['action_data', 'query_details', 'debug_info']:
                    if msg_vals.get(field):
                        msg_vals[field] = sanitize_for_json(msg_vals[field])

                assistant_message = self.create(msg_vals)
                if result.get('write_request_id'):
                    self.env['sh.ai.write.request'].browse(result['write_request_id']).write({
                        'message_id': assistant_message.id,
                    })
                return {'success': True, 'response': result['content']}
            else:
                # Create error message
                self.create({
                    'session_id': session_id,
                    'message_type': 'error',
                    'content': result.get('error', "I encountered an issue while processing your request."),
                    'debug_info': result.get('debug_info'),
                    'llm_id': llm.id,
                    'execution_time': execution_duration
                })
                return {'error': result.get('error')}
        finally:
            # Cleanup registry and PID tracking
            ACTIVE_AI_EVENTS.pop(session_id, None)
            self._clear_processing_status(session_id)
            if session.exists():
                session.write({'sh_active_worker_pid': 0})
