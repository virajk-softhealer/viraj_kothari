# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies Pvt. Ltd.

import json
import logging
from datetime import datetime
from .base_engine import BaseAiEngine
from .utils import clean_ai_response, get_friendly_error_message, OdooJSONEncoder
from ...sh_ai_base.provider.sh_claude_provider import ClaudeProvider
from ...sh_ai_base.provider.exceptions import AiStoppedException

_logger = logging.getLogger(__name__)

class ClaudeEngine(BaseAiEngine):
    """
    Processing engine for Anthropic Claude models.
    """
    provider_type = "claude"
    provider_class = ClaudeProvider

    def _convert_tools(self, tool_declarations):
        # Anthropic uses input_schema instead of parameters
        return [{
            "name": t["name"],
            "description": t["description"],
            "input_schema": {
                "type": t["parameters"]["type"],
                "properties": t["parameters"].get("properties", {}),
                "required": t["parameters"].get("required", [])
            }
        } for t in tool_declarations]

    def execute(self, session, user_message, previous_messages, llm_config):
        api_key = llm_config.get('api_key')
        model_code = llm_config.get('model_code')
        system_instruction = llm_config.get('system_instruction')
        temperature = llm_config.get('temperature', 0.2)
        tool_declarations = llm_config.get('tool_declarations', [])
        view_preference = llm_config.get('view_preference', 'auto')
        stop_event = llm_config.get('stop_event')
        runtime_provider_type = llm_config.get('provider_type') or self.provider_type

        if not api_key:
            return {
                'success': False,
                'error': "The selected AI provider is missing an API key.",
            }
        if not model_code:
            return {
                'success': False,
                'error': "The selected AI provider is missing a model code.",
            }

        provider = self.provider_class(api_key=api_key)
        claude_tools = self._convert_tools(tool_declarations) if tool_declarations else None
        context = self._create_turn_context(
            runtime_provider_type,
            view_preference=view_preference,
            session_id=session.id if session else None,
            model_name=session.llm_id.name if session and session.llm_id else model_code,
            llm_id=session.llm_id.id if session and session.llm_id else None,
        )

        # Build conversation history
        messages = []
        for msg in previous_messages:
            role = "user" if msg.message_type == 'user' else "assistant"
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"] += f"\n\n{msg.content}"
            else:
                messages.append({"role": role, "content": msg.content})
        
        messages.append({"role": "user", "content": user_message})

        try:
            ai_response = ""
            completed = False
            for iteration in range(self.max_iterations):
                context['iteration'] = iteration
                if stop_event and stop_event.is_set():
                    _logger.info(f"🚫 Claude Engine: Stop signal received for session {llm_config.get('session_id')}")
                    return {'success': False, 'error': 'Processing was cancelled'}

                response = provider.generate_content(
                    model=model_code,
                    messages=messages,
                    system=system_instruction,
                    tools=claude_tools,
                    temperature=temperature,
                    stop_event=stop_event
                )
                
                # Accumulate tokens
                usage = getattr(response, 'usage', None)
                if usage:
                    p_tok = getattr(usage, 'prompt_tokens', 0) or 0
                    c_tok = getattr(usage, 'completion_tokens', 0) or 0
                    t_tok = getattr(usage, 'total_tokens', 0) or 0
                    self._accumulate_usage(
                        context,
                        prompt_tokens=p_tok,
                        completion_tokens=c_tok,
                        total_tokens=t_tok,
                    )
                    _logger.info(
                        f"📈 Claude Iteration {iteration+1} usage: P={p_tok}, C={c_tok}, T={t_tok}"
                    )

                message = response.choices[0].message

                if message.tool_calls:
                    # Construct assistant message for history
                    assistant_content = []
                    if message.content:
                        assistant_content.append({"type": "text", "text": message.content})
                    for tc in message.tool_calls:
                        try:
                            tc_args = json.loads(tc.function.arguments or "{}")
                        except Exception:
                            tc_args = {}
                        assistant_content.append({
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.function.name,
                            "input": tc_args
                        })
                    messages.append({
                        "role": "assistant",
                        "content": assistant_content
                    })

                    # Execute tool calls
                    tool_results_content = []
                    for tool_call in message.tool_calls:
                        tool_name = tool_call.function.name
                        try:
                            tool_args = json.loads(tool_call.function.arguments or "{}")
                        except json.JSONDecodeError:
                            malformed_error = f"The provider returned malformed tool arguments for {tool_name}."
                            return self._build_result(context, "", error=malformed_error)

                        guard_error, analysis = self._prepare_tool_call(context, tool_name, tool_args)
                        if guard_error:
                            return self._build_result(context, "", error=guard_error)

                        result, s_call, s_res, a_data = self._execute_tool_call(context, tool_name, tool_args, iteration)

                        if s_call:
                            context['last_search_call'] = s_call
                        if s_res:
                            context['last_search_result'] = s_res
                        if a_data:
                            context['action_data'] = a_data

                        self._register_tool_call(context, tool_name, tool_args, result, analysis=analysis)
                        context['debug_tool_calls'][-1]['response_id'] = tool_call.id

                        tool_results_content.append({
                            "type": "tool_result",
                            "tool_use_id": tool_call.id,
                            "content": json.dumps(result, cls=OdooJSONEncoder)
                        })
                    
                    messages.append({
                        "role": "user",
                        "content": tool_results_content
                    })
                    continue

                ai_response = message.content or ""
                self._mark_assistant_delta(context, ai_response)
                completed = True
                break

            if not completed:
                def force_finalize():
                    final_messages = list(messages)
                    final_messages.append({
                        "role": "user",
                        "content": self._build_forced_finalization_instruction(context),
                    })
                    final_response = provider.generate_content(
                        model=model_code,
                        messages=final_messages,
                        system=system_instruction,
                        temperature=temperature,
                        stop_event=stop_event,
                    )
                    usage = getattr(final_response, 'usage', None)
                    if usage:
                        p_tok = getattr(usage, 'prompt_tokens', 0) or 0
                        c_tok = getattr(usage, 'completion_tokens', 0) or 0
                        t_tok = getattr(usage, 'total_tokens', 0) or 0
                        self._accumulate_usage(
                            context,
                            prompt_tokens=p_tok,
                            completion_tokens=c_tok,
                            total_tokens=t_tok,
                        )
                    final_message = final_response.choices[0].message if final_response.choices else None
                    return final_message.content if final_message else ""

                recovered_response = self._recover_from_iteration_limit(context, provider_finalizer=force_finalize)
                if recovered_response:
                    self._mark_assistant_delta(context, recovered_response)
                    return self._build_result(context, clean_ai_response(recovered_response))

                return self._build_result(
                    context,
                    "",
                    error="The assistant stopped after too many tool steps without reaching a final answer.",
                )

            return self._build_result(context, clean_ai_response(ai_response or ""))

        except AiStoppedException as e:
            _logger.info(f"🚫 Claude Engine: Captured partial response after stop signal")
            partial_response = e.partial_response
            if not partial_response:
                return self._build_result(context, "", is_stopped=True)
            
            usage = getattr(partial_response, 'usage', None)
            if usage:
                p_tok = getattr(usage, 'prompt_tokens', 0) or 0
                c_tok = getattr(usage, 'completion_tokens', 0) or 0
                t_tok = getattr(usage, 'total_tokens', 0) or 0
                self._accumulate_usage(
                    context,
                    prompt_tokens=p_tok,
                    completion_tokens=c_tok,
                    total_tokens=t_tok,
                )

            ai_response = partial_response.choices[0].message.content if partial_response.choices else ""
            self._mark_assistant_delta(context, ai_response)
            return self._build_result(context, clean_ai_response(ai_response), is_stopped=True)

        except Exception as e:
            _logger.error(f"Claude Engine Error: {str(e)}", exc_info=True)
            friendly_error = get_friendly_error_message(e)
            return self._build_result(context, "", error=friendly_error)
