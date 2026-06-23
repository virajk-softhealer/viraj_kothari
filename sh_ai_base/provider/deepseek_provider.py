# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies.

from openai import OpenAI

from .openai_provider import OpenAIProvider


class DeepSeekProvider(OpenAIProvider):
    """OpenAI-compatible provider configured for DeepSeek."""

    def __init__(self, api_key):
        super().__init__(api_key)
        self.client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

    def generate_content(
        self, model, messages, tools=None, temperature=0.2, stop_event=None, prompt_cache_key=None, reasoning_effort=None
    ):
        return super().generate_content(
            model=model,
            messages=messages,
            tools=tools,
            temperature=temperature,
            stop_event=stop_event,
            prompt_cache_key=prompt_cache_key,
            reasoning_effort=None,
        )
