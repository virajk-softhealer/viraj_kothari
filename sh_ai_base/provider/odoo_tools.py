# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies Pvt. Ltd.

import json
import logging
import os
from copy import deepcopy

from odoo.exceptions import AccessError, ValidationError

_logger = logging.getLogger(__name__)

_MANAGE_MODULE_ALLOWED_TEXT_EXTENSIONS = {
    ".xml",
    ".csv",
    ".sql",
    ".po",
    ".js",
    ".css",
    ".scss",
    ".json",
    ".html",
    ".txt",
    ".md",
    ".rst",
}
_MANAGE_MODULE_ALLOWED_BINARY_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".ico",
}
_MANAGE_MODULE_REQUIRED_FILES = {"__manifest__.py"}
_BLOCKED_MCP_CONTROL_MODELS = {
    "sh.ai.mcp.server",
    "sh.ai.mcp.oauth.code",
    "sh.ai.mcp.oauth.token",
    "sh.ai.mcp.client.state",
    "sh.ai.mcp.session",
    "sh.ai.mcp.blocked.field.rule",
    "sh.ai.mcp.tool.access.rule",
    "sh.ai.mcp.execution.log",
}


def _get_blocked_mcp_control_models():
    """Return the internal MCP control models that must never be exposed through MCP tools."""
    return set(_BLOCKED_MCP_CONTROL_MODELS)


def _get_blocked_mcp_model_error(model_name):
    return "AI access to internal MCP control model '%s' is blocked." % model_name


def _validate_mcp_model_allowed(model_name):
    if model_name in _get_blocked_mcp_control_models():
        return _get_blocked_mcp_model_error(model_name)
    return None

def fuzzy_lookup_declaration():
    """
    Function declaration for smart name-to-ID resolution.
    AI should use this BEFORE querying relational fields with names.
    """
    return {
        "name": "fuzzy_lookup",
        "description": "Smart name-to-ID resolver for relational fields. Use this BEFORE querying when you need to filter by a Many2one or Many2many field using a name. It searches all text fields dynamically and strips category words.",
        "parameters": {
            "type": "object",
            "properties": {
                "queries": {
                    "anyOf": [
                        {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "model": {"type": "string"},
                                    "search_term": {"type": "string"},
                                    "limit": {"type": "integer"}
                                },
                                "required": ["model", "search_term"],
                                "additionalProperties": False
                            }
                        },
                        {"type": "null"}
                    ],
                    "description": "A list of lookups to execute. Allows resolving multiple names across different models in a single call."
                },
                "model": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": "The Odoo model to search (e.g., 'hr.department', 'res.partner'). Required if not using 'queries'.",
                },
                "search_term": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": "The name/term to search for (e.g., 'Treasure Department'). Required if not using 'queries'.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum candidates to return per lookup (default: 5)",
                }
            },
            "required": [],
            "additionalProperties": False,
        },
    }


def fuzzy_lookup(env, model=None, search_term=None, limit=5, queries=None):
    """
    Fully dynamic name-to-ID resolver.
    Searches across ALL text-based fields dynamically.
    No hardcoded suffixes or field names.
    Supports batch lookup via 'queries'.
    """
    try:
        if queries:
            if not isinstance(queries, list):
                return {"success": False, "error": "'queries' must be a list of objects.", "model": "mixed"}
            
            results = []
            for query in queries:
                q_model = query.get("model")
                q_term = query.get("search_term")
                q_limit = query.get("limit", 5)
                
                if not q_model or not q_term:
                    return {"success": False, "error": "Each query must contain 'model' and 'search_term'."}
                
                # Recursive call
                sub_res = fuzzy_lookup(env, model=q_model, search_term=q_term, limit=q_limit)
                results.append({
                    "model": q_model,
                    "search_term": q_term,
                    "success": sub_res.get("success", False),
                    "error": sub_res.get("error"),
                    "results": sub_res.get("results", [])
                })
            return {
                "success": True,
                "model": "mixed",
                "queries": results
            }

        if not model or not search_term:
            return {"success": False, "error": "Must provide either 'queries' or both 'model' and 'search_term'."}

        blocked_error = _validate_mcp_model_allowed(model)
        if blocked_error:
            return {"success": False, "error": blocked_error, "results": []}
        try:
            Model = env[model]
        except KeyError:
            return {"success": False, "error": f"Model '{model}' not found", "results": []}

        # SECURITY CHECK: Verify user has read access
        try:
            Model.browse().check_access('read')
        except Exception:
            return {"success": False, "error": "Access denied", "results": []}

        # Step 1: Detect and strip category words dynamically using ir.model
        clean_term = search_term.strip()
        model_info = env['ir.model'].sudo().search([('model', '=', model)], limit=1)
        model_display_name = model_info.name if model_info else (Model._description or '')
        
        if model_display_name:
            display_words = model_display_name.lower().split()
            term_words_lower = clean_term.lower().split()
            term_words_original = clean_term.split()
            
            # Remove trailing words that match model display name
            while term_words_lower and display_words and term_words_lower[-1] == display_words[-1]:
                term_words_lower.pop()
                term_words_original.pop()
                display_words.pop()
            
            if term_words_original:
                clean_term = ' '.join(term_words_original)

        # Step 2: Dynamically identify text-searchable fields
        fields_info = Model.fields_get()
        searchable_fields = [
            fname for fname, info in fields_info.items()
            if info.get('type') in ('char', 'text', 'html') and info.get('searchable', True) and not fname.startswith('_')
        ]
        
        if not searchable_fields:
            searchable_fields = [Model._rec_name] if hasattr(Model, '_rec_name') else ['name']

        # Step 3: Search using Odoo's native OR logic
        def get_or_domain(term, operator='ilike'):
            domain = ['|'] * (len(searchable_fields) - 1)
            for f in searchable_fields:
                domain.append((f, operator, term))
            return domain

        # Strategy A: Exact-ish match (=ilike)
        results = Model.search(get_or_domain(clean_term, '=ilike'), limit=limit)
        
        # Strategy B: Fuzzy match (ilike)
        if not results:
            results = Model.search(get_or_domain(clean_term, 'ilike'), limit=limit)
            
        # Strategy C: All words combined (AND logic)
        if not results and ' ' in clean_term:
            words = [w for w in clean_term.split() if len(w) > 2]
            if words:
                combined_domain = []
                for word in words:
                    combined_domain.extend(get_or_domain(word, 'ilike'))
                results = Model.search(combined_domain, limit=limit)

        return {
            "success": True,
            "results": [{"id": r.id, "display_name": r.display_name} for r in results],
            "model": model,
            "search_term": search_term,
            "cleaned_term": clean_term,
            "fields_searched": searchable_fields[:5]
        }
    except Exception as e:
        return {"success": False, "error": str(e), "results": []}


def _validate_and_fix_domain(env, model, domain):
    """
    Dynamically intercept and fix domains with string values on relational fields.
    Handles Many2one and Many2many using Odoo metadata.
    """
    if not domain or not isinstance(domain, list):
        return domain, []
    
    corrections = []
    try:
        Model = env[model]
    except Exception:
        return domain, []
    
    fixed_domain = []
    
    for clause in domain:
        # Handle operators ('&', '|', '!')
        if not isinstance(clause, (list, tuple)) or len(clause) != 3:
            fixed_domain.append(clause)
            continue
        
        field_name, operator, value = clause
        path_info = _resolve_field_path(env, model, field_name)
        normalized_clause, correction = _normalize_relational_domain_clause(
            env,
            field_name,
            operator,
            value,
            path_info,
        )
        if normalized_clause is not None:
            fixed_domain.append(normalized_clause)
            if correction:
                corrections.append(correction)
            continue
        if correction:
            corrections.append(correction)

        fixed_domain.append(clause)
        
    return fixed_domain, corrections


def _resolve_field_path(env, model, field_path):
    """
    Resolve a dotted field path against Odoo model metadata.

    Returns:
        dict with final field metadata or None if path cannot be resolved.
    """
    current_model = model
    current_field_info = None

    for segment in (field_path or '').split('.'):
        try:
            Model = env[current_model]
        except Exception:
            return None

        fields_info = Model.fields_get()
        current_field_info = fields_info.get(segment)
        if not current_field_info:
            return None

        relation_model = current_field_info.get('relation')
        if relation_model:
            current_model = relation_model

    if not current_field_info:
        return None

    return {
        'field_type': current_field_info.get('type'),
        'relation_model': current_field_info.get('relation'),
    }


def _pick_unique_lookup_result(resolved, raw_value):
    """
    Pick a single lookup result only when the match is unambiguous.
    """
    if not resolved:
        return None, "unresolved"

    if len(resolved) == 1:
        return resolved[0], None

    exact_matches = [
        candidate for candidate in resolved
        if (candidate.get('display_name') or '').strip().lower() == raw_value.strip().lower()
    ]

    if len(exact_matches) == 1:
        return exact_matches[0], None

    return None, "ambiguous"


def _normalize_relational_domain_clause(env, field_name, operator, value, path_info):
    """
    Normalize string filters on relational fields to ID-based filters.
    """
    if not path_info:
        return None, None

    field_type = path_info.get('field_type')
    relation_model = path_info.get('relation_model')
    supported_field_types = ('many2one', 'many2many')
    supported_operators = ('=', '!=', 'in', 'not in')

    if field_type not in supported_field_types or not relation_model:
        return None, None

    if operator not in supported_operators:
        return None, None

    if not isinstance(value, str):
        return None, None

    lookup = fuzzy_lookup(env, relation_model, value, limit=5)
    resolved = lookup.get('results', [])
    selected, resolution_state = _pick_unique_lookup_result(resolved, value)

    if not selected:
        if resolution_state == "ambiguous":
            return None, f"Warning: Ambiguous match for '{value}' on {field_name}; keeping original filter"
        return None, f"Warning: Could not resolve '{value}' for {field_name}"

    resolved_id = selected['id']
    resolved_name = selected['display_name']

    if field_type == 'many2one':
        if operator in ('in', 'not in'):
            normalized_value = [resolved_id]
        else:
            normalized_value = resolved_id
        normalized_operator = operator
    else:
        normalized_value = [resolved_id]
        if operator in ('=', 'in'):
            normalized_operator = 'in'
        else:
            normalized_operator = 'not in'

    return (
        [field_name, normalized_operator, normalized_value],
        f"Fixed {field_name}: '{value}' -> {resolved_name} (ID: {resolved_id})",
    )


def _sanitize_field_value(value, max_length=80):
    """
    Sanitize field values for AI consumption.

    Prevents:
    - Prompt injection through extremely long field values
    - Context overflow from large text fields

    Args:
        value: Field value to sanitize
        max_length: Maximum length for string values

    Returns:
        Sanitized value safe for AI prompts
    """
    if isinstance(value, str):
        # Truncate long strings to prevent prompt injection
        if len(value) > max_length:
            return value[:max_length-3] + "..."
    return value


def _limit_relational_fields(records_data, max_items=30):
    """
    Limit items in relational fields to prevent context flooding.

    Args:
        records_data: List of record dictionaries
        max_items: Maximum items to keep in list/array fields

    Returns:
        Modified records with limited relational fields
    """
    for record in records_data:
        for key, value in list(record.items()):
            # Limit array/list fields (o2m, m2m typically return lists)
            if isinstance(value, (list, tuple)) and len(value) > max_items:
                record[key] = {
                    '_truncated': True,
                    'count': len(value),
                    'items': value[:max_items],
                    'note': f'Showing first {max_items} of {len(value)} items'
                }
    return records_data


def get_search_records_declaration():
    """
    Function declaration for searching Odoo records.
    This is a generic tool that works with any Odoo model.
    """
    return {
        "name": "search_records",
        "strict": True,
        "description": "Search and retrieve records from any Odoo model (e.g., sale.order, res.partner). Use this to query database information like counting records, finding specific data, or retrieving field values.",
        "parameters": {
            "type": "object",
            "properties": {
                "queries": {
                    "anyOf": [
                        {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "model": {"type": "string"},
                                    "domain": {
                                        "anyOf": [
                                            {
                                                "type": "array",
                                                "items": {
                                                    "anyOf": [
                                                        {"type": "string"},
                                                        {
                                                            "type": "array",
                                                            "items": {
                                                                "anyOf": [
                                                                    {"type": "string"},
                                                                    {"type": "number"},
                                                                    {"type": "boolean"},
                                                                    {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "number"}]}}
                                                                ]
                                                            }
                                                        }
                                                    ]
                                                }
                                            },
                                            {"type": "null"}
                                        ]
                                    },
                                    "fields": {
                                        "anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}]
                                    },
                                    "limit": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                                    "order": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                                    "offset": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                                    "group_by": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                                    "count_only": {"anyOf": [{"type": "boolean"}, {"type": "null"}]}
                                },
                                "required": ["model", "domain", "fields", "limit", "order", "offset", "group_by", "count_only"],
                                "additionalProperties": False,
                            }
                        },
                        {"type": "null"}
                    ],
                    "description": "A list of queries to execute. Allows searching multiple models in a single call.",
                },
                "model": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": "The technical name of the Odoo model to search (e.g., 'sale.order', 'res.partner')",
                },
                "domain": {
                    "anyOf": [
                        {
                            "type": "array",
                            "items": {
                                "anyOf": [
                                    {"type": "string"},  # For operators like '|', '&', '!'
                                    {
                                        "type": "array",  # For conditions like ['field', 'operator', 'value']
                                        "items": {
                                            "anyOf": [
                                                {"type": "string"},
                                                {"type": "number"},
                                                {"type": "boolean"},
                                                {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "number"}]}}
                                            ]
                                        }
                                    }
                                ]
                            }
                        },
                        {"type": "null"}
                    ],
                    "description": "Odoo domain filter. Format: [[field, operator, value], ...] or ['|', [...], [...]]. Pass null if no filter needed.",
                },
                "fields": {
                    "anyOf": [
                        {
                            "type": "array",
                            "items": {"type": "string"}
                        },
                        {"type": "null"}
                    ],
                    "description": "List of field names to retrieve. Pass null to get default fields.",
                },
                "limit": {
                    "anyOf": [{"type": "integer"}, {"type": "null"}],
                    "description": "Maximum records to return. Pass null for the connector or tool default limit.",
                },
                "order": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": "Sort order (e.g., 'amount_total desc'). Pass null for default.",
                },
                "offset": {
                    "anyOf": [{"type": "integer"}, {"type": "null"}],
                    "description": "Pagination offset. Pass null for 0.",
                },
                "group_by": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": "Field name to group by for list view grouping only. This does not return grouped aggregates; use aggregate_records for grouped summaries.",
                },
                "count_only": {
                    "anyOf": [{"type": "boolean"}, {"type": "null"}],
                    "description": "Set to true if you only need the count. Pass null/false if actual data needed.",
                }
            },
            "required": ["queries", "model", "domain", "fields", "limit", "order", "offset", "group_by", "count_only"],
            "additionalProperties": False,
        },
    }


def search_records(env, model=None, domain=None, fields=None, limit=None, order=None, offset=0, group_by=None, count_only=False, queries=None):
    """
    Enhanced search_records with superior performance and security.

    Uses Odoo's search_read() for 50% better performance (1 query vs 2).
    Includes our security enhancements and error handling.

    Args:
        env: Odoo environment
        model: Model technical name
        domain: Search domain (Python list or JSON string)
        fields: Fields to retrieve
        limit: Maximum records to return
        order: Sort order (e.g., 'name asc', 'amount_total desc')
        offset: Number of records to skip (for pagination)
        group_by: Field name to group by (for list view grouping)
        count_only: Return only count (no records)
        queries: List of search queries to execute in a single call.

    Returns:
        dict with success, count, records, and optional group_by
    """
    try:
        if queries:
            if not isinstance(queries, list):
                return {"success": False, "error": "'queries' must be a list of objects.", "model": "mixed"}
            
            results = []
            for query in queries:
                q_model = query.get("model")
                if not q_model:
                    return {"success": False, "error": "Each item in 'queries' must contain 'model'.", "model": "mixed"}
                
                # Recursive call for each sub-query
                q_result = search_records(
                    env,
                    model=q_model,
                    domain=query.get("domain"),
                    fields=query.get("fields"),
                    limit=query.get("limit"),
                    order=query.get("order"),
                    offset=query.get("offset") or 0,
                    group_by=query.get("group_by"),
                    count_only=query.get("count_only") or False,
                )
                
                results.append({
                    "model": q_model,
                    "result": q_result
                })
                
            return {
                "success": True,
                "model": "mixed",
                "results": results,
                "summary": f"Successfully executed {len(queries)} search queries."
            }

        if not model:
            return {"success": False, "error": "Either 'queries' array OR 'model' must be provided.", "model": "unknown"}

        blocked_error = _validate_mcp_model_allowed(model)
        if blocked_error:
            return {
                "success": False,
                "error": blocked_error,
                "model": model,
            }
        # Parse domain - support both Python list and JSON string
        if domain is None:
            domain = []
        elif isinstance(domain, str):
            # Support JSON string domains for flexibility
            import json
            try:
                domain = json.loads(domain)
            except json.JSONDecodeError:
                return {
                    "success": False,
                    "error": "Invalid domain format. Use Python list or valid JSON string.",
                    "model": model,
                }

        # Validate domain structure
        if not isinstance(domain, list):
            return {
                "success": False,
                "error": "Domain must be a list of conditions.",
                "model": model,
            }

        # Get model object
        try:
            Model = env[model]
        except KeyError:
            return {
                "success": False,
                "error": f"Model '{model}' not found. Please verify the model name.",
                "model": model,
            }

        # DYNAMIC FIX: Intercept string searches on relational fields
        original_domain = domain
        domain, corrections = _validate_and_fix_domain(env, model, domain)
        if corrections:
            _logger.info(f"🛠️ AI Domain Auto-Fix: {', '.join(corrections)}")


        # SECURITY CHECK: Verify user has read access to this model
        # This prevents unauthorized access to models user doesn't have permission for
        # Use browse() to create empty recordset - this checks model-level permissions
        try:
            Model.browse().check_access('read')
        except AccessError:
            return {
                "success": False,
                "error": "Access denied. You don't have permission to view this data.",
                "model": model,
            }

        # Count-only mode (fast)
        if count_only:
            count = Model.search_count(domain)
            return {
                "success": True,
                "count": count,
                "model": model,
            }

        # If group_by specified, ensure it's in fields list
        if group_by:
            group_by_list = [g.strip() for g in group_by.split(',') if g.strip()]
            
            if fields:
                fields = list(fields)  # Copy to avoid modifying original
                for g in group_by_list:
                    base_field = g.split(':')[0].strip()
                    if base_field not in fields:
                        fields.append(base_field)

            if not order:
                # order by the base fields
                order_fields = [g.split(':')[0].strip() for g in group_by_list]
                order_fields.append("id")
                order = ", ".join(order_fields)

        # CONSISTENCY ENFORCEMENT: Default and maximum limits are configurable through context.
        # MCP connectors inject these values per server; non-MCP callers fall back to 10.
        default_limit = env.context.get("sh_ai_search_default_limit", 10)
        max_display_limit = env.context.get("sh_ai_search_max_limit", 10)
        try:
            default_limit = max(1, int(default_limit))
        except (TypeError, ValueError):
            default_limit = 10
        try:
            max_display_limit = max(1, int(max_display_limit))
        except (TypeError, ValueError):
            max_display_limit = 10
        default_limit = min(default_limit, max_display_limit)

        if limit is None:
            limit = default_limit

        # CRITICAL: Maximum limit enforcement prevents chat/UI overload on large datasets.
        if limit < 1:
            limit = 1
        if limit > max_display_limit:
            limit = max_display_limit

        # PERFORMANCE UPGRADE: Use search_read() instead of search() + read()
        # This reduces database queries from 2 to 1 (50% performance improvement)
        data = Model.search_read(
            domain=domain,
            fields=fields,
            offset=offset,
            limit=limit,
            order=order
        )

        # Get total count for pagination info
        total_count = Model.search_count(domain)

        # OUR SECURITY ENHANCEMENT: Sanitize field values
        for record in data:
            # Sanitize text fields to prevent prompt injection
            for field_name in ['name', 'display_name', 'description', 'note', 'notes']:
                if field_name in record:
                    record[field_name] = _sanitize_field_value(record[field_name])

        # OUR PERFORMANCE ENHANCEMENT: Limit relational fields
        data = _limit_relational_fields(data)

        # Build enhanced response
        response = {
            "success": True,
            "count": len(data),
            "total_count": total_count,  # Total matching records (for pagination)
            "model": model,
            "records": data,
            "domain": domain,
            "order": order,
            "offset": offset,
            "limit": limit,
        }

        # Include group_by for UI grouping
        if group_by:
            response["group_by"] = group_by
            response["group_by_mode"] = "ui_hint"
            response["note"] = "group_by organizes returned rows for browsing. Use aggregate_records for grouped totals or summaries."

        return response

    except AccessError:
        # Odoo's AccessError is raised when user lacks permission
        return {
            "success": False,
            "error": "Access denied. You don't have permission to view this data.",
            "model": model,
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Error querying {model}: {str(e)}",
            "model": model,
        }


def get_models_list_declaration():
    """
    Function declaration for discovering available Odoo models.
    This tool enables AI to learn what models exist in the database.
    """
    return {
        "name": "get_models_list",
        "strict": True,
        "description": "Refresh the list of available Odoo models. Returns all models you have read access to.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    }


def get_models_list(env):
    """
    Execute the get_models_list function.
    Returns list of ALL models current user has READ access to with rich metadata.
    """
    try:
        # Use Odoo's security-aware method from web module
        all_models = env['ir.model'].get_available_models()
        blocked_models = _get_blocked_mcp_control_models()

        # Enhance each model with additional context
        enhanced_models = []
        for model in all_models:
            if model.get('model') in blocked_models:
                continue
            enhanced = {
                "model": model['model'],
                "display_name": model['display_name'],
            }

            # Try to get model description if available
            try:
                Model = env[model['model']]
                if hasattr(Model, '_description') and Model._description:
                    enhanced["description"] = Model._description
            except Exception:
                pass

            enhanced_models.append(enhanced)

        return {
            "success": True,
            "count": len(enhanced_models),
            "models": enhanced_models,
            "note": "Complete catalog of models you have access to"
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }


def get_model_fields_declaration():
    """
    Function declaration for discovering fields of Odoo models.
    """
    return {
        "name": "get_model_fields",
        "strict": True,
        "description": "Discover fields and structure of Odoo models. Use this to understand what fields exist, their types, and relationships.",
        "parameters": {
            "type": "object",
            "properties": {
                "models": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of model technical names to get fields for (e.g., ['sale.order']).",
                },
                "field_types": {
                    "anyOf": [
                        {
                            "type": "array",
                            "items": {"type": "string"}
                        },
                        {"type": "null"}
                    ],
                    "description": "Optional: Filter by field types (e.g., ['many2one']). Pass null for all fields.",
                }
            },
            "required": ["models", "field_types"],
            "additionalProperties": False,
        },
    }


def get_model_fields(env, models, field_types=None):
    """
    Execute the get_model_fields function.
    Returns field information for specified models.
    """
    try:
        # Convert single model to list
        if isinstance(models, str):
            models = [models]

        result = {}

        for model_name in models:
            try:
                blocked_error = _validate_mcp_model_allowed(model_name)
                if blocked_error:
                    result[model_name] = {
                        'success': False,
                        'error': blocked_error,
                    }
                    continue
                # Check if user has access to this model
                Model = env[model_name]
                if not Model.browse().has_access('read'):
                    result[model_name] = {
                        'success': False,
                        'error': 'Access denied',
                    }
                    continue

                # Get fields using fields_get() which is security-aware
                fields_data = Model.fields_get()

                # Filter by field types if specified
                if field_types:
                    fields_data = {
                        fname: fdata
                        for fname, fdata in fields_data.items()
                        if fdata.get('type') in field_types
                    }

                # Simplify field data for AI consumption
                simplified_fields = {}
                for fname, fdata in fields_data.items():
                    simplified_fields[fname] = {
                        'type': fdata.get('type'),
                        'string': fdata.get('string'),
                        'required': fdata.get('required', False),
                        'readonly': fdata.get('readonly', False),
                        'relation': fdata.get('relation'),
                        'help': fdata.get('help', ''),
                    }
                    
                    if fdata.get('type') in ('many2one', 'many2many', 'one2many'):
                        rel_model = fdata.get('relation')
                        try:
                            rel_info = env['ir.model'].sudo().search([('model', '=', rel_model)], limit=1)
                            simplified_fields[fname]['relation_label'] = rel_info.name if rel_info else rel_model
                        except Exception:
                            simplified_fields[fname]['relation_label'] = rel_model

                result[model_name] = {
                    'success': True,
                    'field_count': len(simplified_fields),
                    'fields': simplified_fields,
                }

            except Exception as e:
                result[model_name] = {
                    'success': False,
                    'error': f"Cannot access model: {str(e)}",
                }

        return {
            "success": True,
            "models_count": len(models),
            "models": result,
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }


def get_selection_values_declaration():
    """
    Function declaration for getting valid selection field values.
    """
    return {
        "name": "get_selection_values",
        "strict": True,
        "description": "Get valid values for selection fields (state, type, priority, etc.).",
        "parameters": {
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "description": "The technical name of the Odoo model (e.g., 'sale.order')",
                },
                "field": {
                    "type": "string",
                    "description": "The selection field name (e.g., 'state')",
                },
            },
            "required": ["model", "field"],
            "additionalProperties": False,
        },
    }


def get_selection_values(env, model, field):
    """
    Get valid values for a selection field.
    """
    try:
        blocked_error = _validate_mcp_model_allowed(model)
        if blocked_error:
            return {
                "success": False,
                "error": blocked_error,
            }
        try:
            Model = env[model]
        except KeyError:
            return {
                "success": False,
                "error": f"Model '{model}' not found",
            }

        if not Model.browse().has_access('read'):
            return {
                "success": False,
                "error": f"Access denied for model '{model}'",
            }

        fields_data = Model.fields_get([field])

        if field not in fields_data:
            return {
                "success": False,
                "error": f"Field '{field}' not found in model '{model}'",
            }

        field_info = fields_data[field]

        if field_info.get('type') != 'selection':
            return {
                "success": False,
                "error": f"Field '{field}' is not a selection field (type: {field_info.get('type')})",
            }

        selection_values = field_info.get('selection', [])

        if not selection_values:
            return {
                "success": True,
                "model": model,
                "field": field,
                "values": {},
                "note": "No selection values defined for this field"
            }

        values_dict = {value: label for value, label in selection_values}

        return {
            "success": True,
            "model": model,
            "field": field,
            "field_label": field_info.get('string', field),
            "values": values_dict,
            "count": len(values_dict),
            "note": f"Use these values when filtering by '{field}' in domain filters"
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }


def get_current_date_info_declaration():
    """
    Function declaration for getting current date and time information.
    """
    return {
        "name": "get_current_date_info",
        "strict": True,
        "description": "Get current date, time, and period information (this week, this month, etc.). Use this for queries with 'today', 'last month', etc.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    }


def get_current_date_info(env):
    """
    Get current date and time information with period calculations.
    """
    try:
        from datetime import datetime, timedelta
        from dateutil.relativedelta import relativedelta

        now = datetime.now()

        current_quarter = (now.month - 1) // 3 + 1
        quarter_start_month = (current_quarter - 1) * 3 + 1
        quarter_start = datetime(now.year, quarter_start_month, 1)

        if current_quarter < 4:
            quarter_end_month = current_quarter * 3 + 1
            quarter_end = datetime(now.year, quarter_end_month, 1) - timedelta(days=1)
        else:
            quarter_end = datetime(now.year, 12, 31)

        month_start = datetime(now.year, now.month, 1)
        next_month = month_start + relativedelta(months=1)
        month_end = next_month - timedelta(days=1)

        year_start = datetime(now.year, 1, 1)
        year_end = datetime(now.year, 12, 31)

        week_start = now - timedelta(days=now.weekday())
        week_end = week_start + timedelta(days=6)

        return {
            "success": True,
            "current_datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
            "current_date": now.strftime("%Y-%m-%d"),
            "current_time": now.strftime("%H:%M:%S"),
            "year": now.year,
            "month": now.month,
            "day": now.day,
            "quarter": current_quarter,
            "periods": {
                "this_quarter": {
                    "start": quarter_start.strftime("%Y-%m-%d"),
                    "end": quarter_end.strftime("%Y-%m-%d"),
                    "quarter_number": current_quarter
                },
                "this_month": {
                    "start": month_start.strftime("%Y-%m-%d"),
                    "end": month_end.strftime("%Y-%m-%d"),
                    "month_number": now.month
                },
                "this_year": {
                    "start": year_start.strftime("%Y-%m-%d"),
                    "end": year_end.strftime("%Y-%m-%d"),
                    "year_number": now.year
                },
                "this_week": {
                    "start": week_start.strftime("%Y-%m-%d"),
                    "end": week_end.strftime("%Y-%m-%d")
                }
            }
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }


def get_aggregate_records_declaration():
    """
    Advanced aggregation with ORM-powered database-side calculations.
    """
    return {
        "name": "aggregate_records",
        "strict": True,
        "description": "Perform aggregation (SUM, COUNT, AVG, MIN, MAX) with optional grouping. Use for totals, averages, and group-by analysis.",
        "parameters": {
            "type": "object",
            "properties": {
                "queries": {
                    "anyOf": [
                        {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "model": {"type": "string"},
                                    "operation": {"type": "string", "enum": ["sum", "avg", "min", "max", "count"]},
                                    "domain": {
                                        "anyOf": [
                                            {
                                                "type": "array",
                                                "items": {
                                                    "anyOf": [
                                                        {"type": "string"},
                                                        {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "number"}, {"type": "boolean"}, {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "number"}]}}]}}
                                                    ]
                                                }
                                            },
                                            {"type": "null"}
                                        ]
                                    },
                                    "field": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                                    "group_by": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                                    "having": {
                                        "anyOf": [
                                            {"type": "array", "items": {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "number"}]}}},
                                            {"type": "null"}
                                        ]
                                    },
                                    "limit": {"anyOf": [{"type": "integer"}, {"type": "null"}]}
                                },
                                "required": ["model", "operation", "domain", "field", "group_by", "having", "limit"],
                                "additionalProperties": False,
                            }
                        },
                        {"type": "null"}
                    ],
                    "description": "A list of queries to execute. Allows aggregating multiple models in a single call.",
                },
                "model": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": "Technical model name (e.g., 'sale.order'). Required if not using 'queries'.",
                },
                "domain": {
                    "anyOf": [
                        {
                            "type": "array",
                            "items": {
                                "anyOf": [
                                    {"type": "string"},
                                    {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "number"}, {"type": "boolean"}, {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "number"}]}}]}}
                                ]
                            }
                        },
                        {"type": "null"}
                    ],
                    "description": "Filter domain. array of arrays. Pass null for all records.",
                },
                "operation": {
                    "anyOf": [{"type": "string", "enum": ["sum", "avg", "min", "max", "count"]}, {"type": "null"}],
                    "description": "Aggregation type. Required if not using 'queries'.",
                },
                "field": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": "Field to aggregate. Required for sum/avg/min/max. Pass null for count.",
                },
                "group_by": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": "Group by field (e.g., 'partner_id'). Pass null if not needed.",
                },
                "having": {
                    "anyOf": [
                        {
                            "type": "array",
                            "items": {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "number"}]}}
                        },
                        {"type": "null"}
                    ],
                    "description": "Post-aggregation filter. Pass null if not needed.",
                },
                "limit": {
                    "anyOf": [{"type": "integer"}, {"type": "null"}],
                    "description": "Limit results (e.g., for 'top 10'). Pass null if not needed.",
                },
            },
            "required": ["queries", "model", "operation", "domain", "field", "group_by", "having", "limit"],
            "additionalProperties": False,
        },
    }


def aggregate_records(env, model=None, operation=None, domain=None, field=None, group_by=None, having=None, limit=None, queries=None, **kwargs):
    """
    SUPERIOR aggregate_records using Odoo's _read_group() ORM method.
    """
    try:
        if queries:
            if not isinstance(queries, list):
                return {"success": False, "error": "'queries' must be a list of objects.", "model": "mixed"}
            
            results = []
            for query in queries:
                q_model = query.get("model")
                q_operation = query.get("operation")
                if not q_model or not q_operation:
                    return {"success": False, "error": "Each item in 'queries' must contain 'model' and 'operation'.", "model": "mixed"}
                
                # Recursive call
                q_result = aggregate_records(
                    env,
                    model=q_model,
                    operation=q_operation,
                    domain=query.get("domain"),
                    field=query.get("field"),
                    group_by=query.get("group_by"),
                    having=query.get("having"),
                    limit=query.get("limit")
                )
                results.append({"model": q_model, "result": q_result})
                
            return {
                "success": True,
                "model": "mixed",
                "results": results,
                "summary": f"Successfully executed {len(queries)} aggregate queries."
            }

        if not model or not operation:
            return {"success": False, "error": "Either 'queries' array OR 'model' and 'operation' must be provided.", "model": model or "unknown"}

        blocked_error = _validate_mcp_model_allowed(model)
        if blocked_error:
            return {
                "success": False,
                "error": blocked_error,
            }
        valid_operations = ['sum', 'avg', 'min', 'max', 'count']
        if operation not in valid_operations:
            return {
                "success": False,
                "error": f"Invalid operation '{operation}'. Must be one of: {', '.join(valid_operations)}",
            }

        try:
            Model = env[model]
        except KeyError:
            return {
                "success": False,
                "error": f"Model '{model}' not found. Please verify the model name.",
            }

        try:
            Model.browse().check_access('read')
        except AccessError:
            return {
                "success": False,
                "error": "Access denied. You don't have permission to view this data.",
            }

        if operation in ['sum', 'avg', 'min', 'max'] and not field:
            return {
                "success": False,
                "error": f"Field parameter is required for '{operation}' operation.",
            }

        if domain is None:
            domain = []
        elif isinstance(domain, str):
            try:
                domain = json.loads(domain)
            except json.JSONDecodeError:
                return {
                    "success": False,
                    "error": "Invalid domain format. Use Python list or valid JSON string.",
                }

        # DYNAMIC FIX: Intercept string searches on relational fields
        domain, corrections = _validate_and_fix_domain(env, model, domain)
        if corrections:
            _logger.info(f"🛠️ AI Aggregate Domain Auto-Fix: {', '.join(corrections)}")


        if group_by:
            group_by_list = [g.strip() for g in group_by.split(',') if g.strip()]
            processed_groupby = []
            
            for g in group_by_list:
                # AUTO-FIX: Append ':month' granularity if grouping by date/datetime field without granularity
                base_group_by = g.split(':')[0].strip()
                if base_group_by in Model._fields:
                    field_obj = Model._fields[base_group_by]
                    if field_obj.type in ('date', 'datetime') and ':' not in g:
                        processed_groupby.append(f"{g}:month")
                    else:
                        processed_groupby.append(g)
                else:
                    processed_groupby.append(g)

            try:
                if operation == 'count':
                    count_field = field or 'id'
                    aggregates = [f'{count_field}:count']
                else:
                    aggregates = [f'{field}:{operation}']

                result_groups = Model._read_group(
                    domain=domain,
                    groupby=processed_groupby,
                    aggregates=aggregates,
                    having=having or [],
                    offset=0,
                    limit=None
                )

                groups = []
                for group_tuple in result_groups:
                    group_vals = group_tuple[:len(processed_groupby)]
                    agg_val = group_tuple[-1]
                    
                    labels = []
                    for g_val in group_vals:
                        if hasattr(g_val, 'display_name'):
                            labels.append(g_val.display_name)
                        elif isinstance(g_val, (list, tuple)) and g_val:
                            labels.append(g_val[1] if len(g_val) > 1 else str(g_val[0]))
                        else:
                            labels.append(str(g_val) if g_val is not False else 'False')

                    label = " / ".join(labels)

                    groups.append({
                        "group": label,
                        "value": agg_val
                    })

                groups.sort(key=lambda x: (x['value'] or 0), reverse=True)

                if limit:
                    groups = groups[:limit]

                return {
                    "success": True,
                    "operation": operation,
                    "field": field,
                    "group_by": group_by,
                    "groups": groups,
                    "count": len(groups)
                }

            except Exception as e:
                return {
                    "success": False,
                    "error": f"Aggregation error: {str(e)}"
                }
        else:
            try:
                if operation == 'count':
                    result = Model.search_count(domain)
                else:
                    aggregates = [f'{field}:{operation}']
                    res = Model._read_group(domain=domain, groupby=[], aggregates=aggregates)
                    result = res[0][0] if res else 0

                return {
                    "success": True,
                    "operation": operation,
                    "field": field,
                    "result": result
                }
            except Exception as e:
                return {
                    "success": False,
                    "error": f"Calculation error: {str(e)}"
                }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


def get_open_view_declaration():
    """
    Function declaration for opening Odoo views directly.
    """
    return {
        "name": "open_view",
        "strict": True,
        "description": "Open a specific Odoo view (list, kanban, graph, pivot) for a model. Use this for navigation requests.",
        "parameters": {
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "description": "Technical model name (e.g., 'sale.order')",
                },
                "view_type": {
                    "type": "string",
                    "enum": ["list", "kanban", "graph", "pivot"],
                    "description": "View type to open.",
                },
                "domain": {
                    "anyOf": [
                        {
                            "type": "array",
                            "items": {
                                "anyOf": [
                                    {"type": "string"},
                                    {
                                        "type": "array",
                                        "items": {
                                            "anyOf": [
                                                {"type": "string"},
                                                {"type": "number"},
                                                {"type": "boolean"},
                                                {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "number"}]}}
                                            ]
                                        }
                                    }
                                ]
                            }
                        },
                        {"type": "null"}
                    ],
                    "description": "Optional filter domain. array of arrays. Pass null if all records.",
                },
                "group_by": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": "Optional grouping field. Pass null if not needed.",
                },
                "graph_mode": {
                    "anyOf": [
                        {"type": "string", "enum": ["bar", "line", "pie"]},
                        {"type": "null"}
                    ],
                    "description": "For graph views: bar, line, or pie. Pass null for default.",
                },
            },
            "required": ["model", "view_type", "domain", "group_by", "graph_mode"],
            "additionalProperties": False,
        },
    }


def _get_record_mutation_values_schema():
    return {
        "type": "object",
        "additionalProperties": {
            "anyOf": [
                {"type": "string"},
                {"type": "number"},
                {"type": "boolean"},
                {"type": "null"},
                {"type": "array", "items": {"type": "integer"}},
                {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                    },
                    "required": ["id"],
                    "additionalProperties": False,
                },
            ],
        },
    }


def get_create_record_declaration():
    """
    Function declaration for creating Odoo business record(s).
    """
    return {
        "name": "create_record",
        "strict": True,
        "description": "Create one or more Odoo business records when the user explicitly asks for it. You can create multiple linked records across models by assigning a 'ref' to one record and using '$ref' in relational fields of subsequent records.",
        "parameters": {
            "type": "object",
            "properties": {
                "records": {
                    "anyOf": [
                        {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "model": {"type": "string"},
                                    "values": _get_record_mutation_values_schema(),
                                    "ref": {"type": "string"}
                                },
                                "required": ["model", "values"],
                                "additionalProperties": False,
                            }
                        },
                        {"type": "null"}
                    ],
                    "description": "A list of records to create. Assign a 'ref' string to a record and use '$ref' in subsequent records' values to dynamically link them (e.g., 'partner_id': '$contact1').",
                },
                "model": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": "Technical Odoo model name (for example, 'crm.lead' or 'res.partner'). Required if not using the 'records' array.",
                },
                "values": {
                    "anyOf": [
                        _get_record_mutation_values_schema(),
                        {
                            "type": "array",
                            "items": _get_record_mutation_values_schema()
                        },
                        {"type": "null"}
                    ],
                    "description": "A single dictionary of field values, or a list of dictionaries for batch creation. Required if not using the 'records' array.",
                },
                "fields": {
                    "anyOf": [
                        {"type": "array", "items": {"type": "string"}},
                        {"type": "null"},
                    ],
                    "description": "Optional fields to read back from the created record(s). Pass null to return the created fields plus id.",
                },
            },
            "required": ["records", "model", "values", "fields"],
            "additionalProperties": False,
        },
    }


def get_update_record_declaration():
    """
    Function declaration for updating Odoo business record(s) by ID(s).
    """
    return {
        "name": "update_record",
        "strict": True,
        "description": "Update one or more existing Odoo business records by ID. Use search_records first when record IDs are not known.",
        "parameters": {
            "type": "object",
            "properties": {
                "records": {
                    "anyOf": [
                        {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "model": {"type": "string"},
                                    "id": {"type": "integer"},
                                    "values": _get_record_mutation_values_schema(),
                                },
                                "required": ["model", "id", "values"],
                                "additionalProperties": False,
                            }
                        },
                        {"type": "null"}
                    ],
                    "description": "A list of records to update. Allows updating multiple records across different models in a single call.",
                },
                "model": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": "Technical Odoo model name (for example, 'crm.lead' or 'res.partner'). Required if not using the 'records' array.",
                },
                "record_id": {
                    "anyOf": [
                        {"type": "integer"},
                        {"type": "array", "items": {"type": "integer"}},
                        {"type": "null"}
                    ],
                    "description": "Existing record ID or list of IDs to update. Required if not using the 'records' array.",
                },
                "values": {
                    "anyOf": [
                        _get_record_mutation_values_schema(),
                        {
                            "type": "array",
                            "items": _get_record_mutation_values_schema()
                        },
                        {"type": "null"}
                    ],
                    "description": "A dictionary of field values to apply to all record_ids, or a list of dictionaries (matching record_id list length) for batch updating different values per record. Required if not using the 'records' array.",
                },
                "fields": {
                    "anyOf": [
                        {"type": "array", "items": {"type": "string"}},
                        {"type": "null"},
                    ],
                    "description": "Optional fields to read back from the updated record(s). Pass null to return the updated fields plus id.",
                },
            },
            "required": ["records", "model", "record_id", "values", "fields"],
            "additionalProperties": False,
        },
    }


def get_archive_record_declaration():
    """
    Function declaration for archiving Odoo business record(s) by ID(s).
    """
    return {
        "name": "archive_record",
        "strict": True,
        "description": "Archive one or more existing Odoo business records by setting active to false. This only works on models that have an active field.",
        "parameters": {
            "type": "object",
            "properties": {
                "records": {
                    "anyOf": [
                        {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "model": {"type": "string"},
                                    "id": {"type": "integer"},
                                },
                                "required": ["model", "id"],
                                "additionalProperties": False,
                            }
                        },
                        {"type": "null"}
                    ],
                    "description": "A list of records to archive. Allows processing multiple records across different models in a single call.",
                },
                "model": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": "Technical Odoo model name (for example, 'crm.lead' or 'res.partner'). Required if not using the 'records' array.",
                },
                "record_id": {
                    "anyOf": [
                        {"type": "integer"},
                        {"type": "array", "items": {"type": "integer"}},
                        {"type": "null"}
                    ],
                    "description": "Existing record ID or list of IDs to archive. Required if not using the 'records' array.",
                },
                "fields": {
                    "anyOf": [
                        {"type": "array", "items": {"type": "string"}},
                        {"type": "null"},
                    ],
                    "description": "Optional fields to read back from the archived record(s). Pass null to return id and active.",
                },
            },
            "required": ["records", "model", "record_id", "fields"],
            "additionalProperties": False,
        },
    }


def get_delete_record_declaration():
    """
    Function declaration for deleting Odoo business record(s) by ID(s).
    """
    return {
        "name": "delete_record",
        "strict": True,
        "description": "Delete one or more existing Odoo business records by ID. This permanently removes the records when the current user has unlink permission and no business constraint blocks deletion.",
        "parameters": {
            "type": "object",
            "properties": {
                "records": {
                    "anyOf": [
                        {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "model": {"type": "string"},
                                    "id": {"type": "integer"},
                                },
                                "required": ["model", "id"],
                                "additionalProperties": False,
                            }
                        },
                        {"type": "null"}
                    ],
                    "description": "A list of records to delete. Allows processing multiple records across different models in a single call.",
                },
                "model": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": "Technical Odoo model name (for example, 'crm.lead' or 'res.partner'). Required if not using the 'records' array.",
                },
                "record_id": {
                    "anyOf": [
                        {"type": "integer"},
                        {"type": "array", "items": {"type": "integer"}},
                        {"type": "null"}
                    ],
                    "description": "Existing record ID or list of IDs to delete. Required if not using the 'records' array.",
                },
                "fields": {
                    "anyOf": [
                        {"type": "array", "items": {"type": "string"}},
                        {"type": "null"},
                    ],
                    "description": "Optional fields to read before deletion and include in the response. Pass null to return only the deleted record ids.",
                },
            },
            "required": ["records", "model", "record_id", "fields"],
            "additionalProperties": False,
        },
    }


_SUPPORTED_MUTATION_FIELD_TYPES = {
    "boolean",
    "char",
    "date",
    "datetime",
    "float",
    "html",
    "integer",
    "many2many",
    "many2one",
    "monetary",
    "selection",
    "text",
}


def _normalize_record_values_payload(values):
    if isinstance(values, str):
        try:
            values = json.loads(values)
        except json.JSONDecodeError:
            raise ValueError("Values must be a dictionary or valid JSON object.")
    if not isinstance(values, dict):
        raise ValueError("Values must be a dictionary.")
    if not values:
        raise ValueError("Provide at least one field value.")
    return values


def _normalize_return_fields(fields):
    if fields in (None, False):
        return None
    if isinstance(fields, str):
        fields = [fields]
    if not isinstance(fields, list):
        raise ValueError("fields must be a list of field names or null.")

    normalized_fields = []
    for field_name in fields:
        if not isinstance(field_name, str) or not field_name.strip():
            raise ValueError("fields must only contain non-empty field names.")
        clean_name = field_name.strip()
        if clean_name not in normalized_fields:
            normalized_fields.append(clean_name)
    return normalized_fields


def _normalize_relation_id(value, field_name):
    if value in (None, False):
        return False
    if isinstance(value, dict):
        value = value.get("id")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Field '%s' expects an integer ID or null." % field_name)
    return value


def _normalize_relation_id_list(value, field_name):
    if value in (None, False):
        return []
    if not isinstance(value, list):
        raise ValueError("Field '%s' expects a list of integer IDs or null." % field_name)

    normalized_ids = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ValueError("Field '%s' expects only integer IDs." % field_name)
        if item not in normalized_ids:
            normalized_ids.append(item)
    return normalized_ids


def _normalize_write_field_value(env, Model, field_name, raw_value, field_info):
    field = Model._fields.get(field_name)
    if not field:
        raise ValueError("Field '%s' does not exist on model '%s'." % (field_name, Model._name))
    if field_name == "id":
        raise ValueError("Field 'id' cannot be written.")
    if field.readonly:
        raise ValueError("Field '%s' on model '%s' is read-only." % (field_name, Model._name))
    if field.type not in _SUPPORTED_MUTATION_FIELD_TYPES:
        raise ValueError(
            "Field '%s' on model '%s' uses unsupported type '%s' for AI record mutations."
            % (field_name, Model._name, field.type)
        )

    if field.type in {"char", "text", "html", "date", "datetime"}:
        if raw_value in (None, False):
            return False
        if not isinstance(raw_value, str):
            raise ValueError("Field '%s' expects a string value or null." % field_name)
        return raw_value

    if field.type == "selection":
        if raw_value in (None, False):
            return False
        if not isinstance(raw_value, str):
            raise ValueError("Field '%s' expects a selection key string or null." % field_name)
        selection_values = field_info.get("selection") or []
        valid_keys = {key for key, _label in selection_values if isinstance(key, str)}
        if valid_keys and raw_value not in valid_keys:
            raise ValueError(
                "Field '%s' expects one of: %s."
                % (field_name, ", ".join(sorted(valid_keys)))
            )
        return raw_value

    if field.type == "boolean":
        if not isinstance(raw_value, bool):
            raise ValueError("Field '%s' expects a boolean value." % field_name)
        return raw_value

    if field.type == "integer":
        if raw_value in (None, False):
            return False
        if isinstance(raw_value, bool) or not isinstance(raw_value, int):
            raise ValueError("Field '%s' expects an integer value or null." % field_name)
        return raw_value

    if field.type in {"float", "monetary"}:
        if raw_value in (None, False):
            return False
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raise ValueError("Field '%s' expects a number value or null." % field_name)
        return raw_value

    if field.type == "many2one":
        relation_id = _normalize_relation_id(raw_value, field_name)
        if not relation_id:
            return False
        related_record = env[field.comodel_name].browse(relation_id).exists()
        if not related_record:
            raise ValueError(
                "Related record %s was not found for field '%s'." % (relation_id, field_name)
            )
        related_record.check_access("read")
        return relation_id

    if field.type == "many2many":
        relation_ids = _normalize_relation_id_list(raw_value, field_name)
        if relation_ids:
            related_records = env[field.comodel_name].browse(relation_ids).exists()
            if len(related_records) != len(relation_ids):
                raise ValueError("One or more related records were not found for field '%s'." % field_name)
            related_records.check_access("read")
        return [(6, 0, relation_ids)]

    raise ValueError("Field '%s' is not supported for AI record mutations." % field_name)


def _normalize_record_write_values(env, model, values, extra_field_names=None):
    normalized_values = _normalize_record_values_payload(values)
    blocked_error = _validate_mcp_model_allowed(model)
    if blocked_error:
        raise ValueError(blocked_error)
    try:
        Model = env[model]
    except KeyError:
        raise ValueError("Model '%s' not found." % model)

    field_names = list(normalized_values.keys())
    if extra_field_names:
        for field_name in extra_field_names:
            if field_name not in field_names:
                field_names.append(field_name)

    fields_info = Model.fields_get(field_names)
    prepared_values = {}
    for field_name, raw_value in normalized_values.items():
        prepared_values[field_name] = _normalize_write_field_value(
            env,
            Model,
            field_name,
            raw_value,
            fields_info.get(field_name) or {},
        )
    return Model, prepared_values, fields_info


def _build_mutation_read_fields(Model, values, fields, default_fields=None):
    requested_fields = _normalize_return_fields(fields)
    if requested_fields is not None:
        return requested_fields

    candidate_fields = ["id"]
    for field_name in (default_fields or list(values.keys())):
        if field_name != "id" and field_name in Model._fields and field_name not in candidate_fields:
            candidate_fields.append(field_name)
    return candidate_fields


def _read_single_record_payload(record, field_names):
    if field_names == ["id"]:
        return {"id": record.id}
    payload_list = record.read(field_names)
    if not payload_list:
        return {"id": record.id}
    payload = payload_list[0]
    payload.setdefault("id", record.id)
    return payload


def create_record(env, model=None, values=None, fields=None, records=None):
    """
    Create one or more Odoo business records, or multiple records across different models using `records`.
    """
    try:
        if records:
            if not isinstance(records, list):
                return {"success": False, "error": "'records' must be a list of objects.", "model": model or "mixed"}
            
            results = []
            ref_map = {}
            for item in records:
                if not isinstance(item, dict) or "model" not in item or "values" not in item:
                    return {"success": False, "error": "Each item in 'records' must contain 'model' and 'values'.", "model": model or "mixed"}
                
                m_name = item["model"]
                m_vals = item["values"]
                ref_id = item.get("ref")
                
                if not isinstance(m_vals, dict):
                    return {"success": False, "error": f"Values must be a dictionary for {m_name}.", "model": m_name}
                
                def _resolve_refs(val):
                    if isinstance(val, dict):
                        return {k: _resolve_refs(v) for k, v in val.items()}
                    elif isinstance(val, list):
                        return [_resolve_refs(v) for v in val]
                    elif isinstance(val, str) and val.startswith("$") and val[1:] in ref_map:
                        return ref_map[val[1:]]
                    return val
                
                resolved_vals = _resolve_refs(m_vals)

                try:
                    M, prepared_values, _ = _normalize_record_write_values(env, m_name, resolved_vals)
                    M.browse().check_access("create")
                    rec = M.create(prepared_values)
                    
                    if ref_id and isinstance(ref_id, str):
                        ref_map[ref_id] = rec.id
                        
                    read_fields = _build_mutation_read_fields(M, prepared_values, fields)
                    try:
                        rec.check_access("read")
                        rec_payload = _read_single_record_payload(rec, read_fields)
                    except AccessError:
                        rec_payload = {"id": rec.id}
                        
                    if ref_id:
                        rec_payload["ref"] = ref_id
                        
                    results.append({"model": m_name, "record": rec_payload})
                except AccessError:
                    return {"success": False, "error": f"Access denied. You don't have permission to create {m_name}.", "model": m_name}
                except Exception as error:
                    return {"success": False, "error": str(error), "model": m_name}
            
            return {
                "success": True,
                "model": "mixed",
                "records": results,
                "summary": f"Successfully created {len(records)} records across potentially multiple models."
            }

        if not model or not values:
            return {"success": False, "error": "Either 'records' array OR 'model' and 'values' must be provided.", "model": model or "unknown"}

        if isinstance(values, list):
            if not values:
                return {"success": False, "error": "Values list cannot be empty.", "model": model}

            prepared_values_list = []
            Model = None
            for val in values:
                if not isinstance(val, dict):
                    return {"success": False, "error": "Each item in values must be a dictionary.", "model": model}
                M, prepared, _ = _normalize_record_write_values(env, model, val)
                if Model is None:
                    Model = M
                prepared_values_list.append(prepared)

            Model.browse().check_access("create")
            records = Model.create(prepared_values_list)

            records_payloads = []
            read_fields = _build_mutation_read_fields(Model, prepared_values_list[0] if prepared_values_list else {}, fields)
            try:
                records.check_access("read")
                for rec in records:
                    records_payloads.append(_read_single_record_payload(rec, read_fields))
                note = None
            except AccessError:
                records_payloads = [{"id": r.id} for r in records]
                note = "Records were created, but the current user cannot read the requested fields."

            result = {
                "success": True,
                "model": model,
                "record_ids": records.ids,
                "records": records_payloads,
                "summary": "Created %s records for model %s." % (len(records), model),
            }
            if len(records) == 1:
                result["record_id"] = records.id
                result["record"] = records_payloads[0]
            if note:
                result["note"] = note
            return result

        # Single record create
        Model, prepared_values, _fields_info = _normalize_record_write_values(env, model, values)
        Model.browse().check_access("create")
        record = Model.create(prepared_values)
        read_fields = _build_mutation_read_fields(Model, prepared_values, fields)
        try:
            record.check_access("read")
            record_payload = _read_single_record_payload(record, read_fields)
            note = None
        except AccessError:
            record_payload = {"id": record.id}
            note = "Record was created, but the current user cannot read the requested fields."

        result = {
            "success": True,
            "model": model,
            "record_id": record.id,
            "record": record_payload,
            "summary": "Created %s #%s." % (model, record.id),
        }
        if note:
            result["note"] = note
        return result
    except AccessError:
        return {"success": False, "error": "Access denied. You don't have permission to create this record.", "model": model}
    except Exception as error:
        return {"success": False, "error": str(error), "model": model}


def update_record(env, model=None, record_id=None, values=None, fields=None, records=None):
    """
    Update one or more Odoo business records by ID(s), or multiple records across different models using `records`.
    """
    try:
        if records:
            if not isinstance(records, list):
                return {"success": False, "error": "'records' must be a list of objects.", "model": model or "mixed"}
            
            results = []
            for item in records:
                if not isinstance(item, dict) or "model" not in item or "id" not in item or "values" not in item:
                    return {"success": False, "error": "Each item in 'records' must contain 'model', 'id', and 'values'.", "model": model or "mixed"}
                
                m_name = item["model"]
                m_id = item["id"]
                m_vals = item["values"]
                
                if not isinstance(m_id, int) or isinstance(m_id, bool):
                    return {"success": False, "error": f"Invalid ID '{m_id}' for model {m_name}.", "model": m_name}
                if not isinstance(m_vals, dict):
                    return {"success": False, "error": f"Values must be a dictionary for {m_name} #{m_id}.", "model": m_name}
                
                try:
                    Model = env[m_name]
                    rec = Model.browse(m_id).exists()
                    if not rec:
                        return {"success": False, "error": f"Record {m_name} #{m_id} not found.", "model": m_name}
                    
                    rec.check_access("write")
                    M, prepared_values, _ = _normalize_record_write_values(env, m_name, m_vals)
                    rec.write(prepared_values)
                    
                    read_fields = _build_mutation_read_fields(M, prepared_values, fields)
                    try:
                        rec.check_access("read")
                        rec_payload = _read_single_record_payload(rec, read_fields)
                    except AccessError:
                        rec_payload = {"id": rec.id}
                        
                    results.append({"model": m_name, "record": rec_payload})
                except AccessError:
                    return {"success": False, "error": f"Access denied. You don't have permission to update {m_name} #{m_id}.", "model": m_name}
                except Exception as error:
                    return {"success": False, "error": str(error), "model": m_name}
            
            return {
                "success": True,
                "model": "mixed",
                "records": results,
                "summary": f"Successfully updated {len(records)} records across potentially multiple models."
            }

        if not model or not record_id or not values:
            return {"success": False, "error": "Either 'records' array OR 'model', 'record_id', 'values' must be provided.", "model": model or "unknown"}

        if isinstance(values, list):
            if not isinstance(record_id, list):
                return {"success": False, "error": "When values is a list of dictionaries, record_id must be a list of integers of the same length.", "model": model}
            if len(values) != len(record_id):
                return {"success": False, "error": "The length of values (%s) and record_id (%s) lists must match." % (len(values), len(record_id)), "model": model}
            if not values:
                return {"success": False, "error": "Values list cannot be empty.", "model": model}

            ids = [rid for rid in record_id if isinstance(rid, int) and not isinstance(rid, bool)]
            if len(ids) != len(record_id):
                return {"success": False, "error": "All record_ids must be valid integer IDs.", "model": model}

            Model = env[model]
            records = Model.browse(ids).exists()
            if not records or len(records) != len(ids):
                missing_ids = set(ids) - set(records.ids)
                return {"success": False, "error": "Some records were not found: %s" % list(missing_ids), "model": model}
            
            records.check_access("write")

            records_payloads = []
            read_fields = None
            for rid, val in zip(ids, values):
                if not isinstance(val, dict):
                    return {"success": False, "error": "Each item in values must be a dictionary.", "model": model}
                rec = Model.browse(rid)
                M, prepared_values, _ = _normalize_record_write_values(env, model, val)
                rec.write(prepared_values)
                if read_fields is None:
                    read_fields = _build_mutation_read_fields(M, prepared_values, fields)

            try:
                records.check_access("read")
                for rec in records:
                    records_payloads.append(_read_single_record_payload(rec, read_fields))
                note = None
            except AccessError:
                records_payloads = [{"id": r.id} for r in records]
                note = "Records were updated, but the current user cannot read the requested fields."

            result = {
                "success": True,
                "model": model,
                "record_ids": records.ids,
                "records": records_payloads,
                "summary": "Updated %s records with unique values for model %s." % (len(records), model),
            }
            if len(ids) == 1:
                result["record_id"] = records.id
                result["record"] = records_payloads[0]
                result["summary"] = "Updated %s #%s." % (model, records.id)
            if note:
                result["note"] = note
            return result

        if isinstance(record_id, list):
            ids = [rid for rid in record_id if isinstance(rid, int) and not isinstance(rid, bool)]
            if not ids:
                return {"success": False, "error": "record_id list must contain at least one valid integer ID.", "model": model}
        elif isinstance(record_id, int) and not isinstance(record_id, bool):
            ids = [record_id]
        else:
            return {"success": False, "error": "record_id must be an integer or a list of integers.", "model": model}

        Model, prepared_values, _fields_info = _normalize_record_write_values(env, model, values)
        records = Model.browse(ids).exists()
        if not records:
            return {"success": False, "error": "No records found.", "model": model}
        if len(records) != len(ids):
            missing_ids = set(ids) - set(records.ids)
            return {"success": False, "error": "Some records were not found: %s" % list(missing_ids), "model": model}

        records.check_access("write")
        records.write(prepared_values)

        read_fields = _build_mutation_read_fields(Model, prepared_values, fields)
        records_payloads = []
        try:
            records.check_access("read")
            for rec in records:
                records_payloads.append(_read_single_record_payload(rec, read_fields))
            note = None
        except AccessError:
            records_payloads = [{"id": r.id} for r in records]
            note = "Records were updated, but the current user cannot read the requested fields."

        result = {
            "success": True,
            "model": model,
            "record_ids": records.ids,
            "records": records_payloads,
            "summary": "Updated %s records for model %s." % (len(records), model),
        }
        if len(ids) == 1:
            result["record_id"] = records.id
            result["record"] = records_payloads[0]
            result["summary"] = "Updated %s #%s." % (model, records.id)

        if note:
            result["note"] = note
        return result
    except AccessError:
        return {"success": False, "error": "Access denied. You don't have permission to update this record.", "model": model}
    except Exception as error:
        return {"success": False, "error": str(error), "model": model}


def archive_record(env, model=None, record_id=None, fields=None, records=None):
    """
    Archive one or more Odoo business records by setting active to False, or multiple across models using `records`.
    """
    try:
        if records:
            if not isinstance(records, list):
                return {"success": False, "error": "'records' must be a list of objects.", "model": model or "mixed"}
            
            results = []
            for item in records:
                if not isinstance(item, dict) or "model" not in item or "id" not in item:
                    return {"success": False, "error": "Each item in 'records' must contain 'model' and 'id'.", "model": model or "mixed"}
                
                m_name = item["model"]
                m_id = item["id"]
                
                if not isinstance(m_id, int) or isinstance(m_id, bool):
                    return {"success": False, "error": f"Invalid ID '{m_id}' for model {m_name}.", "model": m_name}
                
                try:
                    Model = env[m_name]
                    active_field = Model._fields.get("active")
                    if not active_field or active_field.type != "boolean":
                        return {"success": False, "error": f"Model '{m_name}' does not support archiving.", "model": m_name}
                    
                    rec = Model.browse(m_id).exists()
                    if not rec:
                        return {"success": False, "error": f"Record {m_name} #{m_id} not found.", "model": m_name}
                    
                    rec.check_access("write")
                    rec.write({"active": False})
                    
                    read_fields = _build_mutation_read_fields(Model, {"active": False}, fields, default_fields=["active"])
                    try:
                        rec.check_access("read")
                        rec_payload = _read_single_record_payload(rec, read_fields)
                    except AccessError:
                        rec_payload = {"id": rec.id}
                        
                    results.append({"model": m_name, "record": rec_payload})
                except AccessError:
                    return {"success": False, "error": f"Access denied. You don't have permission to update {m_name} #{m_id}.", "model": m_name}
                except Exception as error:
                    return {"success": False, "error": str(error), "model": m_name}
            
            return {
                "success": True,
                "model": "mixed",
                "records": results,
                "summary": f"Successfully archived {len(records)} records across potentially multiple models."
            }

        if not model or not record_id:
            return {"success": False, "error": "Either 'records' array OR 'model' and 'record_id' must be provided.", "model": model or "unknown"}

        if isinstance(record_id, list):
            ids = [rid for rid in record_id if isinstance(rid, int) and not isinstance(rid, bool)]
            if not ids:
                return {"success": False, "error": "record_id list must contain at least one valid integer ID.", "model": model}
        elif isinstance(record_id, int) and not isinstance(record_id, bool):
            ids = [record_id]
        else:
            return {"success": False, "error": "record_id must be an integer or a list of integers.", "model": model}

        blocked_error = _validate_mcp_model_allowed(model)
        if blocked_error:
            return {"success": False, "error": blocked_error, "model": model}

        try:
            Model = env[model]
        except KeyError:
            return {"success": False, "error": "Model '%s' not found." % model, "model": model}

        active_field = Model._fields.get("active")
        if not active_field or active_field.type != "boolean":
            return {
                "success": False,
                "error": "Model '%s' does not support archiving because it has no boolean active field." % model,
                "model": model,
            }

        records = Model.browse(ids).exists()
        if not records:
            return {"success": False, "error": "No records found.", "model": model}
        if len(records) != len(ids):
            missing_ids = set(ids) - set(records.ids)
            return {"success": False, "error": "Some records were not found: %s" % list(missing_ids), "model": model}

        records.check_access("write")
        any_changed = any(records.mapped("active"))
        records.write({"active": False})

        read_fields = _build_mutation_read_fields(Model, {"active": False}, fields, default_fields=["active"])
        records_payloads = []
        try:
            records.check_access("read")
            for rec in records:
                records_payloads.append(_read_single_record_payload(rec, read_fields))
            note = None
        except AccessError:
            records_payloads = [{"id": r.id, "active": False} for r in records]
            note = "Records were archived, but the current user cannot read the requested fields."

        result = {
            "success": True,
            "model": model,
            "record_ids": records.ids,
            "records": records_payloads,
            "archived": True,
            "changed": any_changed,
            "summary": "Archived %s records for model %s." % (len(records), model),
        }
        if len(ids) == 1:
            result["record_id"] = records.id
            result["record"] = records_payloads[0]
            result["summary"] = "Archived %s #%s." % (model, records.id)

        if note:
            result["note"] = note
        return result
    except AccessError:
        return {"success": False, "error": "Access denied. You don't have permission to archive this record.", "model": model}
    except Exception as error:
        return {"success": False, "error": str(error), "model": model}


def delete_record(env, model=None, record_id=None, fields=None, records=None):
    """
    Delete one or more Odoo business records by ID, or multiple across models using `records`.
    """
    try:
        if records:
            if not isinstance(records, list):
                return {"success": False, "error": "'records' must be a list of objects.", "model": model or "mixed"}
            
            results = []
            for item in records:
                if not isinstance(item, dict) or "model" not in item or "id" not in item:
                    return {"success": False, "error": "Each item in 'records' must contain 'model' and 'id'.", "model": model or "mixed"}
                
                m_name = item["model"]
                m_id = item["id"]
                
                if not isinstance(m_id, int) or isinstance(m_id, bool):
                    return {"success": False, "error": f"Invalid ID '{m_id}' for model {m_name}.", "model": m_name}
                
                try:
                    Model = env[m_name]
                    rec = Model.browse(m_id).exists()
                    if not rec:
                        return {"success": False, "error": f"Record {m_name} #{m_id} not found.", "model": m_name}
                    
                    rec.check_access("unlink")
                    
                    read_fields = _build_mutation_read_fields(Model, {}, fields)
                    try:
                        rec.check_access("read")
                        rec_payload = _read_single_record_payload(rec, read_fields)
                    except AccessError:
                        rec_payload = {"id": rec.id}
                        
                    rec.unlink()
                    results.append({"model": m_name, "record": rec_payload})
                except AccessError:
                    return {"success": False, "error": f"Access denied. You don't have permission to delete {m_name} #{m_id}.", "model": m_name}
                except Exception as error:
                    return {"success": False, "error": str(error), "model": m_name}
            
            return {
                "success": True,
                "model": "mixed",
                "records": results,
                "summary": f"Successfully deleted {len(records)} records across potentially multiple models."
            }

        if not model or not record_id:
            return {"success": False, "error": "Either 'records' array OR 'model' and 'record_id' must be provided.", "model": model or "unknown"}

        if isinstance(record_id, list):
            ids = [rid for rid in record_id if isinstance(rid, int) and not isinstance(rid, bool)]
            if not ids:
                return {"success": False, "error": "record_id list must contain at least one valid integer ID.", "model": model}
        elif isinstance(record_id, int) and not isinstance(record_id, bool):
            ids = [record_id]
        else:
            return {"success": False, "error": "record_id must be an integer or a list of integers.", "model": model}

        blocked_error = _validate_mcp_model_allowed(model)
        if blocked_error:
            return {"success": False, "error": blocked_error, "model": model}

        try:
            Model = env[model]
        except KeyError:
            return {"success": False, "error": "Model '%s' not found." % model, "model": model}

        records = Model.browse(ids).exists()
        if not records:
            return {"success": False, "error": "No records found.", "model": model}
        if len(records) != len(ids):
            missing_ids = set(ids) - set(records.ids)
            return {"success": False, "error": "Some records were not found: %s" % list(missing_ids), "model": model}

        read_fields = _build_mutation_read_fields(Model, {}, fields, default_fields=["id"])
        records_payloads = []
        try:
            records.check_access("read")
            for rec in records:
                records_payloads.append(_read_single_record_payload(rec, read_fields))
            note = None
        except AccessError:
            records_payloads = [{"id": r.id} for r in records]
            note = "Records were deleted, but the current user could not read the requested fields before deletion."

        records.check_access("unlink")
        records.unlink()

        result = {
            "success": True,
            "model": model,
            "record_ids": ids,
            "records": records_payloads,
            "deleted": True,
            "summary": "Deleted %s records for model %s." % (len(ids), model),
        }
        if len(ids) == 1:
            result["record_id"] = ids[0]
            result["record"] = records_payloads[0]
            result["summary"] = "Deleted %s #%s." % (model, ids[0])

        if note:
            result["note"] = note
        return result
    except AccessError:
        return {"success": False, "error": "Access denied. You don't have permission to delete this record.", "model": model}
    except Exception as error:
        return {"success": False, "error": str(error), "model": model}


def open_view(env, model, view_type, domain=None, group_by=None, graph_mode=None):
    """
    Redirect user to a specific Odoo view.
    """
    try:
        try:
            Model = env[model]
        except KeyError:
            return {"success": False, "error": f"Model '{model}' not found"}

        if not Model.browse().has_access('read'):
            return {"success": False, "error": "Access denied"}

        if domain is None:
            domain = []
        elif isinstance(domain, str):
            try:
                domain = json.loads(domain)
            except json.JSONDecodeError:
                return {
                    "success": False,
                    "error": "Invalid domain format. Use Python list or valid JSON string.",
                }

        # DYNAMIC FIX: Intercept string searches on relational fields
        domain, corrections = _validate_and_fix_domain(env, model, domain)
        if corrections:
            _logger.info(f"🛠️ AI View Domain Auto-Fix: {', '.join(corrections)}")


        total_count = Model.search_count(domain)

        action_data = {
            'model': model,
            'view_type': view_type,
            'domain': domain,
            'total_count': total_count,
        }
        
        if group_by:
            action_data['group_by'] = group_by
        if graph_mode:
            action_data['graph_mode'] = graph_mode

        return {
            "success": True,
            "message": f"Opening {view_type} view for {model}",
            "action_data": action_data
        }

    except Exception as e:
        return {"success": False, "error": str(e)}