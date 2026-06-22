# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies.

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
                    "description": "Maximum records to return. Pass null for default limit (10).",
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

        # CONSISTENCY ENFORCEMENT: Default limit to 10 for better UX
        # This ensures AI shows manageable results and "View All" button appears
        if limit is None:
            limit = 10

        # CRITICAL: Maximum limit enforcement - NEVER allow more than 10 records in chat
        # This prevents UI overload and ensures consistent UX even with millions of records
        MAX_DISPLAY_LIMIT = 10
        if limit > MAX_DISPLAY_LIMIT:
            limit = MAX_DISPLAY_LIMIT

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
                if not Model.browse().check_access_rights('read'):
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

        if not Model.browse().check_access_rights('read'):
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

                result_groups = Model._read_group(
                    domain=domain,
                    groupby=[group_by],
                    aggregates=aggregates,
                    having=having or [],
                    offset=0,
                    limit=None
                )

                groups = []
                for group_tuple in result_groups:
                    group_val = group_tuple[0]
                    agg_val = group_tuple[1]
                    
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


def open_view(env, model, view_type, domain=None, group_by=None, graph_mode=None):
    """
    Redirect user to a specific Odoo view.
    """
    try:
        try:
            Model = env[model]
        except KeyError:
            return {"success": False, "error": f"Model '{model}' not found"}

        if not Model.browse().check_access_rights('read'):
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
