# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies Pvt. Ltd.

import json
import logging
import ssl
import urllib.request

import certifi

from .exceptions import AiStoppedException

_logger = logging.getLogger(__name__)


class ClaudeProvider:
    """Anthropic-compatible Claude provider using the Messages API."""

    def __init__(self, api_key):
        self.api_key = api_key

    def _create_completion(self, params, stream=False, stop_event=None):
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        ssl_context = ssl.create_default_context(cafile=certifi.where())

        if not stream:
            req = urllib.request.Request(
                url,
                data=json.dumps(params).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30, context=ssl_context) as response:
                res = json.loads(response.read().decode("utf-8"))
                content_text = ""
                tool_calls = []
                for block in res.get("content", []):
                    if block.get("type") == "text":
                        content_text += block.get("text", "")
                    elif block.get("type") == "tool_use":

                        class MockFunction:
                            def __init__(self, name, arguments):
                                self.name = name
                                self.arguments = arguments

                        class MockToolCall:
                            def __init__(self, id, function):
                                self.id = id
                                self.type = "function"
                                self.function = function

                        tool_calls.append(
                            MockToolCall(
                                block.get("id"),
                                MockFunction(block.get("name"), json.dumps(block.get("input", {}))),
                            )
                        )

                usage_data = res.get("usage", {})
                p_tok = usage_data.get("input_tokens", 0)
                c_tok = usage_data.get("output_tokens", 0)

                class MockMessage:
                    def __init__(self, content="", tool_calls=None):
                        self.content = content
                        self.tool_calls = tool_calls or None
                        self.role = "assistant"

                class MockChoice:
                    def __init__(self, message):
                        self.message = message
                        self.finish_reason = "stop"

                class MockUsage:
                    def __init__(self, prompt_tokens=0, completion_tokens=0):
                        self.prompt_tokens = prompt_tokens
                        self.completion_tokens = completion_tokens
                        self.total_tokens = prompt_tokens + completion_tokens
                        self.prompt_tokens_details = None
                        self.completion_tokens_details = None
                        self.reasoning_tokens = 0

                class MockResponse:
                    def __init__(self, content="", tool_calls=None, prompt_tokens=0, completion_tokens=0):
                        self.choices = [MockChoice(MockMessage(content, tool_calls))]
                        self.usage = MockUsage(prompt_tokens, completion_tokens)

                return MockResponse(content_text, tool_calls, p_tok, c_tok)

        params["stream"] = True
        req = urllib.request.Request(
            url,
            data=json.dumps(params).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        class MockMessage:
            def __init__(self, content="", tool_calls=None):
                self.content = content
                self.tool_calls = tool_calls or None
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
                self.completion_tokens_details = None
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
                tc_list = []
                for index in sorted(collected_tool_calls.keys()):
                    call = collected_tool_calls[index]
                    arguments = "".join(call["input_parts"])

                    class MockFunction:
                        def __init__(self, name, arguments):
                            self.name = name
                            self.arguments = arguments

                    class MockToolCall:
                        def __init__(self, id, function):
                            self.id = id
                            self.type = "function"
                            self.function = function

                    tc_list.append(MockToolCall(call["id"], MockFunction(call["name"], arguments)))
                full_response.choices[0].message.tool_calls = tc_list
            full_response.usage.total_tokens = full_response.usage.prompt_tokens + full_response.usage.completion_tokens
            return full_response

        try:
            with urllib.request.urlopen(req, timeout=30, context=ssl_context) as response:
                buffer = ""
                for chunk_bytes in response:
                    if stop_event and stop_event.is_set():
                        response.close()
                        raise AiStoppedException(finalize_mock())

                    buffer += chunk_bytes.decode("utf-8")
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()
                        if line.startswith("data: "):
                            data_str = line[6:].strip()
                            if data_str == "[DONE]":
                                continue
                            try:
                                event_data = json.loads(data_str)
                            except Exception:
                                continue

                            ev_type = event_data.get("type")
                            if ev_type == "message_start":
                                msg = event_data.get("message", {})
                                full_response.usage.prompt_tokens = msg.get("usage", {}).get("input_tokens", 0)
                            elif ev_type == "content_block_start":
                                block = event_data.get("content_block", {})
                                index = event_data.get("index", 0)
                                if block.get("type") == "tool_use":
                                    collected_tool_calls[index] = {
                                        "id": block.get("id"),
                                        "name": block.get("name"),
                                        "input_parts": [],
                                    }
                            elif ev_type == "content_block_delta":
                                delta = event_data.get("delta", {})
                                index = event_data.get("index", 0)
                                if delta.get("type") == "text_delta":
                                    content_parts.append(delta.get("text", ""))
                                elif delta.get("type") == "input_json_delta":
                                    if index in collected_tool_calls:
                                        collected_tool_calls[index]["input_parts"].append(delta.get("partial_json", ""))
                            elif ev_type == "message_delta":
                                usage = event_data.get("usage", {})
                                full_response.usage.completion_tokens = usage.get("output_tokens", 0)

            return finalize_mock()
        except AiStoppedException:
            raise
        except Exception as e:
            _logger.error("Claude stream failed: %s", e)
            raise

    def generate_content(self, model, messages, system=None, tools=None, temperature=0.2, stop_event=None, **kwargs):
        params = {
            "model": model,
            "messages": messages,
            "max_tokens": 4000,
        }
        if system:
            params["system"] = system
        if temperature is not None:
            params["temperature"] = temperature
        if tools:
            params["tools"] = tools

        return self._create_completion(params, stream=bool(stop_event), stop_event=stop_event)
