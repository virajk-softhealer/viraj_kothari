# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies.

import logging
from datetime import datetime
from google.genai import types
from .base_engine import BaseAiEngine
from .utils import clean_ai_response, get_friendly_error_message, OdooJSONEncoder
from ...sh_ai_base.provider.gemini_provider import GeminiProvider
from ...sh_ai_base.provider.exceptions import AiStoppedException

_logger = logging.getLogger(__name__)

class GeminiEngine(BaseAiEngine):
    """
    Processing engine for Google Gemini models with multi-tool turn support.
    """

    def _clean_schema(self, schema):
        """
        SIMPLE: Gemini is picky about how tool descriptions are formatted. This method 
        simplifies Odoo's complex data structures so Gemini can understand them without errors.

        TECHNICAL: Recursively strips unsupported JSON schema keys (strict, title, etc.) 
        and flattens 'anyOf' configurations into a single type with a 'nullable' flag.
        """
        if not isinstance(schema, dict):
            return schema
            
        cleaned = dict(schema)
        
        # 0. Handle list type (Gemini only supports single type or nullable)
        type_val = cleaned.get('type')
        if isinstance(type_val, list):
            real_types = [t for t in type_val if t != 'null']
            if real_types:
                cleaned['type'] = real_types[0]
            if 'null' in type_val or 'nullable' in cleaned:
                cleaned['nullable'] = True

        # 1. Simplify 'anyOf' - Gemini struggles with multiple branches
        if 'anyOf' in cleaned:
            real_types = [item for item in cleaned['anyOf'] if item.get('type') != 'null']
            nullable = any(item.get('type') == 'null' for item in cleaned['anyOf']) or cleaned.get('nullable')
            
            if real_types:
                # Merge the first real type's properties
                primary = self._clean_schema(real_types[0])
                # Replacing entirely is safer than merging to avoid stale 'properties' or 'required'
                cleaned = dict(primary)
                if nullable:
                    cleaned['nullable'] = True
            else:
                del cleaned['anyOf']

        # 2. Convert raw 'null' type
        if cleaned.get('type') == 'null':
            cleaned['nullable'] = True
            cleaned['type'] = 'string' 
        
        # 3. Strip non-Gemini or unsupported keys
        # Gemini 1.5 is extremely strict about these fields
        for key in ['strict', 'additionalProperties', 'example', 'default', 'title']:
            if key in cleaned:
                del cleaned[key]

        # 4. Recurse properties
        if 'properties' in cleaned:
            cleaned['properties'] = {k: self._clean_schema(v) for k, v in cleaned['properties'].items()}
        
        # 5. Recurse items
        if 'items' in cleaned:
            cleaned['items'] = self._clean_schema(cleaned['items'])
            
        return cleaned

    def _convert_tools(self, tool_declarations):
        """Clean tool declarations for Gemini compatibility"""
        cleaned = []
        for t in tool_declarations:
            t_copy = dict(t)
            if "parameters" in t_copy:
                t_copy["parameters"] = self._clean_schema(t_copy["parameters"])
            
            # Remove strict key at top level if present
            if "strict" in t_copy: del t_copy["strict"]
            cleaned.append(t_copy)
        return cleaned

    def execute(self, session, user_message, previous_messages, llm_config):
        api_key = llm_config.get('api_key')
        model_code = llm_config.get('model_code')
        system_instruction = llm_config.get('system_instruction')
        temperature = llm_config.get('temperature', 0.2)
        tool_declarations = llm_config.get('tool_declarations', [])
        view_preference = llm_config.get('view_preference', 'auto')
        stop_event = llm_config.get('stop_event')
        prompt_cache_metadata = llm_config.get('prompt_cache_metadata') or {}

        provider = GeminiProvider(api_key=api_key)
        context = self._create_turn_context(
            'gemini',
            view_preference=view_preference,
            session_id=session.id if session else None,
            model_name=session.llm_id.name if session and session.llm_id else model_code,
            llm_id=session.llm_id.id if session and session.llm_id else None,
        )
        context['prompt_cache_metadata'] = prompt_cache_metadata
        
        # 1. Clean and convert tools (only if declarations exist)
        google_tools = None
        if tool_declarations:
            google_tools = types.Tool(function_declarations=self._convert_tools(tool_declarations))
        
        # 2. Tool configuration (only if tools exist)
        tool_config = None
        if google_tools:
            tool_config = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode=types.FunctionCallingConfigMode.AUTO
                )
            )
        
        reasoning_effort = llm_config.get('reasoning_effort')

        # 3. Build dynamic config
        config_args = {
            'system_instruction': system_instruction,
            'temperature': temperature,
        }
        if google_tools:
            config_args['tools'] = [google_tools]
            config_args['tool_config'] = tool_config

        if reasoning_effort:
            try:
                # Add thinking config if SDK supports it. If model rejects it, it will fail naturally.
                config_args['thinking_config'] = types.ThinkingConfig(thinking_level=reasoning_effort.upper())
            except Exception:
                pass

        config = types.GenerateContentConfig(**config_args)

        # Build conversation history with strict role alternation for Gemini
        contents = []
        for msg in previous_messages:
            role = "user" if msg.message_type == 'user' else "model"
            msg_content = msg.content or ""
            
            # Gemini strictly requires turn alternation (User -> Model -> User -> Model)
            if contents and contents[-1].role == role:
                # If consecutive same-role messages, merge their content into the previous turn
                prev_text = contents[-1].parts[0].text if contents[-1].parts else ""
                contents[-1].parts = [types.Part(text=f"{prev_text}\n\n{msg_content}")]
            else:
                contents.append(types.Content(role=role, parts=[types.Part(text=msg_content)]))
        
        # Ensure the conversation starts with a User turn (Gemini requirement)
        if contents and contents[0].role != 'user':
            contents.pop(0)

        contents.append(types.Content(role="user", parts=[types.Part(text=user_message)]))

        try:
            session_id = llm_config.get('session_id')
            ai_response = ""
            completed = False
            for iteration in range(self.max_iterations):
                context['iteration'] = iteration
                # Check for cancellation between turns (Instant Event Check)
                if stop_event and stop_event.is_set():
                    _logger.info(f"🚫 Gemini Engine: Stop signal received for session {session_id}")
                    return {'success': False, 'error': 'Processing was cancelled'}

                response = provider.generate_content(
                    model=model_code, 
                    contents=contents, 
                    config=config,
                    stop_event=stop_event
                )
                
                # Accumulate tokens
                usage = getattr(response, 'usage_metadata', None)
                if usage:
                    self._accumulate_usage(
                        context,
                        prompt_tokens=(usage.prompt_token_count or 0),
                        completion_tokens=(usage.candidates_token_count or 0),
                        total_tokens=(usage.total_token_count or 0),
                        cached_tokens=getattr(usage, 'cached_content_token_count', 0) or 0,
                        reasoning_tokens=getattr(usage, 'thoughts_token_count', 0) or 0,
                    )

                has_function_call = False
                if response.candidates:
                    candidate = response.candidates[0]
                    if hasattr(candidate, 'content') and candidate.content.parts:
                        # Find all function calls in the current candidate
                        function_calls = [p.function_call for p in candidate.content.parts if hasattr(p, 'function_call') and p.function_call]
                        
                        if function_calls:
                            has_function_call = True
                            _logger.info(f"⚡ Gemini multi-call: Processing {len(function_calls)} tools")
                            
                            # 1. Add the model's call to history
                            contents.append(candidate.content)
                            
                            # 2. Execute all calls and collect response parts
                            # SIMPLE: If the AI asks for multiple things at once, we do them 
                            # one by one to keep Odoo safe, then send everything back in one go.
                            response_parts = []
                            for function_call in function_calls:
                                tool_name = function_call.name
                                tool_args = dict(function_call.args)
                                guard_error, analysis = self._prepare_tool_call(context, tool_name, tool_args)
                                if guard_error:
                                    return self._build_result(context, "", error=guard_error)

                                # Execute Tool (sequentially for Odoo safety)
                                result, s_call, s_res, a_data = self._execute_tool_call(
                                    context,
                                    tool_name, tool_args, iteration
                                )

                                if s_call:
                                    context['last_search_call'] = s_call
                                if s_res:
                                    context['last_search_result'] = s_res
                                if a_data:
                                    context['action_data'] = a_data

                                self._register_tool_call(context, tool_name, tool_args, result, analysis=analysis)

                                response_parts.append(types.Part(
                                    function_response=types.FunctionResponse(
                                        id=getattr(function_call, 'id', None),
                                        name=tool_name,
                                        response={"result": result},
                                    )
                                ))
                            
                            # 3. Add all responses as a single turn from 'user'
                            contents.append(types.Content(role="user", parts=response_parts))
                            continue # Check if more steps are needed

                # If no function calls, it's the final text response
                if not has_function_call:
                    ai_response = response.text if hasattr(response, 'text') else None
                    if not ai_response and response.candidates:
                        parts = [p.text for p in response.candidates[0].content.parts if hasattr(p, 'text') and p.text]
                        ai_response = ' '.join(parts) if parts else ""
                    
                    if not ai_response:
                        ai_response = "I've processed your request."
                    self._mark_assistant_delta(context, ai_response)
                    completed = True
                    break

            if not completed:
                def force_finalize():
                    final_instruction = self._build_forced_finalization_instruction(context)
                    final_contents = list(contents)
                    if final_contents and getattr(final_contents[-1], 'role', None) == "user":
                        final_contents[-1] = types.Content(
                            role="user",
                            parts=list(final_contents[-1].parts) + [types.Part(text=final_instruction)],
                        )
                    else:
                        final_contents.append(
                            types.Content(role="user", parts=[types.Part(text=final_instruction)])
                        )

                    final_config = types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        temperature=temperature,
                    )
                    if reasoning_effort:
                        try:
                            final_config.thinking_config = types.ThinkingConfig(thinking_level=reasoning_effort.upper())
                        except Exception:
                            pass
                    final_response = provider.generate_content(
                        model=model_code,
                        contents=final_contents,
                        config=final_config,
                        stop_event=stop_event,
                    )
                    forced_text = final_response.text if hasattr(final_response, 'text') else None
                    if not forced_text and final_response.candidates:
                        parts = [
                            p.text
                            for p in final_response.candidates[0].content.parts
                            if hasattr(p, 'text') and p.text
                        ]
                        forced_text = ' '.join(parts) if parts else ""
                    if forced_text:
                        usage = getattr(final_response, 'usage_metadata', None)
                        if usage:
                            self._accumulate_usage(
                                context,
                                prompt_tokens=(usage.prompt_token_count or 0),
                                completion_tokens=(usage.candidates_token_count or 0),
                                total_tokens=(usage.total_token_count or 0),
                                cached_tokens=getattr(usage, 'cached_content_token_count', 0) or 0,
                                reasoning_tokens=getattr(usage, 'thoughts_token_count', 0) or 0,
                            )
                    return forced_text

                recovered_response = self._recover_from_iteration_limit(context, provider_finalizer=force_finalize)
                if recovered_response:
                    self._mark_assistant_delta(context, recovered_response)
                    return self._build_result(context, clean_ai_response(recovered_response))

                return self._build_result(
                    context,
                    "",
                    error="The assistant stopped after too many tool steps without reaching a final answer.",
                )

            return self._build_result(context, clean_ai_response(ai_response))

        except AiStoppedException as e:
            # SIMPLE: User clicked STOP. We catch the partial answer so far and save it.
            _logger.info(f"🛑 Gemini Engine: Captured partial response after stop signal")
            partial_response = e.partial_response
            
            if not partial_response:
                return self._build_result(context, "", is_stopped=True)

            # Accumulate final tokens from partial response
            usage = getattr(partial_response, 'usage_metadata', None)
            if usage:
                self._accumulate_usage(
                    context,
                    prompt_tokens=(usage.prompt_token_count or 0),
                    completion_tokens=(usage.candidates_token_count or 0),
                    total_tokens=(usage.total_token_count or 0),
                    cached_tokens=getattr(usage, 'cached_content_token_count', 0) or 0,
                    reasoning_tokens=getattr(usage, 'thoughts_token_count', 0) or 0,
                )

            ai_response = partial_response.text if hasattr(partial_response, 'text') else ""
            if not ai_response and partial_response.candidates:
                parts = [p.text for p in partial_response.candidates[0].content.parts if hasattr(p, 'text') and p.text]
                ai_response = ' '.join(parts) if parts else ""
            self._mark_assistant_delta(context, ai_response)
            
            return self._build_result(context, clean_ai_response(ai_response), is_stopped=True)

        except Exception as e:
            _logger.error(f"❌ Gemini Engine Execution Error: {str(e)}", exc_info=True)
            friendly_msg = get_friendly_error_message(e)
            return self._build_result(context, "", error=friendly_msg)
