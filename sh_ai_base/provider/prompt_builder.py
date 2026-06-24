# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies.

"""
Dynamic Prompt Builder for AI Assistant

Assembles the complete system prompt using modular instruction blocks
stored in the LLM configuration. Follows Claude's prompt engineering
best practices:
- Long-form data (Model Catalog) at the top
- XML tags for clear section separation
- User query context at the bottom
"""

import hashlib
import json

SHARED_EXECUTION_POLICY = """
<shared_execution_policy>
These execution rules override any earlier instruction that would cause unnecessary discovery, unnecessary clarification, or extra tool calls.

1. EFFICIENCY FIRST: Use the smallest reliable tool sequence that can answer the user's request accurately.
2. CONDITIONAL DISCOVERY: Do not call `get_model_fields` by default. Use it only when the model, field names, relationships, or filter structure are uncertain.
3. DIRECT EXECUTION: If the request is simple and the required tool path is already clear, execute directly instead of adding exploratory calls.
4. CLARIFY ONLY WHEN BLOCKED: Ask a follow-up question only when ambiguity would materially change the result and cannot be resolved safely from the catalog, tool results, or conversation history.
5. USE THE BEST DEFAULT: If one interpretation is the normal Odoo meaning and it satisfies the request, proceed with it and state the assumption briefly when helpful.
6. STOP WHEN THE ANSWER IS CLEAR: Once a tool result answers the question, synthesize the response. Do not keep exploring for optional details unless the user asked for them.
7. ZERO RESULTS ARE VALID: If a clear query returns zero records, confirm that result after one reasonable verification step and answer directly. Do not ask for clarification only because nothing matched.
8. REUSE KNOWN CONTEXT: Prefer recent tool results, technical memory, and prior schema knowledge over repeating the same discovery calls.
9. STAY ON THE REQUESTED ENTITY: Use the business entity the user asked for unless clarification or a fallback is explicitly needed. Do not automatically broaden from one model family to another just because the first search returned no results.
10. AVOID REDUNDANT VERIFICATION: Prefer one primary retrieval path and at most one verification step. Do not chain multiple count/search variants that answer the same question unless the prior result was malformed or inconclusive.
</shared_execution_policy>
"""


class PromptBuilder:
    """
    Builds dynamic prompts using instructions stored in database LLM records.
    Optimized for Claude/Gemini with XML structure and long-context best practices.
    """

    def __init__(self, env):
        self.env = env

    def build_user_context(self):
        """
        Build dynamic user and company context.
        Placed near the end of prompt (after instructions, before examples).
        """
        user = self.env.user
        company = user.company_id
        user_tz = user.tz or company.partner_id.tz or 'UTC'

        return f"""
<current_context>
<user>
- Name: {user.name}
- Timezone: {user_tz}
- Language: {user.lang or 'en_US'}
</user>
<company>
- Name: {company.name}
- Currency: {company.currency_id.symbol} ({company.currency_id.name})
- Country: {company.country_id.name if company.country_id else 'Not set'}
</company>
</current_context>
"""

    def build_tools_section(self, tool_declarations):
        """
        Build tool documentation from declarations.
        Uses concise format optimized for LLM understanding.
        """
        if not tool_declarations:
            return ""

        section = "<available_tools>\n"
        for tool in tool_declarations:
            section += f"\n## {tool['name']}\n"
            section += f"Purpose: {tool['description']}\n"
            if 'parameters' in tool and 'properties' in tool['parameters']:
                section += "Parameters:\n"
                for p_name, p_info in tool['parameters']['properties'].items():
                    req = "(required)" if p_name in tool['parameters'].get('required', []) else "(optional)"
                    section += f"  - {p_name} {req}: {p_info.get('description', '')}\n"
        section += "\n</available_tools>"
        return section

    def build_model_catalog(self, model_catalog, is_followup=False, previous_messages=None):
        """
        Build the model catalog section.
        CRITICAL: Placed at the TOP of prompt for best long-context performance.
        
        Args:
            is_followup: If True, only sends a 'Smart Subset' of models to save tokens.
        """
        if not model_catalog:
            return "<model_catalog>\nNo models available.\n</model_catalog>"

        # Determine if we should prune the catalog
        if is_followup and previous_messages:
            # 1. Identify models already used in this session
            used_model_names = set()
            for msg in previous_messages:
                debug = msg.debug_info or {}
                for tc in debug.get('tool_calls', []):
                    model = tc.get('args', {}).get('model')
                    if model:
                        used_model_names.add(model)
            
            # 2. Filter catalog based ONLY on dynamically detected models from history
            refined_catalog = [m for m in model_catalog if m.get('model') in used_model_names]
            
            # 3. If no models were used yet, we keep the pruning inactive or use history metadata
            if not refined_catalog and used_model_names:
                # This handles cases where models were found but aren't in the provided catalog list
                pass

            model_catalog = refined_catalog
            pruned_active = True
        else:
            pruned_active = False

        section = "<model_catalog>\n"
        if pruned_active:
            section += "NOTE: This is a REFINED list of models related to your current conversation context to save tokens.\n"
            section += "If you need a model NOT in this list, call get_models_list() to see everything.\n\n"
        else:
            section += "CRITICAL: This is your complete reference of available Odoo models.\n"
            section += "Use ONLY models from this list. If a model is missing, the user lacks access.\n\n"

        for model in model_catalog:
            display = model.get('display_name', '')
            name = model.get('model', '')
            section += f"- {display} ({name})\n"

        section += "\n</model_catalog>"
        return section

    def build_technical_memory(self, previous_messages):
        """
        Extract technical IDs and resolved names from history to give
        the AI 'Technical Memory' for faster follow-up queries.
        """
        if not previous_messages:
            return ""

        resolutions = []
        for msg in previous_messages:
            debug = msg.debug_info or {}
            for tc in debug.get('tool_calls', []):
                # Extract resolutions from fuzzy_lookup or successful search returns
                if tc.get('tool') == 'fuzzy_lookup' and tc.get('result', {}).get('success'):
                    term = tc.get('args', {}).get('search_term')
                    model = tc.get('args', {}).get('model')
                    # We store full results in result now (merged with raw_result logic)
                    raw = tc.get('result') or {}
                    results = raw.get('results', [])
                    for r in results:
                        if isinstance(r, dict) and 'id' in r and 'display_name' in r:
                            resolutions.append(f"- '{term}' resolved to ID {r['id']} ({r['display_name']}) in model '{model}'")
                
                # Also capture direct search findings if they look like specific record hits
                elif tc.get('tool') == 'search_records' and tc.get('result', {}).get('success'):
                    domain = tc.get('args', {}).get('domain', [])
                    if domain:
                        for cond in domain:
                            if isinstance(cond, (list, tuple)) and cond[0] == 'name' and cond[1] == '=':
                                raw = tc.get('result') or {}
                                records = raw.get('records', [])
                                for rec in records:
                                    if isinstance(rec, dict) and 'id' in rec:
                                        resolutions.append(f"- Explicit match: '{cond[2]}' is ID {rec['id']} in model '{tc['args']['model']}'")

        if not resolutions:
            return ""

        unique_resolutions = "\n".join(sorted(list(set(resolutions))))
        return f"""
<recent_technical_memory>
In this session, the following records were already identified. 
USE THESE IDs DIRECTLY for follow-up questions to avoid redundant lookups and ensure accuracy:
{unique_resolutions}
</recent_technical_memory>
"""

    def build_complete_prompt(self, system_prompt, tool_declarations, model_catalog=None, instructions=None, is_followup=False, extra_stable_sections=None):
        prompt_package = self.build_prompt_package(
            system_prompt=system_prompt,
            tool_declarations=tool_declarations,
            model_catalog=model_catalog,
            instructions=instructions,
            is_followup=is_followup,
            extra_stable_sections=extra_stable_sections,
        )
        return prompt_package['full_prompt']

    def _hash_text(self, text):
        return hashlib.sha256((text or '').encode('utf-8')).hexdigest()

    def _build_cache_metadata(self, stable_sections, dynamic_sections, tool_declarations, is_followup):
        stable_prefix = "\n\n".join(section['content'] for section in stable_sections if section.get('content')).strip()
        dynamic_suffix = "\n\n".join(section['content'] for section in dynamic_sections if section.get('content')).strip()
        full_prompt = "\n\n".join(
            [part for part in [stable_prefix, dynamic_suffix] if part]
        ).strip()
        tool_signature = self._hash_text(json.dumps(tool_declarations or [], sort_keys=True, default=str))

        return {
            'stable_prefix_hash': self._hash_text(stable_prefix),
            'dynamic_suffix_hash': self._hash_text(dynamic_suffix),
            'full_prompt_hash': self._hash_text(full_prompt),
            'tool_signature_hash': tool_signature,
            'stable_section_names': [section['name'] for section in stable_sections],
            'dynamic_section_names': [section['name'] for section in dynamic_sections],
            'stable_prefix_chars': len(stable_prefix),
            'dynamic_suffix_chars': len(dynamic_suffix),
            'full_prompt_chars': len(full_prompt),
            'is_followup': bool(is_followup),
            'cache_strategy': {
                'openai': 'prompt_cache_key',
                'gemini': 'implicit_prefix_caching',
            },
        }

    def build_prompt_package(self, system_prompt, tool_declarations, model_catalog=None, instructions=None, is_followup=False, extra_stable_sections=None):
        """
        Assemble the complete prompt following best practices:
        1. System prompt (Identity) first for stable caching
        2. Model catalog (Stable long-form data)
        3. Tools documentation (Stable)
        4. Critical rules (High priority)
        5. User context (Dynamic)
        6. Examples last (near the query)
        """
        instructions = instructions or {}
        stable_sections = []
        dynamic_sections = []

        # LAYER 1: IDENTITY & CORE PRINCIPLES (Stable prefix)
        if system_prompt:
            stable_sections.append({'name': 'system_prompt', 'content': system_prompt.strip()})

        # LAYER 2: MODEL CATALOG (Pruned on follow-up)
        if model_catalog:
            stable_sections.append({'name': 'model_catalog', 'content': self.build_model_catalog(
                model_catalog, 
                is_followup=is_followup, 
                previous_messages=instructions.get('previous_messages')
            ).strip()})

        # LAYER 3: TOOLS DOCUMENTATION (Stable)
        tools_doc = self.build_tools_section(tool_declarations)
        if tools_doc:
            stable_sections.append({'name': 'tools_documentation', 'content': tools_doc.strip()})

        # LAYER 4: CRITICAL GUARDRAILS 
        if instructions.get('critical'):
            stable_sections.append({'name': 'critical_rules', 'content': instructions['critical'].strip()})

        # LAYER 5: WORKFLOW, TOOLING, AND EXECUTION POLICY
        if instructions.get('workflow'):
            stable_sections.append({'name': 'workflow_instruction', 'content': instructions['workflow'].strip()})
        
        if instructions.get('tool'):
            stable_sections.append({'name': 'tool_instruction', 'content': instructions['tool'].strip()})

        stable_sections.append({'name': 'shared_execution_policy', 'content': SHARED_EXECUTION_POLICY.strip()})
        for section in extra_stable_sections or []:
            if not isinstance(section, dict):
                continue
            name = section.get('name')
            content = (section.get('content') or '').strip()
            if name and content:
                stable_sections.append({'name': name, 'content': content})

        # LAYER 6: OUTPUT FORMATTING, SECURITY, AND CONTEXT
        if instructions.get('formatting'):
            stable_sections.append({'name': 'formatting_instruction', 'content': instructions['formatting'].strip()})

        if instructions.get('security'):
            stable_sections.append({'name': 'security_instruction', 'content': instructions['security'].strip()})

        if instructions.get('context'):
            stable_sections.append({'name': 'context_instruction', 'content': instructions['context'].strip()})

        # LAYER 7: USER/COMPANY CONTEXT (Dynamic)
        dynamic_sections.append({'name': 'user_context', 'content': self.build_user_context().strip()})

        # LAYER 8: TECHNICAL MEMORY (Follow-up specific)
        if is_followup and 'previous_messages' in instructions:
            tech_memory = self.build_technical_memory(instructions['previous_messages'])
            if tech_memory:
                dynamic_sections.append({'name': 'technical_memory', 'content': tech_memory.strip()})

        # LAYER 9: EXAMPLES (Only on first query to save tokens)
        if instructions.get('examples') and not is_followup:
            dynamic_sections.append({'name': 'examples', 'content': instructions['examples'].strip()})

        stable_prefix = "\n\n".join(section['content'] for section in stable_sections if section.get('content')).strip()
        dynamic_suffix = "\n\n".join(section['content'] for section in dynamic_sections if section.get('content')).strip()
        full_prompt = "\n\n".join(part for part in [stable_prefix, dynamic_suffix] if part).strip()
        cache_metadata = self._build_cache_metadata(
            stable_sections,
            dynamic_sections,
            tool_declarations,
            is_followup,
        )

        return {
            'full_prompt': full_prompt,
            'stable_prefix': stable_prefix,
            'dynamic_suffix': dynamic_suffix,
            'cache_metadata': cache_metadata,
        }
