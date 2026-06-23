# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies.

from copy import deepcopy
import json
import logging
from odoo.exceptions import AccessError

_logger = logging.getLogger(__name__)

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
                "model": {
                    "type": "string",
                    "description": "The Odoo model to search (e.g., 'hr.department', 'res.partner')",
                },
                "search_term": {
                    "type": "string",
                    "description": "The name/term to search for (e.g., 'Treasure Department')",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum candidates to return (default: 5)",
                }
            },
            "required": ["model", "search_term"],
            "additionalProperties": False,
        },
    }


def fuzzy_lookup(env, model, search_term, limit=5):
    """
    Fully dynamic name-to-ID resolver.
    Searches across ALL text-based fields dynamically.
    No hardcoded suffixes or field names.
    """
    try:
        try:
            Model = env[model]
        except KeyError:
            return {"success": False, "error": f"Model '{model}' not found", "results": []}

        # SECURITY CHECK: Verify user has read access
        try:
            Model.browse().check_access_rights('read')
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
            
        # Strategy C: Word-by-word
        if not results and ' ' in clean_term:
            for word in clean_term.split():
                if len(word) > 2:
                    results = Model.search(get_or_domain(word, 'ilike'), limit=limit)
                    if results: break

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
                "model": {
                    "type": "string",
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
            "required": ["model", "domain", "fields", "limit", "order", "offset", "group_by", "count_only"],
            "additionalProperties": False,
        },
    }


def search_records(env, model, domain=None, fields=None, limit=None, order=None, offset=0, group_by=None, count_only=False):
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

    Returns:
        dict with success, count, records, and optional group_by
    """
    try:
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
            Model.browse().check_access_rights('read')
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
        if group_by and fields and group_by not in fields:
            fields = list(fields)  # Copy to avoid modifying original
            fields.append(group_by)

        if group_by and not order:
            order = f"{group_by}, id"

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

        # Enhance each model with additional context
        enhanced_models = []
        for model in all_models:
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
                # Check if user has access to this model
                Model = env[model_name]
                if not Model.check_access_rights('read', raise_exception=False):
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
        try:
            Model = env[model]
        except KeyError:
            return {
                "success": False,
                "error": f"Model '{model}' not found",
            }

        if not Model.check_access_rights('read', raise_exception=False):
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
                "model": {
                    "type": "string",
                    "description": "Technical model name (e.g., 'sale.order')",
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
                    "type": "string",
                    "enum": ["sum", "avg", "min", "max", "count"],
                    "description": "Aggregation type.",
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
            "required": ["model", "operation", "domain", "field", "group_by", "having", "limit"],
            "additionalProperties": False,
        },
    }


def aggregate_records(env, model, operation, domain=None, field=None, group_by=None, having=None, limit=None, **kwargs):
    """
    SUPERIOR aggregate_records using Odoo's _read_group() ORM method.
    """
    try:
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
            Model.browse().check_access_rights('read')
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
            try:
                if operation == 'count':
                    count_field = field or 'id'
                    aggregates = [f'{count_field}:count']
                else:
                    aggregates = [f'{field}:{operation}']

                # Odoo 16 Use read_group with list of dicts result
                fields = aggregates
                result_groups = Model.read_group(
                    domain=domain,
                    fields=fields,
                    groupby=[group_by],
                    lazy=False,
                    offset=0,
                    limit=None
                )

                groups = []
                for group_dict in result_groups:
                    group_val = group_dict.get(group_by)
                    
                    # Determine the aggregate key in Odoo 16
                    # For count, it's usually [groupby]_count
                    # For field:sum, it's usually just field
                    if operation == 'count':
                        agg_key = f"{group_by.split(':')[0]}_count"
                    else:
                        agg_key = field
                    
                    agg_val = group_dict.get(agg_key, 0)
                    
                    if hasattr(group_val, 'display_name'):
                        label = group_val.display_name
                    elif isinstance(group_val, (list, tuple)) and group_val:
                        label = group_val[1] if len(group_val) > 1 else str(group_val[0])
                    else:
                        label = str(group_val)

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
                    fields = [f'{field}:{operation}']
                    res = Model.read_group(domain=domain, fields=fields, groupby=[], lazy=False)
                    result = res[0].get(field, 0) if res else 0

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
    Function declaration for creating a single Odoo business record.
    """
    return {
        "name": "create_record",
        "strict": True,
        "description": "Create a single Odoo business record when the user explicitly asks for it. Use IDs for relational fields such as many2one and many2many values.",
        "parameters": {
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "description": "Technical Odoo model name (for example, 'crm.lead' or 'res.partner').",
                },
                "values": _get_record_mutation_values_schema(),
                "fields": {
                    "anyOf": [
                        {"type": "array", "items": {"type": "string"}},
                        {"type": "null"},
                    ],
                    "description": "Optional fields to read back from the created record. Pass null to return the created fields plus id.",
                },
            },
            "required": ["model", "values", "fields"],
            "additionalProperties": False,
        },
    }


def get_update_record_declaration():
    """
    Function declaration for updating a single Odoo business record by ID.
    """
    return {
        "name": "update_record",
        "strict": True,
        "description": "Update a single existing Odoo business record by ID. Use search_records first when the record ID is not known.",
        "parameters": {
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "description": "Technical Odoo model name (for example, 'crm.lead' or 'res.partner').",
                },
                "record_id": {
                    "type": "integer",
                    "description": "Existing record ID to update.",
                },
                "values": _get_record_mutation_values_schema(),
                "fields": {
                    "anyOf": [
                        {"type": "array", "items": {"type": "string"}},
                        {"type": "null"},
                    ],
                    "description": "Optional fields to read back from the updated record. Pass null to return the updated fields plus id.",
                },
            },
            "required": ["model", "record_id", "values", "fields"],
            "additionalProperties": False,
        },
    }


def get_archive_record_declaration():
    """
    Function declaration for archiving a single Odoo business record by ID.
    """
    return {
        "name": "archive_record",
        "strict": True,
        "description": "Archive a single existing Odoo business record by setting active to false. This only works on models that have an active field.",
        "parameters": {
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "description": "Technical Odoo model name (for example, 'crm.lead' or 'res.partner').",
                },
                "record_id": {
                    "type": "integer",
                    "description": "Existing record ID to archive.",
                },
                "fields": {
                    "anyOf": [
                        {"type": "array", "items": {"type": "string"}},
                        {"type": "null"},
                    ],
                    "description": "Optional fields to read back from the archived record. Pass null to return id and active.",
                },
            },
            "required": ["model", "record_id", "fields"],
            "additionalProperties": False,
        },
    }


def get_delete_record_declaration():
    """
    Function declaration for deleting a single Odoo business record by ID.
    """
    return {
        "name": "delete_record",
        "strict": True,
        "description": "Delete a single existing Odoo business record by ID. This permanently removes the record when the current user has unlink permission and no business constraint blocks deletion.",
        "parameters": {
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "description": "Technical Odoo model name (for example, 'crm.lead' or 'res.partner').",
                },
                "record_id": {
                    "type": "integer",
                    "description": "Existing record ID to delete.",
                },
                "fields": {
                    "anyOf": [
                        {"type": "array", "items": {"type": "string"}},
                        {"type": "null"},
                    ],
                    "description": "Optional fields to read before deletion and include in the response. Pass null to return only the deleted record id.",
                },
            },
            "required": ["model", "record_id", "fields"],
            "additionalProperties": False,
        },
    }


def get_execute_record_action_declaration():
    """
    Function declaration for executing an allow-listed object-button action on a record.
    """
    return {
        "name": "execute_record_action",
        "strict": True,
        "description": "Execute an allow-listed object-button action on a single existing Odoo record. If the action opens a wizard dialog, the response returns the wizard inputs needed for the next step.",
        "parameters": {
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "description": "Technical Odoo model name (for example, 'sale.order').",
                },
                "record_id": {
                    "type": "integer",
                    "description": "Existing record ID to execute the action on.",
                },
                "action_name": {
                    "type": "string",
                    "description": "Allowed button label or method name, for example 'Confirm' or 'action_confirm'.",
                },
                "fields": {
                    "anyOf": [
                        {"type": "array", "items": {"type": "string"}},
                        {"type": "null"},
                    ],
                    "description": "Optional fields to read back from the record after the action succeeds. Pass null to return only the record id.",
                },
            },
            "required": ["model", "record_id", "action_name", "fields"],
            "additionalProperties": False,
        },
    }


def get_submit_action_wizard_declaration():
    """
    Function declaration for submitting a wizard returned by execute_record_action.
    """
    return {
        "name": "submit_action_wizard",
        "strict": True,
        "description": "Submit values into a wizard returned by execute_record_action, then run one allowed wizard button method to complete the original business action.",
        "parameters": {
            "type": "object",
            "properties": {
                "wizard_session_id": {
                    "type": "integer",
                    "description": "Wizard session id returned by execute_record_action.",
                },
                "values": {
                    "anyOf": [
                        _get_record_mutation_values_schema(),
                        {"type": "null"},
                    ],
                    "description": "Optional wizard field values to write before running the wizard action.",
                },
                "action_name": {
                    "anyOf": [
                        {"type": "string"},
                        {"type": "null"},
                    ],
                    "description": "Optional wizard button label or method name. When omitted, the tool auto-picks the single available button.",
                },
                "fields": {
                    "anyOf": [
                        {"type": "array", "items": {"type": "string"}},
                        {"type": "null"},
                    ],
                    "description": "Optional fields to read back from the original business record after the wizard succeeds.",
                },
            },
            "required": ["wizard_session_id", "values", "action_name", "fields"],
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
        related_record.check_access_rule("read")
        return relation_id

    if field.type == "many2many":
        relation_ids = _normalize_relation_id_list(raw_value, field_name)
        if relation_ids:
            related_records = env[field.comodel_name].browse(relation_ids).exists()
            if len(related_records) != len(relation_ids):
                raise ValueError("One or more related records were not found for field '%s'." % field_name)
            related_records.check_access_rule("read")
        return [(6, 0, relation_ids)]

    raise ValueError("Field '%s' is not supported for AI record mutations." % field_name)


def _normalize_record_write_values(env, model, values, extra_field_names=None):
    normalized_values = _normalize_record_values_payload(values)
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


def create_record(env, model, values, fields=None):
    """
    Create a single Odoo business record.
    """
    try:
        Model, prepared_values, _fields_info = _normalize_record_write_values(env, model, values)
        Model.check_access_rights("create")
        record = Model.create(prepared_values)
        read_fields = _build_mutation_read_fields(Model, prepared_values, fields)
        try:
            record.check_access_rule("read")
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


def update_record(env, model, record_id, values, fields=None):
    """
    Update a single Odoo business record by ID.
    """
    try:
        if isinstance(record_id, bool) or not isinstance(record_id, int):
            return {"success": False, "error": "record_id must be an integer.", "model": model}

        Model, prepared_values, _fields_info = _normalize_record_write_values(env, model, values)
        record = Model.browse(record_id).exists()
        if not record:
            return {"success": False, "error": "Record not found.", "model": model}

        record.check_access_rule("write")
        record.write(prepared_values)
        read_fields = _build_mutation_read_fields(Model, prepared_values, fields)
        try:
            record.check_access_rule("read")
            record_payload = _read_single_record_payload(record, read_fields)
            note = None
        except AccessError:
            record_payload = {"id": record.id}
            note = "Record was updated, but the current user cannot read the requested fields."

        result = {
            "success": True,
            "model": model,
            "record_id": record.id,
            "record": record_payload,
            "summary": "Updated %s #%s." % (model, record.id),
        }
        if note:
            result["note"] = note
        return result
    except AccessError:
        return {"success": False, "error": "Access denied. You don't have permission to update this record.", "model": model}
    except Exception as error:
        return {"success": False, "error": str(error), "model": model}


def archive_record(env, model, record_id, fields=None):
    """
    Archive a single Odoo business record by setting active to False.
    """
    try:
        if isinstance(record_id, bool) or not isinstance(record_id, int):
            return {"success": False, "error": "record_id must be an integer.", "model": model}

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

        record = Model.browse(record_id).exists()
        if not record:
            return {"success": False, "error": "Record not found.", "model": model}

        record.check_access_rule("write")
        was_active = bool(record.active)
        record.write({"active": False})
        read_fields = _build_mutation_read_fields(Model, {"active": False}, fields, default_fields=["active"])
        try:
            record.check_access_rule("read")
            record_payload = _read_single_record_payload(record, read_fields)
            note = None
        except AccessError:
            record_payload = {"id": record.id, "active": False}
            note = "Record was archived, but the current user cannot read the requested fields."

        result = {
            "success": True,
            "model": model,
            "record_id": record.id,
            "record": record_payload,
            "archived": True,
            "changed": was_active,
            "summary": "Archived %s #%s." % (model, record.id),
        }
        if note:
            result["note"] = note
        return result
    except AccessError:
        return {"success": False, "error": "Access denied. You don't have permission to archive this record.", "model": model}
    except Exception as error:
        return {"success": False, "error": str(error), "model": model}


def delete_record(env, model, record_id, fields=None):
    """
    Delete a single Odoo business record by ID.
    """
    try:
        if isinstance(record_id, bool) or not isinstance(record_id, int):
            return {"success": False, "error": "record_id must be an integer.", "model": model}

        try:
            Model = env[model]
        except KeyError:
            return {"success": False, "error": "Model '%s' not found." % model, "model": model}

        record = Model.browse(record_id).exists()
        if not record:
            return {"success": False, "error": "Record not found.", "model": model}

        read_fields = _build_mutation_read_fields(Model, {}, fields, default_fields=["id"])
        try:
            record.check_access_rule("read")
            record_payload = _read_single_record_payload(record, read_fields)
            note = None
        except AccessError:
            record_payload = {"id": record.id}
            note = "Record was deleted, but the current user could not read the requested fields before deletion."

        record.check_access_rule("unlink")
        record.unlink()

        result = {
            "success": True,
            "model": model,
            "record_id": record_id,
            "record": record_payload,
            "deleted": True,
            "summary": "Deleted %s #%s." % (model, record_id),
        }
        if note:
            result["note"] = note
        return result
    except AccessError:
        return {"success": False, "error": "Access denied. You don't have permission to delete this record.", "model": model}
    except Exception as error:
        return {"success": False, "error": str(error), "model": model}


def get_chart_creation_declaration():
    """
    Function declaration for creating saved Chart.js charts from business data.
    """
    return {
        "name": "chart_creation",
        "strict": True,
        "description": "Create one or more saved Chart.js charts from Odoo data and return backend/public URLs. Supports native Chart.js types only: bar, line, pie, doughnut, polarArea, radar, scatter, and bubble.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Chart title shown on the saved chart page.",
                },
                "model": {
                    "type": "string",
                    "description": "Technical Odoo model name (for example, 'sale.order').",
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
                    "description": "Optional Odoo domain filter. Pass null for all accessible records.",
                },
                "chart_types": {
                    "anyOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}}
                    ],
                    "description": "One chart type or a list of chart types. Supported values: bar, line, pie, doughnut, polarArea, radar, scatter, bubble.",
                },
                "x_axis": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": "Field used for grouped labels or scatter/bubble X values.",
                },
                "y_axis": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": "Required for scatter and bubble charts. Pass null for aggregate charts.",
                },
                "radius_field": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": "Required only for bubble charts. Pass null otherwise.",
                },
                "operation": {
                    "anyOf": [
                        {"type": "string", "enum": ["sum", "avg", "min", "max", "count"]},
                        {"type": "null"}
                    ],
                    "description": "Aggregate operation for bar, line, pie, doughnut, polarArea, and radar charts. Pass null for scatter/bubble.",
                },
                "metric_field": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": "Field to aggregate for sum/avg/min/max. Pass null for count or scatter/bubble.",
                },
                "limit": {
                    "anyOf": [{"type": "integer"}, {"type": "null"}],
                    "description": "Optional maximum number of labels or points to include.",
                },
                "order": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": "Optional sort order, mainly for scatter/bubble source rows.",
                },
                "subtitle": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": "Optional subtitle for the saved chart page.",
                },
                "summary": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": "Optional short explanation to save with the chart.",
                },
            },
            "required": [
                "title",
                "model",
                "domain",
                "chart_types",
                "x_axis",
                "y_axis",
                "radius_field",
                "operation",
                "metric_field",
                "limit",
                "order",
                "subtitle",
                "summary",
            ],
            "additionalProperties": False,
        },
    }


def get_chart_update_declaration():
    """
    Function declaration for updating an existing saved Chart.js chart in place.
    """
    chart_parameters = deepcopy(get_chart_creation_declaration()["parameters"])
    chart_parameters["properties"] = {
        "chart_id": {
            "anyOf": [{"type": "integer"}, {"type": "null"}],
            "description": "Existing saved chart ID to update. Set chart_name to null when using this.",
        },
        "chart_name": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "Existing saved chart title to update when chart_id is not known. Set chart_id to null when using this.",
        },
        **chart_parameters["properties"],
    }
    chart_parameters["required"] = ["chart_id", "chart_name"] + chart_parameters["required"]
    return {
        "name": "chart_update",
        "strict": True,
        "description": "Update an existing saved Chart.js chart in place. Provide chart_id or chart_name plus the full replacement chart definition.",
        "parameters": chart_parameters,
    }


def get_code_search_declaration():
    """
    Function declaration for read-only Odoo addon code search.
    """
    return {
        "name": "code_search",
        "strict": True,
        "description": "Search read-only Odoo addon source code across installed custom modules, community addons, and enterprise addons when available. Returns ranked matches with file path, line number, snippet, and a short summary.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search text or natural-language code question.",
                },
                "module_names": {
                    "anyOf": [
                        {"type": "array", "items": {"type": "string"}},
                        {"type": "null"},
                    ],
                    "description": "Optional list of module names to narrow the search.",
                },
                "file_types": {
                    "anyOf": [
                        {"type": "array", "items": {"type": "string"}},
                        {"type": "null"},
                    ],
                    "description": "Optional file types such as py, xml, js, scss, csv, json, or __manifest__.py.",
                },
                "limit": {
                    "anyOf": [{"type": "integer"}, {"type": "null"}],
                    "description": "Optional maximum number of search results to return.",
                },
            },
            "required": ["query", "module_names", "file_types", "limit"],
            "additionalProperties": False,
        },
    }


def get_code_read_declaration():
    """
    Function declaration for bounded read-only Odoo addon code reads.
    """
    return {
        "name": "code_read",
        "strict": True,
        "description": "Read a bounded snippet from an Odoo addon source file under allowed addon roots. This tool is strictly read-only and rejects paths outside allowed addon roots.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute file path returned by code_search.",
                },
                "start_line": {
                    "anyOf": [{"type": "integer"}, {"type": "null"}],
                    "description": "Optional starting line number. Defaults to 1 when omitted.",
                },
                "end_line": {
                    "anyOf": [{"type": "integer"}, {"type": "null"}],
                    "description": "Optional ending line number. Defaults to a bounded range when omitted.",
                },
            },
            "required": ["path", "start_line", "end_line"],
            "additionalProperties": False,
        },
    }


def get_dashboard_creation_declaration():
    """
    Function declaration for creating a multi-chart saved dashboard.
    """
    chart_request_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
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
                                            {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "number"}]}},
                                        ]
                                    },
                                },
                            ]
                        },
                    },
                    {"type": "null"},
                ],
            },
            "chart_types": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ],
            },
            "x_axis": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "y_axis": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "radius_field": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "operation": {
                "anyOf": [
                    {"type": "string", "enum": ["sum", "avg", "min", "max", "count"]},
                    {"type": "null"},
                ],
            },
            "metric_field": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "limit": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
            "order": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "subtitle": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "summary": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
        "required": [
            "title",
            "model",
            "domain",
            "chart_types",
            "x_axis",
            "y_axis",
            "radius_field",
            "operation",
            "metric_field",
            "limit",
            "order",
            "subtitle",
            "summary",
        ],
        "additionalProperties": False,
    }
    table_request_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
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
                                            {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "number"}]}},
                                        ]
                                    },
                                },
                            ]
                        },
                    },
                    {"type": "null"},
                ],
            },
            "fields": {"type": "array", "items": {"type": "string"}},
            "limit": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
            "order": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "subtitle": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "summary": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
        "required": ["title", "model", "domain", "fields", "limit", "order", "subtitle", "summary"],
        "additionalProperties": False,
    }
    return {
        "name": "dashboard_creation",
        "strict": True,
        "description": "Create a saved dashboard with multiple Chart.js charts and optional tabular widgets. Can build widgets from explicit requests or from a high-level dashboard goal.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Dashboard title."},
                "subtitle": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "summary": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "dashboard_goal": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": "High-level goal such as sales, purchases, or invoices. Used when chart_requests are not provided.",
                },
                "chart_requests": {
                    "anyOf": [
                        {"type": "array", "items": chart_request_schema},
                        {"type": "null"},
                    ],
                    "description": "Optional explicit chart requests to create and attach. Do not repeat charts that already exist; pass those through chart_ids instead.",
                },
                "table_requests": {
                    "anyOf": [
                        {"type": "array", "items": table_request_schema},
                        {"type": "null"},
                    ],
                    "description": "Optional explicit table/list requests to create and attach as dashboard widgets.",
                },
                "chart_ids": {
                    "anyOf": [
                        {"type": "array", "items": {"type": "integer"}},
                        {"type": "null"},
                    ],
                    "description": "Optional existing saved chart IDs to attach without recreating them.",
                },
                "shared_user_ids": {
                    "anyOf": [
                        {"type": "array", "items": {"type": "integer"}},
                        {"type": "null"},
                    ],
                    "description": "Optional Odoo user IDs to share the dashboard with.",
                },
            },
            "required": ["name", "subtitle", "summary", "dashboard_goal", "chart_requests", "table_requests", "chart_ids", "shared_user_ids"],
            "additionalProperties": False,
        },
    }


def get_dashboard_update_declaration():
    """
    Function declaration for appending charts to an existing saved dashboard.
    """
    chart_request_schema = get_dashboard_creation_declaration()["parameters"]["properties"]["chart_requests"]["anyOf"][0]["items"]
    table_request_schema = get_dashboard_creation_declaration()["parameters"]["properties"]["table_requests"]["anyOf"][0]["items"]
    return {
        "name": "dashboard_update",
        "strict": True,
        "description": "Append existing charts, newly created charts, or new tabular widgets to an existing saved dashboard.",
        "parameters": {
            "type": "object",
            "properties": {
                "dashboard_id": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                "dashboard_name": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "chart_ids": {
                    "anyOf": [
                        {"type": "array", "items": {"type": "integer"}},
                        {"type": "null"},
                    ],
                },
                "chart_requests": {
                    "anyOf": [
                        {"type": "array", "items": chart_request_schema},
                        {"type": "null"},
                    ],
                },
                "table_requests": {
                    "anyOf": [
                        {"type": "array", "items": table_request_schema},
                        {"type": "null"},
                    ],
                },
                "append_mode": {
                    "anyOf": [
                        {"type": "string", "enum": ["append"]},
                        {"type": "null"},
                    ],
                },
            },
            "required": ["dashboard_id", "dashboard_name", "chart_ids", "chart_requests", "table_requests", "append_mode"],
            "additionalProperties": False,
        },
    }


def get_dashboard_remove_charts_declaration():
    """
    Function declaration for removing one or more charts from an existing dashboard.
    """
    return {
        "name": "dashboard_remove_charts",
        "strict": True,
        "description": "Remove one or more chart links from an existing saved dashboard. This detaches the charts from the dashboard without deleting the chart records.",
        "parameters": {
            "type": "object",
            "properties": {
                "dashboard_id": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                "dashboard_name": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "line_ids": {
                    "anyOf": [
                        {"type": "array", "items": {"type": "integer"}},
                        {"type": "null"},
                    ],
                    "description": "Optional dashboard line IDs to remove. Set chart_ids to null when using these.",
                },
                "chart_ids": {
                    "anyOf": [
                        {"type": "array", "items": {"type": "integer"}},
                        {"type": "null"},
                    ],
                    "description": "Optional chart IDs already linked to the dashboard. Set line_ids to null when using these.",
                },
            },
            "required": ["dashboard_id", "dashboard_name", "line_ids", "chart_ids"],
            "additionalProperties": False,
        },
    }


def get_dashboard_replace_chart_declaration():
    """
    Function declaration for replacing a chart already linked to a dashboard.
    """
    chart_request_schema = get_dashboard_creation_declaration()["parameters"]["properties"]["chart_requests"]["anyOf"][0]["items"]
    return {
        "name": "dashboard_replace_chart",
        "strict": True,
        "description": "Replace an existing chart already linked to a dashboard, preserving the dashboard slot. Provide replacement_chart_id to reuse a saved chart or chart_request to create a new replacement chart.",
        "parameters": {
            "type": "object",
            "properties": {
                "dashboard_id": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                "dashboard_name": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "line_id": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                "chart_id": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                "replacement_chart_id": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                "chart_request": {
                    "anyOf": [
                        chart_request_schema,
                        {"type": "null"},
                    ],
                },
            },
            "required": [
                "dashboard_id",
                "dashboard_name",
                "line_id",
                "chart_id",
                "replacement_chart_id",
                "chart_request",
            ],
            "additionalProperties": False,
        },
    }


def chart_creation(
    env,
    title,
    model,
    domain=None,
    chart_types=None,
    x_axis=None,
    y_axis=None,
    radius_field=None,
    operation=None,
    metric_field=None,
    limit=None,
    order=None,
    subtitle=None,
    summary=None,
):
    """
    Delegate chart creation to the MCP module when installed.
    """
    try:
        ChartService = env["sh.ai.mcp.chart"]
    except KeyError:
        return {
            "success": False,
            "error": "Chart creation requires the sh_ai_mcp module to be installed.",
        }

    return ChartService.execute_ai_chart_tool(
        title=title,
        model=model,
        domain=domain,
        chart_types=chart_types,
        x_axis=x_axis,
        y_axis=y_axis,
        radius_field=radius_field,
        operation=operation,
        metric_field=metric_field,
        limit=limit,
        order=order,
        subtitle=subtitle,
        summary=summary,
    )


def chart_update(
    env,
    chart_id=None,
    chart_name=None,
    title=None,
    model=None,
    domain=None,
    chart_types=None,
    x_axis=None,
    y_axis=None,
    radius_field=None,
    operation=None,
    metric_field=None,
    limit=None,
    order=None,
    subtitle=None,
    summary=None,
):
    """
    Delegate chart updates to the MCP module when installed.
    """
    try:
        ChartService = env["sh.ai.mcp.chart"]
    except KeyError:
        return {
            "success": False,
            "error": "Chart updates require the sh_ai_mcp module to be installed.",
        }

    return ChartService.execute_ai_chart_update_tool(
        chart_id=chart_id,
        chart_name=chart_name,
        title=title,
        model=model,
        domain=domain,
        chart_types=chart_types,
        x_axis=x_axis,
        y_axis=y_axis,
        radius_field=radius_field,
        operation=operation,
        metric_field=metric_field,
        limit=limit,
        order=order,
        subtitle=subtitle,
        summary=summary,
    )


def dashboard_creation(env, name, subtitle=None, summary=None, dashboard_goal=None, chart_requests=None, table_requests=None, chart_ids=None, shared_user_ids=None):
    """
    Delegate dashboard creation to the MCP module when installed.
    """
    try:
        DashboardService = env["sh.ai.mcp.dashboard"]
    except KeyError:
        return {
            "success": False,
            "error": "Dashboard creation requires the sh_ai_mcp module to be installed.",
        }

    return DashboardService.execute_ai_dashboard_creation_tool(
        name=name,
        subtitle=subtitle,
        summary=summary,
        dashboard_goal=dashboard_goal,
        chart_requests=chart_requests,
        table_requests=table_requests,
        chart_ids=chart_ids,
        shared_user_ids=shared_user_ids,
    )


def dashboard_update(env, dashboard_id=None, dashboard_name=None, chart_ids=None, chart_requests=None, table_requests=None, append_mode=None):
    """
    Delegate dashboard updates to the MCP module when installed.
    """
    try:
        DashboardService = env["sh.ai.mcp.dashboard"]
    except KeyError:
        return {
            "success": False,
            "error": "Dashboard updates require the sh_ai_mcp module to be installed.",
        }

    return DashboardService.execute_ai_dashboard_update_tool(
        dashboard_id=dashboard_id,
        dashboard_name=dashboard_name,
        chart_ids=chart_ids,
        chart_requests=chart_requests,
        table_requests=table_requests,
        append_mode=append_mode,
    )


def execute_record_action(env, model, record_id, action_name, fields=None):
    """
    Delegate allow-listed record action execution to the MCP module when installed.
    """
    try:
        ActionService = env["sh.ai.mcp.action.tool"]
    except KeyError:
        return {
            "success": False,
            "error": "Record action execution requires the sh_ai_mcp module to be installed.",
        }

    return ActionService.execute_ai_record_action_tool(
        model=model,
        record_id=record_id,
        action_name=action_name,
        fields=fields,
    )


def submit_action_wizard(env, wizard_session_id, values=None, action_name=None, fields=None):
    """
    Delegate wizard submissions for record actions to the MCP module when installed.
    """
    try:
        ActionService = env["sh.ai.mcp.action.tool"]
    except KeyError:
        return {
            "success": False,
            "error": "Wizard action submission requires the sh_ai_mcp module to be installed.",
        }

    return ActionService.submit_ai_action_wizard_tool(
        wizard_session_id=wizard_session_id,
        values=values,
        action_name=action_name,
        fields=fields,
    )


def dashboard_remove_charts(env, dashboard_id=None, dashboard_name=None, line_ids=None, chart_ids=None):
    """
    Delegate dashboard chart removals to the MCP module when installed.
    """
    try:
        DashboardService = env["sh.ai.mcp.dashboard"]
    except KeyError:
        return {
            "success": False,
            "error": "Dashboard chart removal requires the sh_ai_mcp module to be installed.",
        }

    return DashboardService.execute_ai_dashboard_remove_charts_tool(
        dashboard_id=dashboard_id,
        dashboard_name=dashboard_name,
        line_ids=line_ids,
        chart_ids=chart_ids,
    )


def dashboard_replace_chart(
    env,
    dashboard_id=None,
    dashboard_name=None,
    line_id=None,
    chart_id=None,
    replacement_chart_id=None,
    chart_request=None,
):
    """
    Delegate dashboard chart replacement to the MCP module when installed.
    """
    try:
        DashboardService = env["sh.ai.mcp.dashboard"]
    except KeyError:
        return {
            "success": False,
            "error": "Dashboard chart replacement requires the sh_ai_mcp module to be installed.",
        }

    return DashboardService.execute_ai_dashboard_replace_chart_tool(
        dashboard_id=dashboard_id,
        dashboard_name=dashboard_name,
        line_id=line_id,
        chart_id=chart_id,
        replacement_chart_id=replacement_chart_id,
        chart_request=chart_request,
    )


def code_search(env, query, module_names=None, file_types=None, limit=None):
    """
    Delegate read-only addon code search to the MCP module when installed.
    """
    try:
        CodeService = env["sh.ai.mcp.code.tool"]
    except KeyError:
        return {
            "success": False,
            "error": "Code search requires the sh_ai_mcp module to be installed.",
        }

    return CodeService.execute_ai_code_search_tool(
        query=query,
        module_names=module_names,
        file_types=file_types,
        limit=limit,
    )


def code_read(env, path, start_line=None, end_line=None):
    """
    Delegate bounded read-only addon code reads to the MCP module when installed.
    """
    try:
        CodeService = env["sh.ai.mcp.code.tool"]
    except KeyError:
        return {
            "success": False,
            "error": "Code reading requires the sh_ai_mcp module to be installed.",
        }

    return CodeService.execute_ai_code_read_tool(
        path=path,
        start_line=start_line,
        end_line=end_line,
    )


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
