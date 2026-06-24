# -*- coding: utf-8 -*-
# Part of SoftHealer Technologies PVT.LTD.

"""Shared AI tool registry for transport-agnostic integrations."""

from __future__ import annotations

from copy import deepcopy

from .odoo_tools import (
    aggregate_records,
    archive_record,
    create_record,
    delete_record,
    fuzzy_lookup,
    fuzzy_lookup_declaration,
    get_aggregate_records_declaration,
    get_archive_record_declaration,
    get_create_record_declaration,
    get_delete_record_declaration,
    get_current_date_info,
    get_current_date_info_declaration,
    get_model_fields,
    get_model_fields_declaration,
    get_models_list,
    get_models_list_declaration,
    get_open_view_declaration,
    get_search_records_declaration,
    get_selection_values,
    get_selection_values_declaration,
    open_view,
    search_records,
    update_record,
    get_update_record_declaration,
)


_TOOL_SPECS = (
    {
        "name": "fuzzy_lookup",
        "declaration_factory": fuzzy_lookup_declaration,
        "executor": fuzzy_lookup,
        "read_only": True,
        "remote_safe": True,
    },
    {
        "name": "search_records",
        "declaration_factory": get_search_records_declaration,
        "executor": search_records,
        "read_only": True,
        "remote_safe": True,
    },
    {
        "name": "get_models_list",
        "declaration_factory": get_models_list_declaration,
        "executor": get_models_list,
        "read_only": True,
        "remote_safe": True,
    },
    {
        "name": "get_model_fields",
        "declaration_factory": get_model_fields_declaration,
        "executor": get_model_fields,
        "read_only": True,
        "remote_safe": True,
    },
    {
        "name": "get_selection_values",
        "declaration_factory": get_selection_values_declaration,
        "executor": get_selection_values,
        "read_only": True,
        "remote_safe": True,
    },
    {
        "name": "get_current_date_info",
        "declaration_factory": get_current_date_info_declaration,
        "executor": get_current_date_info,
        "read_only": True,
        "remote_safe": True,
    },
    {
        "name": "aggregate_records",
        "declaration_factory": get_aggregate_records_declaration,
        "executor": aggregate_records,
        "read_only": True,
        "remote_safe": True,
    },
    {
        "name": "create_record",
        "declaration_factory": get_create_record_declaration,
        "executor": create_record,
        "read_only": False,
        "remote_safe": True,
    },
    {
        "name": "update_record",
        "declaration_factory": get_update_record_declaration,
        "executor": update_record,
        "read_only": False,
        "remote_safe": True,
    },
    {
        "name": "archive_record",
        "declaration_factory": get_archive_record_declaration,
        "executor": archive_record,
        "read_only": False,
        "remote_safe": True,
    },
    {
        "name": "delete_record",
        "declaration_factory": get_delete_record_declaration,
        "executor": delete_record,
        "read_only": False,
        "remote_safe": True,
    },



    {
        "name": "open_view",
        "declaration_factory": get_open_view_declaration,
        "executor": open_view,
        "read_only": True,
        "remote_safe": False,
    },
)

_TOOL_REGISTRY = {spec["name"]: dict(spec) for spec in _TOOL_SPECS}


def _filtered_tool_specs(tool_names=None, remote_safe_only=False):
    """Return tool specs matching the requested names and exposure policy."""
    if tool_names is None:
        selected_names = [spec["name"] for spec in _TOOL_SPECS]
    elif isinstance(tool_names, str):
        selected_names = [tool_names]
    else:
        selected_names = list(tool_names)

    filtered_specs = []
    for tool_name in selected_names:
        spec = _TOOL_REGISTRY.get(tool_name)
        if not spec:
            raise ValueError("Unknown AI tool '%s'." % tool_name)
        if remote_safe_only and not spec.get("remote_safe"):
            continue
        filtered_specs.append(spec)
    return filtered_specs


def get_sh_ai_tool_names(remote_safe_only=False):
    """Return the registered tool names in stable order."""
    return [spec["name"] for spec in _filtered_tool_specs(remote_safe_only=remote_safe_only)]


def get_sh_ai_tool_metadata(tool_name):
    """Return metadata for a single tool."""
    spec = _TOOL_REGISTRY.get(tool_name)
    if not spec:
        raise ValueError("Unknown AI tool '%s'." % tool_name)
    metadata = dict(spec)
    metadata["declaration"] = metadata["declaration_factory"]()
    metadata.pop("declaration_factory", None)
    return metadata


def get_sh_ai_tool_declarations(tool_names=None, remote_safe_only=False):
    """Return OpenAI/Gemini-style tool declarations for the selected tools."""
    return [
        deepcopy(spec["declaration_factory"]())
        for spec in _filtered_tool_specs(tool_names=tool_names, remote_safe_only=remote_safe_only)
    ]


def get_sh_ai_mcp_tool_definitions(tool_names=None, remote_safe_only=True):
    """Return MCP `tools/list` payloads for remote-safe tools."""
    definitions = []
    for spec in _filtered_tool_specs(tool_names=tool_names, remote_safe_only=remote_safe_only):
        declaration = spec["declaration_factory"]()
        definitions.append({
            "name": declaration["name"],
            "description": declaration.get("description") or "",
            "inputSchema": deepcopy(declaration.get("parameters") or {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            }),
        })
    return definitions


def execute_sh_ai_tool(env, tool_name, tool_args=None):
    """Execute a registered AI tool against the provided Odoo environment."""
    spec = _TOOL_REGISTRY.get(tool_name)
    if not spec:
        raise ValueError("Unknown AI tool '%s'." % tool_name)

    tool_args = tool_args or {}
    if not isinstance(tool_args, dict):
        raise ValueError("Tool arguments for '%s' must be a dictionary." % tool_name)

    return spec["executor"](env, **tool_args)
