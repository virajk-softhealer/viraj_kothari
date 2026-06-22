# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies.

import json
import ssl
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import certifi

from .exceptions import AiStoppedException


class ClaudeProvider:
    """Anthropic-compatible Claude provider using the Messages API."""

    api_url = "https://api.anthropic.com/v1/messages"

    def __init__(self, api_key):
        self.api_key = api_key

    def _normalize_text(self, value):
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return str(value)

    def _get_value(self, payload, key, default=None):
        if isinstance(payload, dict):
            return payload.get(key, default)
        return getattr(payload, key, default)

    def _convert_tool_declarations(self, tool_declarations):
        converted = []
        for tool in tool_declarations or []:
            converted.append({
                "name": tool.get("name"),
                "description": tool.get("description"),
                "input_schema": tool.get("parameters") or {},
            })
        return converted

    def _convert_messages(self, messages):
        system_parts = []
        converted_messages = []

        for message in messages or []:
            role = message.get("role")
            content = message.get("content")
            if role == "system":
                if content:
                    system_parts.append(self._normalize_text(content))
                continue

            if role == "tool":
                tool_use_id = message.get("tool_call_id") or message.get("tool_use_id")
                converted_messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": self._normalize_text(content),
                    }],
                })
                continue

            if role == "assistant":
                blocks = []
                tool_calls = message.get("tool_calls") or []
                if content:
                    blocks.append({
                        "type": "text",
                        "text": self._normalize_text(content),
                    })
                for tool_call in tool_calls:
                    function = tool_call.get("function") or {}
                    arguments = function.get("arguments") or "{}"
                    try:
                        input_payload = json.loads(arguments)
                    except Exception:
                        input_payload = {"arguments": arguments}
                    blocks.append({
                        "type": "tool_use",
                        "id": tool_call.get("id"),
                        "name": function.get("name"),
                        "input": input_payload,
                    })
                converted_messages.append({
                    "role": "assistant",
                    "content": blocks or self._normalize_text(content),
                })
                continue

            converted_messages.append({
                "role": "user",
                "content": self._normalize_text(content),
            })

        system_text = "\n\n".join(part for part in system_parts if part)
        return system_text, converted_messages

    def _request_json(self, payload):
        request = Request(
            self.api_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "User-Agent": "Odoo18 SH AI Assistance",
            },
            method="POST",
        )
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        try:
            with urlopen(request, timeout=60, context=ssl_context) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            body = ""
            try:
                body = error.read().decode("utf-8")
            except Exception:
                body = ""
            raise ValueError("Claude request failed with status %s. %s" % (error.code, body or error.reason)) from error
        except URLError as error:
            raise ValueError("Claude request failed: %s" % error.reason) from error

    def _wrap_response(self, text, tool_calls, usage, stop_reason="end_turn"):
        message = SimpleNamespace(
            content=text,
            tool_calls=tool_calls or None,
            role="assistant",
        )
        choice = SimpleNamespace(message=message, finish_reason=stop_reason or "stop")
        usage_obj = SimpleNamespace(
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
            prompt_tokens_details=None,
            completion_tokens_details=None,
            reasoning_tokens=usage.get("reasoning_tokens", 0),
        )
        return SimpleNamespace(choices=[choice], usage=usage_obj)

    def _wrap_tool_calls(self, content_blocks):
        tool_calls = []
        text_parts = []
        for block in content_blocks or []:
            block_type = self._get_value(block, "type")
            if block_type == "text":
                text_parts.append(self._normalize_text(self._get_value(block, "text", "")))
                continue
            if block_type == "tool_use":
                tool_input = self._get_value(block, "input", {})
                if not isinstance(tool_input, dict):
                    tool_input = {"input": tool_input}
                tool_calls.append(SimpleNamespace(
                    id=self._get_value(block, "id"),
                    type="function",
                    function=SimpleNamespace(
                        name=self._get_value(block, "name"),
                        arguments=json.dumps(tool_input),
                    ),
                ))
        return "".join(text_parts), tool_calls

    def _stream_response(self, payload, stop_event):
        request = Request(
            self.api_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Accept": "text/event-stream",
                "User-Agent": "Odoo18 SH AI Assistance",
            },
            method="POST",
        )
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        try:
            response = urlopen(request, timeout=60, context=ssl_context)
        except HTTPError as error:
            body = ""
            try:
                body = error.read().decode("utf-8")
            except Exception:
                body = ""
            raise ValueError("Claude stream failed with status %s. %s" % (error.code, body or error.reason)) from error
        except URLError as error:
            raise ValueError("Claude stream failed: %s" % error.reason) from error

        class MockUsage:
            def __init__(self):
                self.prompt_tokens = 0
                self.completion_tokens = 0
                self.total_tokens = 0
                self.prompt_tokens_details = None
                self.completion_tokens_details = None
                self.reasoning_tokens = 0

        class MockMessage:
            def __init__(self):
                self.content = ""
                self.tool_calls = None
                self.role = "assistant"

        class MockChoice:
            def __init__(self, message):
                self.message = message
                self.finish_reason = "stop"

        class MockResponse:
            def __init__(self):
                self.choices = [MockChoice(MockMessage())]
                self.usage = MockUsage()

        full_response = MockResponse()
        text_parts = []
        block_types = {}
        tool_inputs = {}
        tool_metadata = {}
        stop_reason = "end_turn"

        def finalize_mock():
            full_response.choices[0].message.content = "".join(text_parts)
            tool_calls = []
            for index in sorted(tool_metadata.keys()):
                meta = tool_metadata[index]
                raw_input = tool_inputs.get(index, "")
                try:
                    parsed_input = json.loads(raw_input) if raw_input else {}
                except Exception:
                    parsed_input = {"arguments": raw_input}
                tool_calls.append(SimpleNamespace(
                    id=meta.get("id"),
                    type="function",
                    function=SimpleNamespace(
                        name=meta.get("name"),
                        arguments=json.dumps(parsed_input),
                    ),
                ))
            full_response.choices[0].message.tool_calls = tool_calls or None
            full_response.choices[0].finish_reason = stop_reason or "stop"
            return full_response

        event_name = None
        data_lines = []

        try:
            for raw_line in response:
                if stop_event and stop_event.is_set():
                    response.close()
                    raise AiStoppedException(finalize_mock())

                line = raw_line.decode("utf-8").rstrip("\n")
                if not line:
                    if data_lines:
                        payload_text = "\n".join(data_lines)
                        data_lines = []
                        if payload_text == "[DONE]":
                            break
                        try:
                            event_payload = json.loads(payload_text)
                        except Exception:
                            continue

                        if event_name == "content_block_start":
                            block = event_payload.get("content_block") or {}
                            index = event_payload.get("index")
                            block_type = block.get("type")
                            if block_type:
                                block_types[index] = block_type
                            if block_type == "tool_use":
                                tool_metadata[index] = {
                                    "id": block.get("id"),
                                    "name": block.get("name"),
                                }
                                tool_inputs[index] = ""
                        elif event_name == "content_block_delta":
                            index = event_payload.get("index")
                            delta = event_payload.get("delta") or {}
                            delta_type = delta.get("type")
                            if delta_type == "text_delta":
                                text_parts.append(delta.get("text") or "")
                            elif delta_type == "input_json_delta":
                                tool_inputs[index] = tool_inputs.get(index, "") + (delta.get("partial_json") or "")
                        elif event_name == "message_delta":
                            delta = event_payload.get("delta") or {}
                            usage = event_payload.get("usage") or {}
                            stop_reason = delta.get("stop_reason") or stop_reason
                            full_response.usage.prompt_tokens = usage.get("input_tokens", full_response.usage.prompt_tokens or 0) or 0
                            full_response.usage.completion_tokens = usage.get("output_tokens", full_response.usage.completion_tokens or 0) or 0
                            full_response.usage.total_tokens = (
                                (full_response.usage.prompt_tokens or 0)
                                + (full_response.usage.completion_tokens or 0)
                            )
                        elif event_name == "message_stop":
                            break
                    event_name = None
                    continue

                if line.startswith("event:"):
                    event_name = line.split(":", 1)[1].strip()
                    continue
                if line.startswith("data:"):
                    data_lines.append(line.split(":", 1)[1].lstrip())

            return finalize_mock()
        finally:
            try:
                response.close()
            except Exception:
                pass

    def generate_content(self, model, messages, tools=None, temperature=0.2, stop_event=None, prompt_cache_key=None, reasoning_effort=None):
        system_text, converted_messages = self._convert_messages(messages)
        payload = {
            "model": model,
            "max_tokens": 2048,
            "messages": converted_messages,
        }
        if system_text:
            payload["system"] = system_text
        if tools:
            payload["tools"] = self._convert_tool_declarations(tools)
        if temperature is not None:
            payload["temperature"] = temperature

        if stop_event:
            payload["stream"] = True
            return self._stream_response(payload, stop_event)

        response_payload = self._request_json(payload)
        text, tool_calls = self._wrap_tool_calls(response_payload.get("content") or [])
        usage = response_payload.get("usage") or {}
        normalized_usage = {
            "prompt_tokens": usage.get("input_tokens", 0) or 0,
            "completion_tokens": usage.get("output_tokens", 0) or 0,
            "total_tokens": (usage.get("input_tokens", 0) or 0) + (usage.get("output_tokens", 0) or 0),
            "reasoning_tokens": 0,
        }
        return self._wrap_response(text, tool_calls, normalized_usage, stop_reason=response_payload.get("stop_reason"))
