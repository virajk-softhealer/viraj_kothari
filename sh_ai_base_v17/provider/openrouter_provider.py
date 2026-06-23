# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies.

from openai import OpenAI

from .exceptions import AiStoppedException


class OpenRouterProvider:
    def __init__(self, api_key):
        self.client = OpenAI(
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
            default_headers={
                "HTTP-Referer": "https://openrouter.ai",
                "X-OpenRouter-Title": "Odoo SH AI Assistance",
            },
        )

    def _create_completion(self, params):
        try:
            return self.client.chat.completions.create(**params)
        except TypeError:
            params = dict(params)
            params.pop("prompt_cache_key", None)
            return self.client.chat.completions.create(**params)

    def generate_content(self, model, messages, tools=None, temperature=0.2, stop_event=None, prompt_cache_key=None, reasoning_effort=None):
        params = {
            "model": model,
            "messages": messages,
        }

        if "o1" not in (model or "") and "o3" not in (model or "") and "gpt-5" not in (model or ""):
            params["temperature"] = temperature

        if reasoning_effort:
            params["reasoning_effort"] = reasoning_effort

        if tools:
            params["tools"] = tools
            params["tool_choice"] = "auto"

        if prompt_cache_key:
            params["prompt_cache_key"] = prompt_cache_key

        if not stop_event:
            return self._create_completion(params)

        params["stream"] = True
        params["stream_options"] = {"include_usage": True}
        stream = self._create_completion(params)

        class MockMessage:
            def __init__(self, content="", tool_calls=None):
                self.content = content
                self.tool_calls = tool_calls
                self.role = "assistant"

        class MockChoice:
            def __init__(self, message):
                self.message = message
                self.finish_reason = "stop"

        class MockUsage:
            def __init__(self):
                self.prompt_tokens = 0
                self.completion_tokens = 0
                self.total_tokens = 0
                self.prompt_tokens_details = None
                self.reasoning_tokens = 0

        class MockResponse:
            def __init__(self):
                self.choices = [MockChoice(MockMessage())]
                self.usage = MockUsage()

        full_response = MockResponse()
        content_parts = []
        collected_tool_calls = {}

        def finalize_mock():
            full_response.choices[0].message.content = "".join(content_parts)
            if collected_tool_calls:
                from types import SimpleNamespace

                tc_list = []
                for tc in sorted(collected_tool_calls.keys()):
                    call = collected_tool_calls[tc]
                    func = SimpleNamespace(name=call["function"]["name"], arguments=call["function"]["arguments"])
                    tc_list.append(SimpleNamespace(id=call["id"], type="function", function=func))
                full_response.choices[0].message.tool_calls = tc_list
            return full_response

        try:
            for chunk in stream:
                if stop_event.is_set():
                    stream.close()
                    raise AiStoppedException(finalize_mock())

                usage = getattr(chunk, "usage", None)
                if usage:
                    full_response.usage.prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
                    full_response.usage.completion_tokens = getattr(usage, "completion_tokens", 0) or 0
                    full_response.usage.total_tokens = getattr(usage, "total_tokens", 0) or 0
                    if hasattr(usage, "prompt_tokens_details"):
                        full_response.usage.prompt_tokens_details = usage.prompt_tokens_details
                    ct_details = getattr(usage, "completion_tokens_details", None)
                    if ct_details:
                        if isinstance(ct_details, dict):
                            full_response.usage.reasoning_tokens = ct_details.get("reasoning_tokens", 0) or 0
                        else:
                            full_response.usage.reasoning_tokens = getattr(ct_details, "reasoning_tokens", 0) or 0

                if not chunk.choices:
                    continue

                delta = chunk.choices[0].delta
                if hasattr(delta, "content") and delta.content:
                    content_parts.append(delta.content)

                if hasattr(delta, "tool_calls") and delta.tool_calls:
                    for tc in delta.tool_calls:
                        index = tc.index
                        if index not in collected_tool_calls:
                            collected_tool_calls[index] = {
                                "id": tc.id,
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        if tc.id:
                            collected_tool_calls[index]["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                collected_tool_calls[index]["function"]["name"] += tc.function.name
                            if tc.function.arguments:
                                collected_tool_calls[index]["function"]["arguments"] += tc.function.arguments

            return finalize_mock()

        except AiStoppedException:
            raise
        except Exception:
            raise
