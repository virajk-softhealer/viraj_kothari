# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies Pvt. Ltd.

from .gemini_engine import GeminiEngine
from .openai_engine import OpenAiEngine
from .openrouter_engine import OpenRouterEngine
from .sh_deepseek_engine import DeepSeekEngine
from .sh_claude_engine import ClaudeEngine

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
