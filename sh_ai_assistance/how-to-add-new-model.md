# How To Add New Model

This note is for adding a new AI model or provider into this Odoo AI app, for example Claude, Qwen, new OpenAI models, or new Gemini models.

The goal is not only "make API call work". The goal is:

- correct answers
- stable tool calling
- provider-neutral behavior
- good debug visibility
- safe failure handling
- good UX in chat

## 1. First Decide: New Model Or New Provider

There are two different cases.

### New model on an existing provider

Examples:

- `gpt-5-mini` to `gpt-5-large`
- `gemini-3-flash-preview` to `gemini-3-pro`

Usually you only need:

- new `sh.ai.llm` record
- capability verification
- prompt/tool compatibility testing
- caching verification
- live scenario evaluation

### New provider

Examples:

- Claude
- Qwen
- DeepSeek

You usually need:

- new provider wrapper
- new engine or adapter
- capability mapping
- tool-call protocol implementation
- usage extraction
- error mapping
- cancellation behavior
- provider-specific tests

Do not treat these two cases as the same task.

## 2. Keep Provider-Neutral Rules In The Shared Layer

Shared business behavior belongs in the common engine/tool layer, not inside each provider.

Keep these generic:

- prompt assembly
- tool result contract
- domain normalization
- grouped aggregation correctness
- loop guard rules
- action-data generation
- debug event schema

Provider files should stay thin.

## 3. Minimum Capability Checklist

Before enabling a model, verify these items from official docs and live tests:

- text generation
- tool/function calling
- multi-turn tool loop support
- streaming
- token/usage reporting
- cancellation behavior
- system prompt support
- JSON/schema/tool declaration format
- caching behavior

If any capability is missing, document it in the provider capability matrix instead of silently assuming parity.

## 4. Required Files To Review

When adding a new provider/model, review these files first:

- [ai_automation/sh_ai_assistance/ai_processing/base_engine.py](/Users/mayurbechara/Documents/odoo19/ai_automation/sh_ai_assistance/ai_processing/base_engine.py)
- [ai_automation/sh_ai_assistance/ai_processing/engine_factory.py](/Users/mayurbechara/Documents/odoo19/ai_automation/sh_ai_assistance/ai_processing/engine_factory.py)
- [ai_automation/sh_ai_assistance/ai_processing/openai_engine.py](/Users/mayurbechara/Documents/odoo19/ai_automation/sh_ai_assistance/ai_processing/openai_engine.py)
- [ai_automation/sh_ai_assistance/ai_processing/gemini_engine.py](/Users/mayurbechara/Documents/odoo19/ai_automation/sh_ai_assistance/ai_processing/gemini_engine.py)
- [ai_automation/sh_ai_base/provider/openai_provider.py](/Users/mayurbechara/Documents/odoo19/ai_automation/sh_ai_base/provider/openai_provider.py)
- [ai_automation/sh_ai_base/provider/gemini_provider.py](/Users/mayurbechara/Documents/odoo19/ai_automation/sh_ai_base/provider/gemini_provider.py)
- [ai_automation/sh_ai_base/provider/prompt_builder.py](/Users/mayurbechara/Documents/odoo19/ai_automation/sh_ai_base/provider/prompt_builder.py)
- [ai_automation/sh_ai_base/provider/odoo_tools.py](/Users/mayurbechara/Documents/odoo19/ai_automation/sh_ai_base/provider/odoo_tools.py)
- [ai_automation/sh_ai_assistance/models/sh_ai_llm.py](/Users/mayurbechara/Documents/odoo19/ai_automation/sh_ai_assistance/models/sh_ai_llm.py)
- [ai_automation/sh_ai_assistance/models/sh_ai_chat_message.py](/Users/mayurbechara/Documents/odoo19/ai_automation/sh_ai_assistance/models/sh_ai_chat_message.py)
- [ai_automation/sh_ai_assistance/static/src/components/ai_chat_content/ai_chat_content.js](/Users/mayurbechara/Documents/odoo19/ai_automation/sh_ai_assistance/static/src/components/ai_chat_content/ai_chat_content.js)
- [ai_automation/sh_ai_assistance/static/src/components/chat_message/chat_message.js](/Users/mayurbechara/Documents/odoo19/ai_automation/sh_ai_assistance/static/src/components/chat_message/chat_message.js)

## 5. Tool Calling Rules

New providers must obey the same internal tool contract:

- `success`
- `data`
- `user_safe_summary`
- `debug`
- `retryable`
- `action_data`

Do not let provider-specific raw payloads leak into the rest of the app.

### Critical tool rules learned from real bugs

- grouped `count` must count records, not distinct group values
- relational filters must preserve operator meaning
- dotted relational paths must resolve dynamically
- do not hard-code field-specific logic
- ambiguous fuzzy matches should not silently rewrite the query

Examples of wrong behavior:

- changing `!=` into `=`
- changing `not in` into `in`
- treating `group_by` in plain search as real aggregation
- counting each group as `1`

## 6. Prompt Rules

The model prompt must push the provider toward:

- justified tool calls
- schema discovery when needed, not by habit
- ID resolution for relational filters
- stopping when the answer is already clear
- treating zero-result answers as valid outcomes

Do not hard-code prompt behavior for one model or one field.

When adding a new model:

- confirm the existing prompt style still works
- confirm long instruction blocks are accepted
- confirm tool documentation format is understood
- confirm examples do not trigger provider-specific parsing issues

## 7. Cache Considerations

Caching is not optional to think about. Even if the provider handles it implicitly, prompt structure still matters.

### Current learned rules

- keep stable instructions at the top
- keep dynamic user/session content later
- keep tool declarations stable when possible
- avoid unnecessary prompt churn

### OpenAI

- supports prompt-prefix caching behavior
- current app sends a stable `prompt_cache_key`
- verify cached token reporting

### Gemini

- implicit caching benefits from stable prefixes
- explicit cache objects are a separate feature and not currently implemented here

When adding a new model/provider, check:

- does it support caching?
- is caching automatic or explicit?
- what fields expose cache hits?
- will cache keys break if tool schemas change often?

## 8. Streaming And Partial Response Handling

Do not assume a provider's non-streaming and streaming payloads are identical.

Important rules:

- preserve all model parts needed for the next turn
- do not reconstruct provider messages loosely if the provider requires exact round-trip data
- partial responses must still be valid for stop/cancel behavior

### Real lesson from Gemini 3

Gemini 3 tool calling required preserving:

- original model `Part` objects
- `thought_signature`
- function call IDs

If these were dropped, the next request failed even though the first API call succeeded.

This is the kind of provider-specific protocol detail that must stay in the provider/engine layer.

## 9. Cancellation And Loop Guards

Every provider integration must support:

- stop while streaming
- no assistant persistence after cancellation
- duplicate tool call guard
- max iteration guard
- malformed tool argument guard

Also make sure the engine does not crash if the model never produces a final text answer. It must fail cleanly.

## 10. Error Handling

Map provider errors into user-safe messages, but keep technical detail in debug logs.

Need both:

- friendly UI error
- technical backend trace

Do not expose raw provider stack traces to the user.

Also make sure failed AI messages still carry model identity:

- `llm_id` must be stored
- UI should show model name, not a generic `Error`

## 11. UI Requirements

When a new model is added, verify:

- correct model name in chat header
- correct avatar/icon
- correct model label on assistant messages
- correct model label on error messages
- typing indicator still works
- debug panel still shows useful info

If an error happens, the user should still know which model failed.

## 12. Query Metadata And Debug Truth

One important lesson:

- correct answer is not enough
- debug metadata must also reflect the real evidence path

Watch for drift in:

- `query_details`
- `final_query`
- `last_search_call`
- `last_search_result`

If the model explores multiple paths, make sure the final stored metadata does not misleadingly point to an irrelevant last exploration step.

## 13. Database And Config Checks

Before blaming the code, verify:

- DB `sh.ai.llm` row exists
- API key is real, not placeholder
- model code is correct
- provider selection is correct
- active/default flags are correct

Common mistake:

- code is fixed but DB still has demo keys like `YOUR_GEMINI_KEY` or `YOUR_OPENAI_KEY`

## 14. Tests You Must Add

At minimum add:

- provider-specific unit or regression tests
- shared engine contract tests
- tool-call protocol tests
- failure-mode tests

Examples:

- signed tool-call round-trip preserved
- function response ID preserved
- engine fails cleanly after max iterations
- cancellation returns partial or stops safely
- grouped aggregation remains correct
- negative relational operators remain correct

## 15. Real Scenario Testing Checklist

Do not stop at unit tests.

Run live scenario tests on a real DB with actual provider credentials.

Use queries that cover:

- plain count
- grouped count
- relational lookup
- negative relational filter
- dotted relational path
- zero-result query
- open-view action
- ambiguous lookup
- provider failure path

Compare the assistant answer to ORM truth, not only to what "sounds right".

## 16. Recommended Rollout Order

When adding a new model/provider:

1. add provider/model record
2. verify auth
3. verify plain text generation
4. verify single tool call
5. verify multi-step tool loop
6. verify cancellation
7. verify cache behavior
8. verify error rendering in UI
9. run real scenario evaluation set

## 17. Do Not Do These Things

- do not hard-code field-specific behavior
- do not hard-code country-specific or model-specific fixes for one prompt
- do not copy one provider's protocol fix into another provider blindly
- do not trust friendly-looking answers without ORM verification
- do not store incomplete provider metadata if the next tool turn needs it
- do not let debug metadata drift away from actual executed logic

## 18. Current Real Lessons From This Project

These were real bugs we already hit and fixed:

- grouped count returned `1` for every group
- relational operators were rewritten incorrectly
- dotted relational paths were not normalized
- Gemini 3 tool loop failed because `thought_signature` was dropped
- Gemini tool loop could crash if no final text answer was produced
- error messages in UI showed `Error` instead of the model name

Use these as a warning: new provider work fails most often in the edges between turns, not in the first API call.

## 19. Final Standard

A provider/model is not "added" when:

- it appears in the dropdown
- one hello-world prompt works

A provider/model is added only when:

- real auth works
- tool loops work
- errors are handled
- cache behavior is understood
- UI shows the right identity
- debug info is trustworthy
- real scenarios match ORM truth
