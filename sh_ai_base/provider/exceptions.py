# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies.

class AiStoppedException(Exception):
    """Custom exception raised when an AI request is manually stopped by the user"""
    def __init__(self, partial_response=None):
        self.partial_response = partial_response
        super().__init__("AI request was stopped by user")
