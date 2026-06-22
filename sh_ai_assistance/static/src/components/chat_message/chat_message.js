/** @odoo-module **/

import { Component, markup, useState } from "@odoo/owl";
import { deserializeDateTime } from "@web/core/l10n/dates";
import { useService } from "@web/core/utils/hooks";

export class ChatMessageComponent extends Component {
    static template = "sh_ai_assistance.ChatMessageTemplate";
    static props = {
        message: { type: Object },
        messageIndex: { type: Number, optional: true },
        currentUser: { type: Object, optional: true },
        currentLlm: { type: [Object, { value: null }], optional: true },
        availableModels: { type: Array, optional: true },
        onWriteRequestChanged: { type: Function, optional: true },
    };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.userService = useService("user");
        this.state = useState({
            showDebugInfo: false,
            writeActionPending: false,
        });
    }

    get isDebugMode() {
        return !!this.env.debug;
    }

    get isAdmin() {
        return this.userService.isAdmin || false;
    }

    get hasDebugInfo() {
        return (
            this.props.message.message_type === 'assistant' &&
            (this.props.message.query_details ||
                this.turnEvents.length ||
                (this.props.message.debug_info && this.props.message.debug_info.final_query))
        );
    }

    get turnEvents() {
        return this.props.message.debug_info?.turn_events || [];
    }

    get providerCapabilities() {
        return this.props.message.debug_info?.provider_capabilities || {};
    }

    get promptCache() {
        return this.props.message.debug_info?.prompt_cache || {};
    }

    get promptCacheStrategyLabel() {
        const strategy = this.promptCache.metadata?.cache_strategy || {};
        const company = (this.props.message.llm_details?.sh_company || "").toLowerCase();
        if (company === "google") {
            return strategy.gemini || "implicit";
        }
        if (company === "openai") {
            return strategy.openai || "implicit";
        }
        return strategy.openai || strategy.gemini || "implicit";
    }

    get toolLifecycleEvents() {
        return this.turnEvents.filter((event) =>
            ['tool.called', 'tool.completed', 'tool.failed', 'turn.failed', 'turn.cancelled', 'turn.completed', 'write.proposed', 'write.approved', 'write.executing', 'write.completed', 'write.rejected', 'write.failed'].includes(event.type)
        );
    }

    get eventBadges() {
        const turnCompleted = this.turnEvents.find((event) => event.type === 'turn.completed');
        const turnCancelled = this.turnEvents.find((event) => event.type === 'turn.cancelled');
        const turnFailed = this.turnEvents.find((event) => event.type === 'turn.failed');
        const completedTools = this.turnEvents.filter((event) => event.type === 'tool.completed').length;
        const failedTools = this.turnEvents.filter((event) => event.type === 'tool.failed').length;
        const badges = [];

        if (turnCompleted) {
            badges.push({ label: 'Completed', className: 'success' });
        }
        if (turnCancelled) {
            badges.push({ label: 'Cancelled', className: 'warning' });
        }
        if (turnFailed) {
            badges.push({ label: 'Failed', className: 'danger' });
        }
        if (completedTools) {
            badges.push({ label: `${completedTools} tool${completedTools > 1 ? 's' : ''}`, className: 'info' });
        }
        if (failedTools) {
            badges.push({ label: `${failedTools} failed`, className: 'danger' });
        }
        return badges;
    }

    get isToolVerified() {
        return this.turnEvents.some((event) => event.type === "tool.completed");
    }

    get evidenceSource() {
        return this.props.message.action_data || this._activeQuery || null;
    }

    formatTechnicalLabel(value) {
        if (!value) {
            return "";
        }
        return String(value)
            .replace(/_/g, " ")
            .replace(/\s+/g, " ")
            .trim();
    }

    get evidenceModelLabel() {
        const displayName = this.evidenceSource?.model_display_name;
        if (displayName) {
            return String(displayName).trim();
        }
        const model = this.evidenceSource?.model;
        return model ? String(model).trim() : "";
    }

    get evidenceGroupByLabel() {
        return this.formatTechnicalLabel(this.evidenceSource?.group_by);
    }

    get evidenceOperationLabel() {
        const operation = this._activeQuery?.operation;
        const labels = {
            search: "record lookup",
            count: "count",
            sum: "sum",
            avg: "average",
            min: "minimum",
            max: "maximum",
        };
        return labels[operation] || (operation ? this.formatTechnicalLabel(operation) : "");
    }

    get evidenceRecordCount() {
        const totalCount = this.props.message.action_data?.total_count;
        if (typeof totalCount === "number") {
            return totalCount;
        }
        for (const event of [...this.toolLifecycleEvents].reverse()) {
            const count = event.result?.count;
            if (typeof count === "number") {
                return count;
            }
        }
        return null;
    }

    get hasEvidence() {
        return this.props.message.message_type === "assistant" && !!(this.evidenceModelLabel || this.evidenceGroupByLabel || this.evidenceRecordCount !== null);
    }

    get writeRequest() {
        return this.props.message.write_request || null;
    }

    get hasWriteRequest() {
        return this.props.message.message_type === "assistant" && !!this.writeRequest;
    }

    get writePreview() {
        return this.writeRequest?.preview_json || {};
    }

    get writeResult() {
        return this.writeRequest?.result_json || {};
    }

    get writeRiskBadges() {
        return this.writePreview.risk_badges || [];
    }

    get writeFieldDiffs() {
        return this.writePreview.field_diffs || [];
    }

    get writeSampleRecords() {
        return (this.writePreview.sample_records || []).slice(0, 3);
    }

    get writeRequestStateLabel() {
        const labels = {
            pending: "Pending approval",
            approved: "Approved",
            executing: "Executing",
            done: "Completed",
            rejected: "Rejected",
            failed: "Failed",
            expired: "Expired",
        };
        return labels[this.writeRequest?.state] || "Pending";
    }

    get writeRequestTitle() {
        if (!this.writeRequest) {
            return "";
        }
        const operation = this.writePreview.operation_label || this.writeRequest.operation || "Write";
        const modelName = this.writeRequest.target_model_display_name || this.writePreview.model_display_name || this.writeRequest.target_model || "records";
        return `${operation} ${modelName}`;
    }

    get writeRequestSummary() {
        return this.writeRequest?.summary || this.writePreview.summary || "";
    }

    get writeResultMessage() {
        return this.writeResult?.message || "";
    }

    get writeFailureMessage() {
        return this.writeRequest?.failure_reason || this.writeResult?.message || "";
    }

    get canApproveWriteRequest() {
        return this.writeRequest?.state === "pending" && !this.state.writeActionPending;
    }

    get canRejectWriteRequest() {
        return this.writeRequest?.state === "pending" && !this.state.writeActionPending;
    }

    get canOpenWriteRequestRecords() {
        if (!this.writeRequest) {
            return false;
        }
        if (this.writeRequest.state === "done") {
            return !!(this.writeResult?.action_data || this.writeRequest.action_data);
        }
        return !!this.writeRequest.action_data;
    }

    get writeOpenButtonLabel() {
        if (this.writeRequest?.operation === "create" && this.writeRequest?.state === "done") {
            return "Open created records";
        }
        return "Open records";
    }

    get evidenceItems() {
        const items = [];
        if (this.isToolVerified) {
            items.push("Tool-verified");
        }
        if (this.evidenceModelLabel) {
            items.push(`Based on ${this.evidenceModelLabel}`);
        }
        if (this.evidenceOperationLabel && this.evidenceOperationLabel !== "record lookup") {
            items.push(this.evidenceOperationLabel);
        }
        if (this.evidenceGroupByLabel) {
            items.push(`Grouped by ${this.evidenceGroupByLabel}`);
        }
        if (this.evidenceRecordCount !== null) {
            items.push(`${this.evidenceRecordCount} matching record${this.evidenceRecordCount === 1 ? "" : "s"}`);
        }
        return items;
    }

    get actionButtons() {
        if (!this.props.message.action_data) {
            return [];
        }
        const hasGrouping = !!this.props.message.action_data.group_by || !!this._activeQuery?.group_by;
        const buttons = [
            { key: "list", label: "Open records", icon: "fa-list-ul", className: "btn btn-outline-primary" },
            { key: "kanban", label: "Kanban", icon: "fa-th-large", className: "btn btn-outline-secondary" },
        ];
        if (hasGrouping || this._activeQuery?.operation) {
            buttons.push({ key: "graph", label: "Graph", icon: "fa-bar-chart", className: "btn btn-outline-secondary" });
            buttons.push({ key: "pivot", label: "Pivot", icon: "fa-table", className: "btn btn-outline-secondary" });
        }
        return buttons;
    }

    formatEventType(type) {
        return (type || '').replace(/\./g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
    }

    eventSummary(event) {
        if (event.type === 'tool.called') {
            const warnings = event.analysis?.warnings || [];
            if (warnings.length) {
                return warnings.map((warning) => warning.code).join(", ");
            }
            const reasoningTags = event.analysis?.reasoning_tags || [];
            if (reasoningTags.length) {
                return reasoningTags.join(", ");
            }
        }
        if (event.result?.user_safe_summary) {
            return event.result.user_safe_summary;
        }
        if (event.error) {
            return event.error;
        }
        if (event.reason) {
            return event.reason;
        }
        if (event.type && event.type.startsWith("write.")) {
            return event.operation || event.type;
        }
        if (event.tool_name) {
            return event.tool_name;
        }
        return '';
    }

    get _activeQuery() {
        if (this.props.message.query_details) {
            return this.props.message.query_details;
        }
        if (this.props.message.debug_info && this.props.message.debug_info.final_query) {
            return this.props.message.debug_info.final_query;
        }
        return null;
    }

    get debugDomain() {
        return this._activeQuery?.domain || [];
    }

    get debugDomainFormatted() {
        return JSON.stringify(this.debugDomain, null, 2);
    }

    get debugModel() {
        return this._activeQuery?.model || '';
    }

    get debugFields() {
        return this._activeQuery?.fields || [];
    }

    get debugFieldsFormatted() {
        return JSON.stringify(this.debugFields, null, 2);
    }

    get usagePromptTokens() {
        return this.props.message.prompt_tokens || 0;
    }

    get usageCompletionTokens() {
        return this.props.message.completion_tokens || 0;
    }

    get usageTotalTokens() {
        return this.props.message.total_tokens || 0;
    }

    get usageReasoningTokens() {
        return this.props.message.sh_reasoning_tokens || 0;
    }

    get reasoningEffortLabel() {
        const effort = this.props.message.sh_reasoning_effort;
        if (!effort) return null;
        return effort.charAt(0).toUpperCase() + effort.slice(1);
    }

    get toolCallCount() {
        return this.props.message.tool_call_count || 0;
    }

    get executionTime() {
        const time = this.props.message.execution_time || 0;
        return time.toFixed(2);
    }

    get debugGroupBy() {
        return this._activeQuery?.group_by || "NO GROUPBY APPLIED";
    }

    get debugOperation() {
        return this._activeQuery?.operation || "NO OPERATION PERFORMED";
    }

    toggleDebugInfo() {
        this.state.showDebugInfo = !this.state.showDebugInfo;
    }

    get authorName() {
        const modelName =
            this.props.message.llm_details?.name ||
            this.props.message.llm?.name ||
            this.props.currentLlm?.name ||
            'AI';

        switch (this.props.message.message_type) {
            case 'user': return this.props.currentUser?.name || 'You';
            case 'assistant':
                return modelName;
            case 'system': return 'System';
            case 'error':
                return modelName;
            default: return 'Message';
        }
    }

    get avatarUrl() {
        if (this.props.message.message_type === 'user' && this.props.currentUser?.id) {
            return `/web/image/res.users/${this.props.currentUser.id}/avatar_128`;
        }

        if (this.props.message.message_type === 'assistant' || (this.props.message.message_type === 'error' && this.props.message.llm_id)) {
            if (this.props.message.llm_details && this.props.message.llm_details.image) {
                return `/web/image/sh.ai.llm/${this.props.message.llm_details.id}/image`;
            }

            if (this.props.currentLlm?.id && this.props.currentLlm?.image) {
                return `/web/image/sh.ai.llm/${this.props.currentLlm.id}/image`;
            }
        }

        return 'data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iMzYiIGhlaWdodD0iMzYiIHZpZXdCb3g9IjAgMCAzNiAzNiIgZmlsbD0ibm9uZSIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj4KPHJlY3Qgd2lkdGg9IjM2IiBoZWlnaHQ9IjM2IiByeD0iMTgiIGZpbGw9IiM2MzY2RjEiLz4KPHN2ZyB4PSI2IiB5PSI2IiB3aWR0aD0iMjQiIGhlaWdodD0iMjQiIGZpbGw9IndoaXRlIj4KPHN2ZyB3aWR0aD0iMjQiIGhlaWdodD0iMjQiIHZpZXdCb3g9IjAgMCAyNCAyNCIgZmlsbD0ibm9uZSIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj4KPHBhdGggZD0iTTEyIDJMMTMuMDkgOC4yNkwyMCA5TDEzLjA5IDE1Ljc0TDEyIDIyTDEwLjkxIDE1Ljc0TDQgOUwxMC45MSA4LjI2TDEyIDJaIiBmaWxsPSJjdXJyZW50Q29sb3IiLz4KPC9zdmc+Cjwvc3ZnPgo8L3N2Zz4K';
    }

    get richContent() {
        if (this.props.message.message_type === 'assistant' && this.props.message.content) {
            try {
                const html = window.marked.parse(this.props.message.content);
                return markup(html);
            } catch (error) {
                console.error('Failed to parse markdown:', error);
                return markup(this.props.message.content);
            }
        }
        return this.props.message.content || "";
    }

    get timestamp() {
        if (this.props.message.create_date) {
            try {
                return deserializeDateTime(this.props.message.create_date);
            } catch (error) {
                console.warn('Failed to deserialize datetime:', error);
                return new Date(this.props.message.create_date);
            }
        }
        return new Date();
    }

    get formattedTime() {
        const date = this.timestamp;
        const now = new Date();
        const diffMinutes = Math.floor((now - date) / (1000 * 60));

        if (!this.props.message.create_date) {
            return 'Now';
        }

        if (diffMinutes < 0) {
            return 'Just now';
        } else if (diffMinutes < 1) {
            return 'Just now';
        } else if (diffMinutes < 60) {
            return `${diffMinutes}m ago`;
        } else if (diffMinutes < 1440) {
            const hours = Math.floor(diffMinutes / 60);
            return `${hours}h ago`;
        } else {
            return date.toLocaleString(undefined, {
                month: 'short',
                day: 'numeric',
                hour: '2-digit',
                minute: '2-digit'
            });
        }
    }

    get fullTimestamp() {
        return this.timestamp.toLocaleString(undefined, {
            year: 'numeric',
            month: 'long',
            day: 'numeric',
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
            timeZoneName: 'short'
        });
    }

    onChartTypeChange(chartType) {
        if (this.props.onChartTypeChange) {
            this.props.onChartTypeChange(chartType, this.props.messageIndex);
        }
    }

    async onOpenView(viewType) {
        try {
            const action = await this.orm.call(
                "sh.ai.chat.message",
                "open_records_action",
                [this.props.message.id, viewType]
            );

            if (!action || !action.res_model) {
                console.error("Invalid action received:", action);
                return;
            }

            this.action.doAction(action);
        } catch (error) {
            console.error(`Error opening ${viewType} view:`, error);
        }
    }

    async approveWriteRequest() {
        if (!this.writeRequest) {
            return;
        }
        this.state.writeActionPending = true;
        try {
            const result = await this.orm.call(
                "sh.ai.write.request",
                "action_approve_from_chat",
                [[this.writeRequest.id]]
            );
            if (result?.success) {
                this.notification.add("Write request executed successfully.", {
                    type: "success",
                });
            } else if (result?.error) {
                this.notification.add(result.error, { type: "danger" });
            }
        } catch (error) {
            console.error("Failed to approve write request:", error);
            this.notification.add("Failed to execute the write request.", { type: "danger" });
        } finally {
            this.state.writeActionPending = false;
            if (this.props.onWriteRequestChanged) {
                await this.props.onWriteRequestChanged();
            }
        }
    }

    async rejectWriteRequest() {
        if (!this.writeRequest) {
            return;
        }
        this.state.writeActionPending = true;
        try {
            const result = await this.orm.call(
                "sh.ai.write.request",
                "action_reject_from_chat",
                [[this.writeRequest.id]]
            );
            if (result?.success) {
                this.notification.add("Write request rejected.", {
                    type: "warning",
                });
            } else if (result?.error) {
                this.notification.add(result.error, { type: "danger" });
            }
        } catch (error) {
            console.error("Failed to reject write request:", error);
            this.notification.add("Failed to reject the write request.", { type: "danger" });
        } finally {
            this.state.writeActionPending = false;
            if (this.props.onWriteRequestChanged) {
                await this.props.onWriteRequestChanged();
            }
        }
    }

    async openWriteRequestRecords() {
        if (!this.writeRequest) {
            return;
        }
        try {
            const action = await this.orm.call(
                "sh.ai.write.request",
                "action_open_target_records",
                [[this.writeRequest.id], "list"]
            );
            if (action?.res_model) {
                this.action.doAction(action);
            }
        } catch (error) {
            console.error("Failed to open write request records:", error);
            this.notification.add("Failed to open the target records.", { type: "danger" });
        }
    }
}
