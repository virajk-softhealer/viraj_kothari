# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies.

from google import genai
from google.genai import types
from .exceptions import AiStoppedException


def clone_gemini_part(part):
    """Return a deep copy of a Gemini Part, preserving thought signatures."""
    if hasattr(part, 'model_copy'):
        return part.model_copy(deep=True)
    return types.Part.model_validate(part)


class GeminiProvider:
    def __init__(self, api_key):
        self.client = genai.Client(api_key=api_key)

    def generate_content(self, model, contents, config=None, stop_event=None):
        """Basic Gemini API request with optional interruption support"""
        if not stop_event:
            return self.client.models.generate_content(
                model=model,
                contents=contents,
                config=config
            )

        # SIMPLE: "Stop" button support. We force a stream so we can cut it off anytime.
        # TECHNICAL: Converts a standard request into a stream to enable 
        # mid-flight interruption.
        stream = self.client.models.generate_content_stream(
            model=model,
            contents=contents,
            config=config
        )

        # SIMPLE: Our main engine expects a complete "Model Response". 
        # Here we build a fake one that uses the real Google SDK parts.
        # TECHNICAL: Using types.Candidate/types.Content ensures the partial 
        # response is valid and can be passed back into 'contents' in the next turn.
        class MockResponse:
            def __init__(self):
                self.candidates = [types.Candidate(
                    content=types.Content(parts=[], role="model"),
                    finish_reason=types.FinishReason.STOP
                )]
                self.usage_metadata = types.GenerateContentResponseUsageMetadata()
                self.text = ""

        full_response = MockResponse()
        text_parts = []
        raw_parts = []
        model_role = "model"
        finish_reason = types.FinishReason.STOP

        def finalize_mock():
            full_text = "".join(text_parts)
            full_response.text = full_text

            parts = raw_parts or ([types.Part(text=full_text)] if full_text else [])
            if full_response.candidates and full_response.candidates[0].content:
                full_response.candidates[0].content.parts = parts
                full_response.candidates[0].content.role = model_role
                full_response.candidates[0].finish_reason = finish_reason
            return full_response

        try:
            for chunk in stream:
                if stop_event.is_set():
                    # SIMPLE: THE STOP MOMENT. The user clicked cancel, so we jump out 
                    # of this loop and return what we've heard so far.
                    # TECHNICAL: Raises a custom exception to signal the engine 
                    # that work was aborted, passing the partial response data.
                    raise AiStoppedException(finalize_mock())
                
                if not chunk.candidates:
                    continue
                
                candidate = chunk.candidates[0]
                if getattr(candidate, 'finish_reason', None):
                    finish_reason = candidate.finish_reason
                if candidate.content and getattr(candidate.content, 'role', None):
                    model_role = candidate.content.role
                if candidate.content and candidate.content.parts:
                    for part in candidate.content.parts:
                        if part.text:
                            text_parts.append(part.text)
                        raw_parts.append(clone_gemini_part(part))
                
                # Accumulate usage if available in chunk (Gemini sends this in the last chunk(s))
                if chunk.usage_metadata and (chunk.usage_metadata.total_token_count or 0) > 0:
                    usage = chunk.usage_metadata
                    full_response.usage_metadata.prompt_token_count = usage.prompt_token_count or 0
                    full_response.usage_metadata.candidates_token_count = usage.candidates_token_count or 0
                    full_response.usage_metadata.total_token_count = usage.total_token_count or 0
                    # Extract cached tokens if present
                    if hasattr(usage, 'cached_content_token_count'):
                        full_response.usage_metadata.cached_content_token_count = usage.cached_content_token_count or 0
                    
                    # Extract reasoning (thoughts) tokens if present
                    if hasattr(usage, 'thoughts_token_count'):
                        full_response.usage_metadata.thoughts_token_count = usage.thoughts_token_count or 0

            return finalize_mock()

        except AiStoppedException:
            raise
        except Exception as e:
            import logging
            _logger = logging.getLogger(__name__)
            _logger.error(f"❌ Gemini API Error: {str(e)}")
            raise
