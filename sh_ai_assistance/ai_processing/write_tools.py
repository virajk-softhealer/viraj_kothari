# -*- coding: utf-8 -*-
# Part of Softhealer Technologies.


DOMAIN_SCHEMA = {
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
    "description": "Odoo domain filter. Use arrays like [['field', '=', value]]. Pass null for all records.",
}

VALUE_OBJECT_SCHEMA = {
    "type": "object",
    "description": (
        "Field/value mapping using technical field names. "
        "Use simple scalars for scalar fields, a record name or ID for many2one, "
        "a list or {set/add/link} object for many2many, and a list or {create/update/link/set} object for one2many."
    ),
    "additionalProperties": True,
}

SHARED_WRITE_SAFETY_POLICY = """
<shared_write_safety_policy>
When write-preparation tools are available, these rules are mandatory:

1. PREPARE ONLY: `prepare_create_record`, `prepare_update_records`, and `prepare_archive_records` only prepare an approval request. They never execute the business write.
2. USE THE RIGHT TOOL: If the user asks to create a record, call `prepare_create_record`. If the user asks to update records, call `prepare_update_records`. If the user asks to archive/deactivate records, call `prepare_archive_records`.
3. APPROVAL REQUIRED: Never say a record was created, updated, or archived immediately after a prepare tool call. Tell the user an approval preview is ready and waiting for confirmation.
4. ASK FOLLOW-UP QUESTIONS ON AMBIGUITY: If the write tool says the target, related record, or required values are unclear, ask the user for the missing detail instead of guessing.
5. NO DELETE: Archive is allowed only through the archive preparation tool. Never promise delete or unlink behavior.
6. NO RAW TECHNICAL PAYLOADS: Summarize the proposed change in business language, then ask the user to approve or reject it.
7. STAY WITHIN POLICY: If the tool reports that the model, field, or operation is blocked, explain that the AI write policy does not allow the change.
</shared_write_safety_policy>
"""


def get_write_prompt_sections():
    return [{
        "name": "shared_write_safety_policy",
        "content": SHARED_WRITE_SAFETY_POLICY.strip(),
    }]


def get_prepare_create_record_declaration():
    return {
        "name": "prepare_create_record",
        "strict": False,
        "description": (
            "Prepare a create request for approval. This DOES NOT create any business data. "
            "Use it when the user wants to create a record and all required values are clear."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "description": "Technical model name to create into.",
                },
                "values": VALUE_OBJECT_SCHEMA,
            },
            "required": ["model", "values"],
            "additionalProperties": False,
        },
    }


def get_prepare_update_records_declaration():
    return {
        "name": "prepare_update_records",
        "strict": False,
        "description": (
            "Prepare an update request for approval. This DOES NOT update any business data. "
            "Use it only when the target records and intended field changes are clear."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "description": "Technical model name to update.",
                },
                "domain": DOMAIN_SCHEMA,
                "values": VALUE_OBJECT_SCHEMA,
            },
            "required": ["model", "domain", "values"],
            "additionalProperties": False,
        },
    }


def get_prepare_archive_records_declaration():
    return {
        "name": "prepare_archive_records",
        "strict": False,
        "description": (
            "Prepare an archive request for approval. This DOES NOT archive any business data. "
            "Use it when the user wants to deactivate or archive matching records."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "description": "Technical model name to archive.",
                },
                "domain": DOMAIN_SCHEMA,
            },
            "required": ["model", "domain"],
            "additionalProperties": False,
        },
    }


def prepare_create_record(env, model=None, values=None, session_id=None, llm_id=None):
    if not model or not values:
        return {"error": "Missing required arguments for prepare_create_record. Both 'model' and 'values' are required.", "success": False}
    
    return env["sh.ai.write.request"].prepare_create_from_ai(
        model=model,
        values=values,
        session_id=session_id,
        llm_id=llm_id,
    )


def prepare_update_records(env, model=None, domain=None, values=None, session_id=None, llm_id=None):
    if not model or not values:
        return {"error": "Missing required arguments for prepare_update_records. 'model', 'domain' and 'values' are all required.", "success": False}
    return env["sh.ai.write.request"].prepare_update_from_ai(
        model=model,
        domain=domain,
        values=values,
        session_id=session_id,
        llm_id=llm_id,
    )


def prepare_archive_records(env, model=None, domain=None, session_id=None, llm_id=None):
    if not model:
        return {"error": "Missing required argument 'model' for prepare_archive_records.", "success": False}
    return env["sh.ai.write.request"].prepare_archive_from_ai(
        model=model,
        domain=domain,
        session_id=session_id,
        llm_id=llm_id,
    )
