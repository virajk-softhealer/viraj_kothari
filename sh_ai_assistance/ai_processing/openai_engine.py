# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies.

import json
import logging
from datetime import datetime
from .base_engine import BaseAiEngine
from .utils import clean_ai_response, get_friendly_error_message, OdooJSONEncoder
from ...sh_ai_base.provider.openai_provider import OpenAIProvider
from ...sh_ai_base.provider.exceptions import AiStoppedException

_logger = logging.getLogger(__name__)

class OpenAiEngine(BaseAiEngine):
    """
    Processing engine for OpenAI models.
    """
    provider_type = "openai"
    provider_class = OpenAIProvider

    def _convert_tools(self, tool_declarations):
        return [{
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
                "strict": t.get("strict", False)
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
        prompt_cache_key = llm_config.get('prompt_cache_key')
        reasoning_effort = llm_config.get('reasoning_effort')
        prompt_cache_metadata = llm_config.get('prompt_cache_metadata') or {}
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
        openai_tools = self._convert_tools(tool_declarations)
        context = self._create_turn_context(
            runtime_provider_type,
            view_preference=view_preference,
            session_id=session.id if session else None,
            model_name=session.llm_id.name if session and session.llm_id else model_code,
            llm_id=session.llm_id.id if session and session.llm_id else None,
        )
        context['prompt_cache_metadata'] = prompt_cache_metadata
        context['prompt_cache_key'] = prompt_cache_key

        messages = [{"role": "system", "content": system_instruction}]
        
        # Build simple conversation history
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
                # Check if the user clicked "Stop" before starting the next turn
                if stop_event and stop_event.is_set():
                    _logger.info(f"🚫 OpenAI Engine: Stop signal received for session {llm_config.get('session_id')}")
                    return {'success': False, 'error': 'Processing was cancelled'}

                response = provider.generate_content(
                    model=model_code, messages=messages, 
                    tools=openai_tools, temperature=temperature,
                    stop_event=stop_event,
                    prompt_cache_key=prompt_cache_key,
                    reasoning_effort=reasoning_effort,
                )
                
                # Accumulate tokens
                usage = getattr(response, 'usage', None)
                if usage:
                    p_tok = getattr(usage, 'prompt_tokens', 0) or 0
                    c_tok = getattr(usage, 'completion_tokens', 0) or 0
                    t_tok = getattr(usage, 'total_tokens', 0) or 0

                    # Extract cached tokens if available
                    pt_details = getattr(usage, 'prompt_tokens_details', None)
                    cached_tokens = 0
                    if pt_details:
                        cached_tokens = getattr(pt_details, 'cached_tokens', 0) or 0

                    # Extract reasoning tokens if available (flattened by provider)
                    reasoning_tokens = getattr(usage, 'reasoning_tokens', 0) or 0
                    if not reasoning_tokens:
                        ct_details = getattr(usage, 'completion_tokens_details', None)
                        if ct_details:
                            if isinstance(ct_details, dict):
                                reasoning_tokens = ct_details.get('reasoning_tokens', 0) or 0
                            else:
                                reasoning_tokens = getattr(ct_details, 'reasoning_tokens', 0) or 0

                    self._accumulate_usage(
                        context,
                        prompt_tokens=p_tok,
                        completion_tokens=c_tok,
                        total_tokens=t_tok,
                        cached_tokens=cached_tokens,
                        reasoning_tokens=reasoning_tokens,
                    )
                    _logger.info(
                        f"📈 OpenAI Iteration {iteration+1} usage: "
                        f"P={p_tok}, C={c_tok}, T={t_tok}, Cached={context['usage']['cached_tokens']}, "
                        f"Reasoning={reasoning_tokens}"
                    )

                message = response.choices[0].message

                if message.tool_calls:
                    messages.append({
                        "role": "assistant",
                        "content": message.content,
                        "tool_calls": [
                            {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}} 
                            for tc in message.tool_calls
                        ]
                    })

                    # Odoo Safety: Use sequential execution for database thread safety
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

                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": json.dumps(result, cls=OdooJSONEncoder)
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
                        temperature=temperature,
                        stop_event=stop_event,
                        prompt_cache_key=prompt_cache_key,
                        reasoning_effort=reasoning_effort,
                    )
                    usage = getattr(final_response, 'usage', None)
                    if usage:
                        p_tok = getattr(usage, 'prompt_tokens', 0) or 0
                        c_tok = getattr(usage, 'completion_tokens', 0) or 0
                        t_tok = getattr(usage, 'total_tokens', 0) or 0
                        pt_details = getattr(usage, 'prompt_tokens_details', None)
                        cached_tokens = 0
                        if pt_details:
                            cached_tokens = getattr(pt_details, 'cached_tokens', 0) or 0
                        # Extract reasoning tokens if available (flattened by provider)
                        reasoning_tokens = getattr(usage, 'reasoning_tokens', 0) or 0
                        if not reasoning_tokens:
                            ct_details = getattr(usage, 'completion_tokens_details', None)
                            if ct_details:
                                if isinstance(ct_details, dict):
                                    reasoning_tokens = ct_details.get('reasoning_tokens', 0) or 0
                                else:
                                    reasoning_tokens = getattr(ct_details, 'reasoning_tokens', 0) or 0

                        self._accumulate_usage(
                            context,
                            prompt_tokens=p_tok,
                            completion_tokens=c_tok,
                            total_tokens=t_tok,
                            cached_tokens=cached_tokens,
                            reasoning_tokens=reasoning_tokens,
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
            _logger.info(f"🛑 OpenAI Engine: Captured partial response after stop signal")
            partial_response = e.partial_response
            
            if not partial_response:
                return self._build_result(context, "", is_stopped=True)
            
            # Accumulate tokens from partial response
            usage = getattr(partial_response, 'usage', None)
            if usage:
                p_tok = getattr(usage, 'prompt_tokens', 0) or 0
                c_tok = getattr(usage, 'completion_tokens', 0) or 0
                t_tok = getattr(usage, 'total_tokens', 0) or 0

                # Extract cached tokens from partial response
                pt_details = getattr(usage, 'prompt_tokens_details', None)
                cached_tokens = 0
                if pt_details:
                    cached_tokens = getattr(pt_details, 'cached_tokens', 0) or 0
                # Extract reasoning tokens if available (flattened by provider)
                reasoning_tokens = getattr(usage, 'reasoning_tokens', 0) or 0
                if not reasoning_tokens:
                    ct_details = getattr(usage, 'completion_tokens_details', None)
                    if ct_details:
                        if isinstance(ct_details, dict):
                            reasoning_tokens = ct_details.get('reasoning_tokens', 0) or 0
                        else:
                            reasoning_tokens = getattr(ct_details, 'reasoning_tokens', 0) or 0

                self._accumulate_usage(
                    context,
                    prompt_tokens=p_tok,
                    completion_tokens=c_tok,
                    total_tokens=t_tok,
                    cached_tokens=cached_tokens,
                    reasoning_tokens=reasoning_tokens,
                )
                _logger.info(
                    f"📈 OpenAI Partial usage: P={p_tok}, C={c_tok}, T={t_tok}, "
                    f"Cached={context['usage']['cached_tokens']}"
                )

            ai_response = partial_response.choices[0].message.content if partial_response.choices else ""
            self._mark_assistant_delta(context, ai_response)
            return self._build_result(context, clean_ai_response(ai_response), is_stopped=True)

        except Exception as e:
            _logger.error(f"OpenAI Engine Error: {str(e)}", exc_info=True)
            friendly_error = get_friendly_error_message(e)
            return self._build_result(context, "", error=friendly_error)
