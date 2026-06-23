# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies.

from .openai_engine import OpenAiEngine
from ...sh_ai_base.provider.deepseek_provider import DeepSeekProvider


class DeepSeekEngine(OpenAiEngine):
    """Processing engine for DeepSeek models."""

    provider_type = "deepseek"
    provider_class = DeepSeekProvider
