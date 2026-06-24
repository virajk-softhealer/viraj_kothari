# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies Pvt. Ltd.

from .openai_engine import OpenAiEngine
from ...sh_ai_base.provider.sh_deepseek_provider import DeepSeekProvider

class DeepSeekEngine(OpenAiEngine):
    provider_type = "deepseek"
    provider_class = DeepSeekProvider
