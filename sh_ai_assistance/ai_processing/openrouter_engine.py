# -*- coding: utf-8 -*-
# Part of Softhealer Technologies.

from .openai_engine import OpenAiEngine
from ...sh_ai_base.provider.openrouter_provider import OpenRouterProvider


class OpenRouterEngine(OpenAiEngine):
    provider_type = "openrouter"
    provider_class = OpenRouterProvider
