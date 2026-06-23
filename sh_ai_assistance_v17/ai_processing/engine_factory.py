# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies.

from .claude_engine import ClaudeEngine
from .deepseek_engine import DeepSeekEngine
from .gemini_engine import GeminiEngine
from .openai_engine import OpenAiEngine
from .openrouter_engine import OpenRouterEngine

class AiEngineFactory:
    """
    Factory to create the appropriate AI Engine based on provider type.
    """
    
    @staticmethod
    def get_engine(env, provider_type):
        if provider_type == 'openai':
            return OpenAiEngine(env)
        if provider_type == 'openrouter':
            return OpenRouterEngine(env)
        if provider_type == 'deepseek':
            return DeepSeekEngine(env)
        if provider_type == 'claude':
            return ClaudeEngine(env)
        # Default to Gemini
        return GeminiEngine(env)
