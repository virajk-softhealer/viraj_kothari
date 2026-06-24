# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies Pvt. Ltd.

from openai import OpenAI
from .openai_provider import OpenAIProvider

class DeepSeekProvider(OpenAIProvider):
    def __init__(self, api_key):
        self.client = OpenAI(
            api_key=api_key,
            base_url="https://api.deepseek.com"
        )
