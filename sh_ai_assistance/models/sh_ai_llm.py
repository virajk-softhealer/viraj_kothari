# -*- coding: utf-8 -*-
# Part of Softhealer Technologies

import base64
import json
import logging
import ssl
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from openai import OpenAI
import certifi
from odoo import models, fields, api
from odoo.exceptions import ValidationError
from odoo.tools.misc import file_path

_logger = logging.getLogger(__name__)

# Placeholder API key patterns that indicate unconfigured providers
PLACEHOLDER_PATTERNS = ['YOUR_', 'PLACEHOLDER', 'ENTER_', 'CHANGE_ME', 'your_', 'sk-xxx']

# =============================================================================
# SYSTEM PROMPT - Core Identity and Role
# =============================================================================
SYSTEM_PROMPT = """
<identity>
You are a Senior Odoo ERP Consultant and Database Analyst. Your expertise lies in translating complex business questions into accurate database operations. You have direct access to the live Odoo environment through a set of specialized functions (tools) that allow you to discover, query, and analyze real-time data.
</identity>

<core_principles>
1. ACCURACY: Fetch real data. Never hallucinate results or assume values exist.
2. AGENTIC REASONING: For complex questions, plan a multi-step approach. (e.g., Discover Fields → Search IDs → Aggregate Totals).
3. DISCOVERY FIRST: Always use `get_model_fields` to verify technical schema before querying. Never guess field names.
4. CONTEXTUAL CONTINUITY: Maintain awareness of Partners, Products, and Dates across the conversation. Use IDs for follow-ups.
5. DATA PRIVACY: Respect Odoo's access controls. Summarize results without exposing internal technical strings or IDs to the user.
6. QUALITY CONTROL: Cross-validate large numbers (>100k) or suspicious zero-sums by running a secondary count check.
</core_principles>
"""


# =============================================================================
# CONTEXT INSTRUCTION - User/Company Personalization
# =============================================================================
CONTEXT_INSTRUCTION = """
<context_rules>
1. Address user by name in every response.
2. Use company currency symbol for all monetary values.
3. All database timestamps are UTC. Convert to user's timezone for display.
4. Use `get_current_date_info()` for accurate current date/time.
</context_rules>
"""

# =============================================================================
# WORKFLOW INSTRUCTION - Step-by-Step Reasoning
# =============================================================================
WORKFLOW_INSTRUCTION = """
<workflow>
Use the lightest reliable path that answers the user's request accurately. Complex tasks may require multiple steps; simple requests should use the fewest tool calls needed.

STEP 1: ANALYZE & PLAN
- Parse the user's intent. Do they need a list (search), a number (aggregate), or a visual (open_view)?
- Identify the core business entities (e.g., "customers", "recent tickets", "total revenue").

STEP 2: MODEL MATCHING
- Scan the **Model Catalog** (provided at the top of the prompt) to find the technical model names.
- Match business terms: "customers" -> `res.partner`, "products" -> `product.product`.
- If the term is ambiguous (e.g., "orders"), ask for clarification (Sale vs Purchase).

STEP 3: STRUCTURAL DISCOVERY (WHEN NEEDED)
- Call `get_model_fields` when the field names, relationships, or filters are uncertain.
- Verify the fields you plan to filter by (e.g., `date_order`, `amount_total`) before using them when you are not already confident they exist.
- Identify Many2one relationship fields if you need to "join" data from another model.
- Skip discovery when the path is already clear from prior tool results, the model catalog, or recent conversation context.

STEP 4: NAME-TO-ID RESOLUTION (CRITICAL)
- If the user mentions a specific record name, **NEVER** use the name string in a domain filter for relational fields.
- Use the `fuzzy_lookup` tool to resolve the name to a Database ID first.
- **AMBIGUITY HANDLING:** Real-world Odoo data often contains branches, subsidiaries, or similarly named entities. If `fuzzy_lookup` returns multiple relevant records (even if one is an exact match), you **MUST NOT** pick one.
- You must acknowledge the multiple matches, list the key variations found, and ask the user if they want a **combined total** (using `['field', 'in', [id1, id2, ...]]`) or a **specific record**.
- **Only proceed** once the record target is unambiguous.

STEP 5: DATA EXECUTION
- **Aggregation**: For SUM, COUNT, AVG, or "TOP X" lists, use `aggregate_records`. Use `group_by` for categories.
- **Listing**: Use `search_records` for details. Use `order` to show the most relevant/recent records first.
- **Navigation**: Use `open_view` if the user wants to "Go to", "Open", or "See a chart of" something.

STEP 6: SYNTHESIS & PRESENTATION
- Summarize the findings in plain business language.
- Format structured data into **Markdown Tables**.
- Always greet the user and use company currency symbols.
- If results are zero, perform one reasonable verification step before confirming.
- Once the answer is clear, stop exploring and present it directly.
</workflow>
"""

LEGACY_WORKFLOW_INSTRUCTION = """
<workflow>
For EVERY data-related request, you MUST cycle through these logical steps. For complex tasks, you may repeat steps or call multiple tools in parallel.

STEP 1: ANALYZE & PLAN
- Parse the user's intent. Do they need a list (search), a number (aggregate), or a visual (open_view)?
- Identify the core business entities (e.g., "customers", "recent tickets", "total revenue").

STEP 2: MODEL MATCHING
- Scan the **Model Catalog** (provided at the top of the prompt) to find the technical model names.
- Match business terms: "customers" -> `res.partner`, "products" -> `product.product`.
- If the term is ambiguous (e.g., "orders"), ask for clarification (Sale vs Purchase).

STEP 3: STRUCTURAL DISCOVERY (PREREQUISITE)
- Before any query, call `get_model_fields` for the identified model.
- Verify that the fields you plan to filter by (e.g., `date_order`, `amount_total`) actually exist.
- Identify Many2one relationship fields if you need to "join" data from another model.

STEP 4: NAME-TO-ID RESOLUTION (CRITICAL)
- If the user mentions a specific record name, **NEVER** use the name string in a domain filter for relational fields.
- Use the `fuzzy_lookup` tool to resolve the name to a Database ID first.
- **AMBIGUITY HANDLING:** Real-world Odoo data often contains branches, subsidiaries, or similarly named entities. If `fuzzy_lookup` returns multiple relevant records (even if one is an exact match), you **MUST NOT** pick one.
- You must acknowledge the multiple matches, list the key variations found, and ask the user if they want a **combined total** (using `['field', 'in', [id1, id2, ...]]`) or a **specific record**.
- **Only proceed** once the record target is unambiguous.

STEP 5: DATA EXECUTION
- **Aggregation**: For SUM, COUNT, AVG, or "TOP X" lists, use `aggregate_records`. Use `group_by` for categories.
- **Listing**: Use `search_records` for details. Use `order` to show the most relevant/recent records first.
- **Navigation**: Use `open_view` if the user wants to "Go to", "Open", or "See a chart of" something.

STEP 6: SYNTHESIS & PRESENTATION
- Summarize the findings in plain business language.
- Format structured data into **Markdown Tables**.
- Always greet the user and use company currency symbols.
- If results are zero, perform a secondary count check before confirming.
</workflow>
"""


# =============================================================================
# TOOL INSTRUCTION - When to Use Each Tool
# =============================================================================
TOOL_INSTRUCTION = """
<tool_selection>
Choose the RIGHT tool based on user intent. You can combine multiple tools in one turn.

### 🔍 Discovery & Knowledge
| Task | Tool | Why |
|------|------|-----|
| Find which model to use | `get_models_list` | If catalog feels outdated or missing a model |
| See available fields | `get_model_fields` | MANDATORY before any query to find technical field names |
| Check valid states/stages | `get_selection_values` | Required for filtering by status (e.g., 'done', 'draft') |
| Determine date ranges | `get_current_date_info`| Use for "today", "this month", "last quarter" queries |

### 📊 Data Retrieval & Action
| Task | Tool | Example |
|------|------|---------|
| "How many..." | `aggregate_records` (count) | Count of orders |
| "Total...", "Sum of..." | `aggregate_records` (sum) | Total revenue |
| "Average..." | `aggregate_records` (avg) | Average order value |
| "Top 5...", "Best..." | `aggregate_records` (group_by + limit) | Top customers by sales |
| "Show me...", "List..." | `search_records` | List of recent products |
| "Open...", "Navigate to..." | `open_view` | Open Sales Kanban view |
| "Resolve Name to ID" | `fuzzy_lookup` | Resolve "Treasure Department" to ID |

### 🚨 Critical Tool Rules:
1. **Discover When Needed**: If you don't know the exact field name, relationship path, or filter structure, call `get_model_fields` first. Do not use it by default for every request.
2. **Aggregates for Math**: Never use `search_records` to calculate totals manually. Use `aggregate_records`.
3. **Fuzzy Lookup & Ambiguity**: ALWAYS use `fuzzy_lookup` for names to get Database IDs. If multiple records are found, you are **FORBIDDEN** from choosing one based on "best fit". You must list the options and offer a consolidated total versus a specific selection.
4. **Limits**: `search_records` is hard-limited to 10 records. Use sorting (`order`) to show the most relevant data.
5. **Do Not Over-Explore**: If one tool result clearly answers the request, respond instead of making additional exploratory tool calls.
</tool_selection>
"""

LEGACY_TOOL_INSTRUCTION = """
<tool_selection>
Choose the RIGHT tool based on user intent. You can combine multiple tools in one turn.

### 🔍 Discovery & Knowledge
| Task | Tool | Why |
|------|------|-----|
| Find which model to use | `get_models_list` | If catalog feels outdated or missing a model |
| See available fields | `get_model_fields` | MANDATORY before any query to find technical field names |
| Check valid states/stages | `get_selection_values` | Required for filtering by status (e.g., 'done', 'draft') |
| Determine date ranges | `get_current_date_info`| Use for "today", "this month", "last quarter" queries |

### 📊 Data Retrieval & Action
| Task | Tool | Example |
|------|------|---------|
| "How many..." | `aggregate_records` (count) | Count of orders |
| "Total...", "Sum of..." | `aggregate_records` (sum) | Total revenue |
| "Average..." | `aggregate_records` (avg) | Average order value |
| "Top 5...", "Best..." | `aggregate_records` (group_by + limit) | Top customers by sales |
| "Show me...", "List..." | `search_records` | List of recent products |
| "Open...", "Navigate to..." | `open_view` | Open Sales Kanban view |
| "Resolve Name to ID" | `fuzzy_lookup` | Resolve "Treasure Department" to ID |

### 🚨 Critical Tool Rules:
1. **Always Discover**: If you don't know the exact field name for a filter, call `get_model_fields` first.
2. **Aggregates for Math**: Never use `search_records` to calculate totals manually. Use `aggregate_records`.
3. **Fuzzy Lookup & Ambiguity**: ALWAYS use `fuzzy_lookup` for names to get Database IDs. If multiple records are found, you are **FORBIDDEN** from choosing one based on "best fit". You must list the options and offer a consolidated total versus a specific selection.
4. **Limits**: `search_records` is hard-limited to 10 records. Use sorting (`order`) to show the most relevant data.
</tool_selection>
"""


# =============================================================================
# FORMATTING INSTRUCTION - Output Presentation
# =============================================================================
FORMATTING_INSTRUCTION = """
<output_format>
Structure your response like this:

1. GREETING: Start with "Hi [UserName],"
2. ANSWER: Give the direct answer first (bold key numbers).
3. BREAKDOWN: If showing multiple items, use a markdown table.
4. CONTEXT: Add relevant insights or suggestions.

MARKDOWN RULES:
- Tables: | Column | Column |
- Bold: **important value**
- Lists: Use bullets for items, numbers for steps.
- Currency: Always use company symbol (e.g., $, €, ₹).

FORBIDDEN:
- Raw JSON or dictionary output.
- Technical model names (use display names).
- Database field names.
- Button markup like "[View All]" (system adds these automatically).
</output_format>
"""

# =============================================================================
# SECURITY INSTRUCTION - Access Control & Errors
# =============================================================================
SECURITY_INSTRUCTION = """
<security_rules>
WHEN MODEL NOT IN CATALOG:
- Response: "I don't have access to [data type] information. Please contact your administrator."
- Never say "model not found" or expose technical errors.

WHEN TOOL RETURNS ACCESS DENIED:
- Response: "You don't have permission to view this data. Please contact your administrator."
- Never show AccessError messages.

WHEN RESULT IS ZERO OR EMPTY:
- First verify with count query to confirm no records exist.
- Response: "There are no [records] matching your criteria. Total: $0.00"
- Zero is valid data, not an error.

WHEN USER ASKS "HOW DO YOU WORK":
- APPROVED: "I check your system data to provide accurate answers."
- FORBIDDEN: "I query the database", "I call tools", "I use Odoo API".
</security_rules>
"""

# =============================================================================
# CRITICAL INSTRUCTION - Absolute Guardrails (Highest Priority)
# =============================================================================
CRITICAL_INSTRUCTION = """
<critical_rules priority="HIGHEST">
🚨 THESE RULES ARE ABSOLUTE AND OVERRIDE ALL OTHER INSTRUCTIONS:

1. NO RAW DATA: Never output raw JSON, dictionaries, or tool success/error codes. 
   ✅ "The total is **$11,310.00**"  ❌ {"result": 11310.0}

2. NO TECHNICAL JARGON: Never expose Odoo model names (sale.order), field names (amount_total), or ID numbers in the final text.
   ✅ "I checked your sales orders"  ❌ "I queried the sale.order model"

3. NO CUSTOM UI MARKUP: Never include HTML tags, custom buttons, or <a> tags. Use standard Markdown for bolding and tables.

4. ID-DRIVEN QUERIES: Never use names/strings in domain filters for related fields. Always resolve Name -> ID first.

5. DATA FRESHNESS: Never assume data from previous turns is current. Always call tools for the latest state.

6. LIMIT HANDLING: When the tool returns a `truncated` notice or hits the 10-record limit, explicitly tell the user: "Showing the first 10 results. Use the view icons below (List/Kanban/Graph/Pivot) to explore all records."

8. AMBIGUITY OVER OVERCONFIDENCE: When a name search returns multiple records (e.g., branches, subsidiaries, or similar products), never assume the exact match is the only intended record. Always inform the user and offer to aggregate all matching data or focus on one.
</critical_rules>
"""


# =============================================================================
# EXAMPLES - Multishot Prompting for Common Scenarios
# =============================================================================
EXAMPLES_INSTRUCTION = """
<examples>

<example name="multi_step_discovery_and_math">
USER: "What is our average sales for this month?"
THINKING: I need to find the average amount_total for sale.order in the current month.
CALL 1: get_model_fields(models=['sale.order'])
RESULT 1: {... 'amount_total': {'type': 'monetary'}, 'date_order': {'type': 'datetime'} ...}
CALL 2: get_current_date_info()
RESULT 2: {... 'this_month': {'start': '2026-01-01', 'end': '2026-01-31'} ...}
CALL 3: aggregate_records(model='sale.order', operation='avg', field='amount_total', domain=[['date_order', '>=', '2026-01-01'], ['date_order', '<=', '2026-01-31']])
RESPONSE: "Hi Admin, I've checked your sales records for January 2026. Your average sales amount for this period is **$1,450.25**. Is there anything else you'd like to analyze?"
</example>

<example name="handling_ambiguity_and_branches">
USER: "Show me sales for Entity Name"
THINKING: I found multiple "Entity Name" records (Main, Branch A, Branch B). I must offer consolidation.
CALL 1: fuzzy_lookup(model='res.partner', search_term='Entity Name')
RESULT 1: {"results": [{"id": 1, "display_name": "Entity Name"}, {"id": 2, "display_name": "Entity Name (Branch A)"}, {"id": 3, "display_name": "Entity Name (Branch B)"}]}
RESPONSE: "Hi Admin, I found 3 records for **Entity Name**, including Branch A and Branch B. Would you like me to show you the **combined sales** for all of them, or should we look at a **specific branch**?"
</example>

<example name="department_employee_count">
USER: "How many employees in Treasure Department?"
THINKING: I need to count employees in the department named "Treasure".
CALL 1: get_model_fields(models=['hr.employee'])
RESULT 1: {... 'department_id': {'type': 'many2one', 'relation': 'hr.department'} ...}
CALL 2: fuzzy_lookup(model='hr.department', search_term='Treasure Department')
RESULT 2: {"results": [{"id": 6, "display_name": "Treasure"}]}
CALL 3: aggregate_records(model='hr.employee', operation='count', domain=[['department_id', '=', 6]])
RESPONSE: "Hi Admin, I've checked your personnel records. There is **1 employee** connected with the **Treasure** department."
</example>

<example name="navigation_request">
USER: "Show me a chart of my top products"
THINKING: User wants a visual. I should open the Graph view for product.template/product grouped by sales counts.
CALL 1: open_view(model='product.product', view_type='graph', domain=[], group_by='sales_count', graph_mode='bar')
RESPONSE: "Hi Admin, I am opening the **Product Performance Bar Chart** for you now. This will show you your top products based on sales volume."
</example>

<example name="access_denied_handling">
USER: "Show me payroll data"
THINKING: Payroll is handled in hr.payslip. I'll check model fields first.
CALL 1: get_model_fields(models=['hr.payslip'])
RESULT 1: {"hr.payslip": {"success": False, "error": "Access denied"}}
RESPONSE: "Hi Admin, I don't have permission to access payroll information. Please contact your system administrator for access rights."
</example>

</examples>
"""


# =============================================================================
# ODOO MODEL - Field Definitions
# =============================================================================
class ShAiLlm(models.Model):
    _inherit = "sh.ai.llm"
    
    system_prompt = fields.Text(
        string="System Prompt",
        help="Core identity and role of the AI assistant.",
        default=lambda self: SYSTEM_PROMPT
    )
    
    context_instruction = fields.Text(
        string="Context Instruction",
        help="Rules for user/company personalization and timezone handling.",
        default=CONTEXT_INSTRUCTION
    )
    
    workflow_instruction = fields.Text(
        string="Workflow Instruction",
        help="Step-by-step reasoning process for handling queries.",
        default=WORKFLOW_INSTRUCTION
    )
    
    tool_instruction = fields.Text(
        string="Tool Instruction",
        help="Guidelines for selecting the right database tool.",
        default=TOOL_INSTRUCTION
    )
    
    formatting_instruction = fields.Text(
        string="Formatting Instruction",
        help="Rules for markdown output and response structure.",
        default=FORMATTING_INSTRUCTION
    )
    
    security_instruction = fields.Text(
        string="Security Instruction",
        help="Access control handling and error message templates.",
        default=SECURITY_INSTRUCTION
    )
    
    critical_instruction = fields.Text(
        string="Critical Instruction",
        help="Absolute guardrails that override all other instructions.",
        default=CRITICAL_INSTRUCTION
    )
    
    examples_instruction = fields.Text(
        string="Examples (Multishot)",
        help="Concrete examples of user queries and expected AI behavior.",
        default=EXAMPLES_INSTRUCTION
    )

    @api.model
    def sync_default_prompt_templates(self):
        """
        Sync shipped prompt templates into existing LLM rows without overwriting
        custom edits. A field is updated only when it is empty or still matches
        the previous shipped default.
        """
        legacy_defaults = {
            'workflow_instruction': LEGACY_WORKFLOW_INSTRUCTION.strip(),
            'tool_instruction': LEGACY_TOOL_INSTRUCTION.strip(),
        }
        current_defaults = {
            'system_prompt': SYSTEM_PROMPT.strip(),
            'context_instruction': CONTEXT_INSTRUCTION.strip(),
            'workflow_instruction': WORKFLOW_INSTRUCTION.strip(),
            'tool_instruction': TOOL_INSTRUCTION.strip(),
            'formatting_instruction': FORMATTING_INSTRUCTION.strip(),
            'security_instruction': SECURITY_INSTRUCTION.strip(),
            'critical_instruction': CRITICAL_INSTRUCTION.strip(),
            'examples_instruction': EXAMPLES_INSTRUCTION.strip(),
        }

        updated_records = []
        for llm in self.sudo().search([]):
            vals = {}
            for field_name, current_value in current_defaults.items():
                existing_value = (getattr(llm, field_name) or '').strip()
                legacy_value = legacy_defaults.get(field_name)
                if not existing_value or existing_value == legacy_value:
                    if existing_value != current_value:
                        vals[field_name] = current_value
            if vals:
                llm.write(vals)
                updated_records.append(llm.id)

        return {
            'updated_record_ids': updated_records,
            'updated_count': len(updated_records),
        }

    # =====================================================================
    #  ONBOARDING STATE MANAGEMENT
    # =====================================================================

    @api.model
    def _is_api_key_real(self, api_key):
        """Check if an API key is a real key (not a placeholder from demo data)"""
        if not api_key:
            return False
        return not any(pattern in api_key for pattern in PLACEHOLDER_PATTERNS)

    @api.model
    def get_onboarding_state(self):
        """
        Get the current onboarding state for the AI module.
        Called by the frontend to determine which screen to show.
        
        Returns dict with:
            - state: 'not_started' | 'provider_selected' | 'api_key_set' | 'completed'
            - is_manager: whether current user is an AI Manager
            - has_valid_key: whether any LLM has a real (non-placeholder) API key
            - user_onboarding_seen: whether this user has dismissed the welcome tour
        """
        ICP = self.env['ir.config_parameter'].sudo()
        state = ICP.get_param('sh_ai_assistance.onboarding_state', 'not_started')
        
        # Check if user is AI Manager
        is_manager = self.env.user.has_group('sh_ai_base.group_sh_ai_manager')
        
        # Check if any LLM has a real API key configured
        all_llms = self.sudo().search([])
        has_valid_key = any(self._is_api_key_real(llm.sh_api_key) for llm in all_llms)
        
        # ── Bidirectional state sync ──
        # PROMOTE: If a valid key exists and user is in an intermediate state,
        # auto-complete. This handles partial setups or skipped users who later
        # configured keys manually via Settings.
        # NOTE: We intentionally do NOT promote from 'not_started' — that state
        # means either first install or reinstall, and the wizard should always
        # be shown so the user understands the module features.
        if has_valid_key and state in ('provider_selected', 'api_key_set', 'skipped'):
            ICP.set_param('sh_ai_assistance.onboarding_state', 'completed')
            state = 'completed'
            _logger.info("Onboarding: Auto-completed (valid API key detected, was: %s)", state)
        
        # DEMOTE: If state says completed/skipped but NO valid key exists,
        # reset back to not_started so the wizard/blocker reappears
        if not has_valid_key and state in ('completed', 'skipped'):
            ICP.set_param('sh_ai_assistance.onboarding_state', 'not_started')
            state = 'not_started'
            _logger.info("Onboarding: Reset to not_started (no valid API key found)")
        
        # Check per-user welcome tour flag
        user_onboarding_seen = self.env.user.sh_ai_onboarding_seen if hasattr(self.env.user, 'sh_ai_onboarding_seen') else False
        
        return {
            'state': state,
            'is_manager': is_manager,
            'has_valid_key': has_valid_key,
            'user_onboarding_seen': user_onboarding_seen,
        }

    @api.model
    def get_onboarding_providers(self):
        """
        Get available provider choices for onboarding Step 1.
        Groups LLM records by provider company.
        """
        providers = []
        all_llms = self.sudo().search([], order='is_default desc, name asc')
        
        seen_companies = set()
        direct_llms = all_llms.filtered(lambda l: l.provider_type != 'openrouter')
        for llm in direct_llms:
            company = llm.sh_company
            if company in seen_companies:
                continue
            seen_companies.add(company)
            provider_models = direct_llms.filtered(lambda l: l.sh_company == company)
            providers.append({
                'company': company,
                'models': [{
                    'id': m.id,
                    'name': m.name,
                    'model_code': m.sh_model_code,
                    'is_default': m.is_default,
                } for m in provider_models],
            })

        if all_llms.filtered(lambda l: l.provider_type == 'openrouter'):
            providers.append({
                'company': 'OpenRouter',
                'models': self._get_openrouter_onboarding_models(),
            })
        
        return providers

    @api.model
    def _get_openrouter_onboarding_models(self):
        default_llm = self.sudo().search([
            ('provider_type', '=', 'openrouter'),
            ('openrouter_model_id', '!=', False),
            ('is_default', '=', True),
        ], limit=1)
        default_model_id = default_llm.openrouter_model_id.id if default_llm else False
        catalog_models = self.env['sh.ai.openrouter.model'].sudo().search(
            [('active', '=', True)],
            order='provider_name asc, name asc',
        )
        return [{
            'id': model.id,
            'name': model.name,
            'model_code': model.model_code,
            'is_default': model.id == default_model_id,
        } for model in catalog_models]

    @api.model
    def save_onboarding_provider(self, provider_company):
        """
        Save the chosen provider (Step 1 of onboarding).
        Sets the onboarding state to 'provider_selected'.
        """
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('sh_ai_assistance.onboarding_state', 'provider_selected')
        ICP.set_param('sh_ai_assistance.onboarding_provider', provider_company)
        _logger.info(f"Onboarding: Provider selected → {provider_company}")
        return {'success': True}

    @api.model
    def save_onboarding_api_key(self, provider_company, api_key):
        """
        Save the API key for all LLM records of the chosen provider (Step 2).
        Verifies the key with a real API call before saving.
        """
        # Verify API key first
        verification = self._verify_api_key(provider_company, api_key)
        if not verification.get('success'):
            return verification
        
        provider_company_normalized = (provider_company or '').strip().lower()
        if provider_company_normalized == 'openrouter':
            provider_llms = self.sudo().search([('provider_type', '=', 'openrouter')])
        else:
            provider_llms = self.sudo().search([('sh_company', '=', provider_company)])

        if provider_llms:
            provider_llms.write({'sh_api_key': api_key})

        response = {'success': True, 'message': 'API key verified successfully!'}

        if provider_company_normalized == 'openrouter':
            fetch_llm = provider_llms[:1]
            if not fetch_llm:
                return {
                    'success': False,
                    'message': 'OpenRouter provider record was not found. Please reinstall demo data or create one provider first.',
                }
            try:
                payloads = fetch_llm._fetch_openrouter_models_payload()
                sync_result = self.env['sh.ai.openrouter.model'].sudo().sync_from_openrouter_payload(payloads)
            except Exception as error:
                return {
                    'success': False,
                    'message': 'OpenRouter sync failed: %s' % error,
                }

            provider_models = self._get_openrouter_onboarding_models()
            if not provider_models:
                return {
                    'success': False,
                    'message': 'No function-calling models were returned from OpenRouter for this API key.',
                }
            response.update({
                'message': 'OpenRouter API key verified successfully!',
                'models': provider_models,
                'sync_result': sync_result,
            })
        
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('sh_ai_assistance.onboarding_state', 'api_key_set')
        _logger.info(f"Onboarding: API key verified and saved for {provider_company}")
        return response

    @api.model
    def _verify_api_key(self, provider_company, api_key):
        """
        Verify an API key by making a real test call to the provider.
        Returns {'success': True/False, 'message': str}
        """
        if not api_key or not api_key.strip():
            return {'success': False, 'message': 'Please enter an API key.'}

        provider_company_normalized = (provider_company or '').strip().lower()
        provider_type = 'gemini'
        if provider_company_normalized == 'openrouter':
            provider_type = 'openrouter'
        elif 'deepseek' in provider_company_normalized:
            provider_type = 'deepseek'
        elif 'anthropic' in provider_company_normalized or 'claude' in provider_company_normalized:
            provider_type = 'claude'
        elif provider_company_normalized == 'openai':
            provider_type = 'openai'

        try:
            if provider_type == 'openai':
                return self._verify_openai_key(api_key)
            if provider_type == 'openrouter':
                return self._verify_openrouter_key(api_key)
            if provider_type == 'deepseek':
                return self._verify_deepseek_key(api_key)
            if provider_type == 'claude':
                return self._verify_claude_key(api_key)
            else:
                return self._verify_gemini_key(api_key)
        except Exception as e:
            _logger.error(f"API key verification failed: {str(e)}")
            return {
                'success': False,
                'message': f'Verification failed: {str(e)}',
            }

    @api.model
    def _verify_deepseek_key(self, api_key):
        """Test a DeepSeek API key with a minimal request"""
        try:
            
            client = OpenAI(api_key=api_key, base_url='https://api.deepseek.com')
            response = client.chat.completions.create(
                model='deepseek-v3',
                messages=[{'role': 'user', 'content': 'Say "hello" in one word.'}],
                max_tokens=5,
            )
            if response and response.choices:
                return {'success': True, 'message': 'DeepSeek API key is valid!'}
            return {'success': False, 'message': 'DeepSeek returned an empty response. Please check your API key.'}
        except Exception as e:
            error_msg = str(e).lower()
            if 'api_key' in error_msg or 'invalid' in error_msg or '401' in error_msg or 'authentication' in error_msg:
                return {'success': False, 'message': 'Invalid API key. Please check and try again.'}
            if 'quota' in error_msg or '429' in error_msg:
                return {'success': True, 'message': 'API key is valid (quota limit reached, but key accepted).'}
            return {'success': False, 'message': f'Connection error: {str(e)}'}

    @api.model
    def _verify_claude_key(self, api_key):
        """Test a Claude API key with a minimal request"""
        try:
            url = 'https://api.anthropic.com/v1/messages'
            payload = json.dumps({
                'model': 'claude-sonnet-4-6',
                'max_tokens': 5,
                'messages': [{'role': 'user', 'content': 'Say "hello" in one word.'}],
            }).encode('utf-8')
            request = Request(
                url,
                data=payload,
                headers={
                    'x-api-key': api_key,
                    'anthropic-version': '2023-06-01',
                    'content-type': 'application/json',
                },
                method='POST',
            )
            ssl_context = ssl.create_default_context(cafile=certifi.where())
            with urlopen(request, timeout=30, context=ssl_context) as response:
                response_data = json.loads(response.read().decode('utf-8'))
            if response_data and response_data.get('content'):
                return {'success': True, 'message': 'Claude API key is valid!'}
            return {'success': False, 'message': 'Claude returned an empty response. Please check your API key.'}
        except Exception as e:
            error_msg = str(e).lower()
            if 'api_key' in error_msg or 'invalid' in error_msg or '401' in error_msg or 'authentication' in error_msg:
                return {'success': False, 'message': 'Invalid API key. Please check and try again.'}
            if 'quota' in error_msg or '429' in error_msg:
                return {'success': True, 'message': 'API key is valid (quota limit reached, but key accepted).'}
            return {'success': False, 'message': f'Connection error: {str(e)}'}

    @api.model
    def _verify_gemini_key(self, api_key):
        """Test a Gemini API key with a minimal request"""
        try:
            from google import genai
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents='Say "hello" in one word.',
            )
            if response and response.text:
                return {'success': True, 'message': 'Gemini API key is valid!'}
            return {'success': False, 'message': 'Gemini returned an empty response. Please check your API key.'}
        except Exception as e:
            error_msg = str(e).lower()
            if 'api_key' in error_msg or 'invalid' in error_msg or '401' in error_msg or '403' in error_msg:
                return {'success': False, 'message': 'Invalid API key. Please check and try again.'}
            if 'quota' in error_msg or '429' in error_msg:
                return {'success': True, 'message': 'API key is valid (quota limit reached, but key accepted).'}
            return {'success': False, 'message': f'Connection error: {str(e)}'}

    @api.model
    def _verify_openai_key(self, api_key):
        """Test an OpenAI API key with a minimal request"""
        try:
            
            client = OpenAI(api_key=api_key)
            response = client.chat.completions.create(
                model='gpt-4o-mini',
                messages=[{'role': 'user', 'content': 'Say "hello" in one word.'}],
                max_tokens=5,
            )
            if response and response.choices:
                return {'success': True, 'message': 'OpenAI API key is valid!'}
            return {'success': False, 'message': 'OpenAI returned an empty response. Please check your API key.'}
        except Exception as e:
            error_msg = str(e).lower()
            if 'api_key' in error_msg or 'invalid' in error_msg or '401' in error_msg or 'authentication' in error_msg:
                return {'success': False, 'message': 'Invalid API key. Please check and try again.'}
            if 'quota' in error_msg or '429' in error_msg:
                return {'success': True, 'message': 'API key is valid (quota limit reached, but key accepted).'}
            return {'success': False, 'message': f'Connection error: {str(e)}'}

    @api.model
    def _verify_openrouter_key(self, api_key):
        """Test an OpenRouter API key with a minimal request"""
        try:
            
            client = OpenAI(
                api_key=api_key,
                base_url='https://openrouter.ai/api/v1',
                default_headers={
                    'HTTP-Referer': 'https://openrouter.ai',
                    'X-OpenRouter-Title': 'Odoo SH AI Assistance',
                },
            )
            response = client.chat.completions.create(
                model='openai/gpt-4o-mini',
                messages=[{'role': 'user', 'content': 'Say "hello" in one word.'}],
                max_tokens=5,
            )
            if response and response.choices:
                return {'success': True, 'message': 'OpenRouter API key is valid!'}
            return {'success': False, 'message': 'OpenRouter returned an empty response. Please check your API key.'}
        except Exception as e:
            error_msg = str(e).lower()
            if 'api_key' in error_msg or 'invalid' in error_msg or '401' in error_msg or 'authentication' in error_msg:
                return {'success': False, 'message': 'Invalid API key. Please check and try again.'}
            if 'quota' in error_msg or '429' in error_msg:
                return {'success': True, 'message': 'API key is valid (quota limit reached, but key accepted).'}
            return {'success': False, 'message': f'Connection error: {str(e)}'}

    @api.model
    def save_onboarding_default_model(self, model_id):
        """
        Set the chosen model as default (Step 3).
        """
        ICP = self.env['ir.config_parameter'].sudo()
        provider_company = ICP.get_param('sh_ai_assistance.onboarding_provider', '')
        if (provider_company or '').strip().lower() == 'openrouter':
            llm = self.sudo().search([('provider_type', '=', 'openrouter')], order='is_default desc, id asc', limit=1)
            openrouter_model = self.env['sh.ai.openrouter.model'].sudo().browse(model_id)
            if llm.exists() and openrouter_model.exists():
                llm.write({
                    'openrouter_model_id': openrouter_model.id,
                    'is_default': True,
                })
                _logger.info("Onboarding: OpenRouter default model set → %s", openrouter_model.name)
            return {'success': True}

        llm = self.sudo().browse(model_id)
        if llm.exists():
            # Set as default (this triggers _unset_others_default via write)
            llm.write({'is_default': True})
            _logger.info(f"Onboarding: Default model set → {llm.name}")
        return {'success': True}

    @api.model
    def save_onboarding_style(self, style):
        """
        Set the response style/temperature for the default model (Step 4).
        """
        default_llm = self.sudo().search([('is_default', '=', True)], limit=1)
        if default_llm:
            default_llm.write({'temperature': style})
            _logger.info(f"Onboarding: Response style set → {style}")
        
        # Mark onboarding as completed
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('sh_ai_assistance.onboarding_state', 'completed')
        return {'success': True}

    @api.model
    def complete_onboarding(self):
        """Mark onboarding as complete (called after the success screen)."""
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('sh_ai_assistance.onboarding_state', 'completed')
        _logger.info("Onboarding: Completed!")
        return {'success': True}

    @api.model
    def skip_onboarding(self):
        """
        Skip onboarding. Sets state to 'skipped' so the banner reminder shows.
        Chat input remains disabled until setup is completed.
        """
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('sh_ai_assistance.onboarding_state', 'skipped')
        _logger.info("Onboarding: Skipped by user")
        return {'success': True}

    @api.model
    def mark_user_onboarding_seen(self):
        """Mark that the current user has seen the welcome tour."""
        self.env.user.sudo().write({'sh_ai_onboarding_seen': True})
        return {'success': True}
