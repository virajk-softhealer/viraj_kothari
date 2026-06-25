/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { BooleanToggleField } from "@web/views/fields/boolean_toggle/boolean_toggle_field";
import { ListBooleanToggleField } from "@web/views/fields/boolean_toggle/list_boolean_toggle_field";
import { _t } from "@web/core/l10n/translation";

const shAiBooleanTogglePatch = {
    async onChange(newValue) {
        const fieldName = this.props.name;
        const record = this.props.record;
        const model = record.model;

        if (record.resModel === 'sh.ai.llm' && fieldName === 'is_default') {
            // Case: Trying to unset the default
            if (newValue === false) {
                // Check local records if in List view, otherwise rely on server
                const otherDefaultExists = (model.root.records && model.root.records.some(rec => 
                    rec.resId !== record.resId && rec.data.is_default
                )) || false;

                if (!otherDefaultExists) {
                    // Fallback check to server (handles Form view and other pages)
                    const count = await this.env.services.orm.call(
                        'sh.ai.llm',
                        'search_count',
                        [[['is_default', '=', true], ['id', '!=', record.resId]]]
                    );
                    
                    if (count === 0) {
                        this.env.services.notification.add(
                            _t("Cannot unset the only default LLM. Please set another LLM as default first."),
                            { type: "warning" }
                        );
                        
                        // Visual "Snap-Back" Animation
                        record.data.is_default = false;
                        if (model && typeof model.notify === 'function') {
                            model.notify();
                        }
                        
                        setTimeout(() => {
                            record.data.is_default = true;
                            if (model && typeof model.notify === 'function') {
                                model.notify();
                            }
                        }, 250);
                        return;
                    }
                }
            }

            // Case: Setting a new default (Exclusivity) - Only visible in List view
            if (newValue === true && model.root && model.root.records) {
                model.root.records.forEach(rec => {
                    if (rec.resId !== record.resId && rec.data.is_default) {
                        rec.data.is_default = false;
                    }
                });
            }
        }

        // Apply change (calls Python write)
        await this.props.update(newValue, { save: this.props.autosave });
        
        // Notify model to refresh UI components for all visible rows/fields
        if (model && typeof model.notify === 'function') {
            model.notify();
        }
    },
};

// Patch the base component (Form view)
patch(BooleanToggleField.prototype, "sh_ai_base.BooleanToggleFieldPatch", shAiBooleanTogglePatch);

// Patch the list version (Tree/List view)
patch(ListBooleanToggleField.prototype, "sh_ai_base.ListBooleanToggleFieldPatch", {
    async onClick() {
        if (!this.props.readonly) {
            // For ListBooleanToggleField, this.props.value is the current boolean value
            const newValue = !this.props.value;
            await this.onChange(newValue);
        }
    },
    // Spread methods to ensure they are available in the patched component
    ...shAiBooleanTogglePatch,
});