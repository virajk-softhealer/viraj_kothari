/** @odoo-module **/

import { Component, useState, useRef, onMounted, onPatched, onWillUnmount, onWillUpdateProps } from "@odoo/owl";
import { browser } from "@web/core/browser/browser";
import { useService } from "@web/core/utils/hooks";
import { session } from "@web/session";
import { ChatMessageComponent } from "../chat_message/chat_message";
import { AiTypingIndicatorComponent } from "../ai_typing_indicator/ai_typing_indicator";

export class AiChatContentComponent extends Component {
    static template = "sh_ai_assistance.AiChatContentTemplate";
    static components = {
        ChatMessage: ChatMessageComponent,
        AiTypingIndicator: AiTypingIndicatorComponent,
    };
    static props = {
        sessionId: { type: Number },
        sessionToken: { type: String },
        selectedModel: { type: [Object, { value: null }], optional: true },
        availableModels: { type: Array, optional: true },
        currentLlmCompany: { type: [String, { value: null }], optional: true },
        onMessageSent: { type: Function, optional: true },
        onMessageCancelled: { type: Function, optional: true },
        onProcessingStateChanged: { type: Function, optional: true },
    };

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");
        // NOTE: In Odoo 17, bus_service is async:true → cannot use useService().
        // Access it via this.env.services.bus_service at mount time instead.
        this.messagesContainerRef = useRef("messagesContainer");
        this.textInputRef = useRef("textInput");
        this.shouldAutoScroll = false;
        this.lastSessionId = null;
        this._busUnsubscribe = null;

        this.state = useState({
            messages: [],
            currentQuery: "",
            isProcessing: false,
            isFetchLoading: false,
            viewPreference: this.getStoredViewPreference(),
            demoQuestions: [],
            demoModulesUsed: [],
            isLoadingDemoQuestions: false,
            processingStatus: null,
            showShiftEnterHint: false,
            isListening: false,
            isSpeaking: false,
            speechSupported: !!(window.SpeechRecognition || window.webkitSpeechRecognition),
            interimTranscript: "",
        });

        this.recognition = null;

        // Subscribe to bus notifications after the component mounts
        // (bus_service is async in Odoo 17 and available via env.services)
        onMounted(() => {
            const busService = this.env.services.bus_service;
            if (busService && busService.subscribe) {
                busService.subscribe("sh_ai_assistance/processing_status", (notifStatus) => {
                    if (!notifStatus || notifStatus.session_id !== this.props.sessionId) {
                        return;
                    }
                    if (notifStatus.active === false) {
                        this.state.processingStatus = null;
                        return;
                    }
                    this.state.processingStatus = notifStatus;
                });
            }
        });

        // Auto-scroll to bottom when new message is added or session changes
        onPatched(() => {
            // Check if session has changed
            if (this.props.sessionId !== this.lastSessionId) {
                this.lastSessionId = this.props.sessionId;
                this.loadSessionMessages();
                return; // loadSessionMessages will set shouldAutoScroll
            }

            // Normal auto-scroll for new messages
            if (this.shouldAutoScroll) {
                this.scrollToBottom();
                this.shouldAutoScroll = false;
            }
        });

        // Watch for session changes and prepare for auto-scroll
        onWillUpdateProps((nextProps) => {
            if (nextProps.sessionId !== this.props.sessionId) {
                // Session is changing, we'll handle reload in onPatched
                console.log('Session changing from', this.props.sessionId, 'to', nextProps.sessionId);
                // Clear messages immediately so we don't show old ones
                this.state.messages = [];
                this.state.isFetchLoading = true;
                this.state.demoQuestions = [];
                this.state.demoModulesUsed = [];
            }
        });

        onWillUnmount(() => {
            this.state.processingStatus = null;
        });

        // Load session messages initially
        this.loadSessionMessages();
        this.lastSessionId = this.props.sessionId;
    }

    get currentUser() {
        return {
            id: session.uid || 1,
            name: session.partner_display_name || session.name || 'User',
        };
    }

    get providerWarningMessage() {
        const company = this.props.currentLlmCompany;
        if (!company) return null;

        const companyLower = company.toLowerCase();

        if (companyLower.includes('google') || companyLower.includes('gemini')) {
            return "Google AI models may make mistakes, so double-check outputs.";
        } else if (companyLower.includes('openai') || companyLower.includes('open ai') || companyLower.includes('deepseek')) {
            return "ChatGPT models may make mistakes, so double-check outputs.";
        }

        return null;
    }

    get viewPreferenceStorageKey() {
        return `sh_ai_assistance.view_preference.${session.uid || "anonymous"}`;
    }

    getStoredViewPreference() {
        const rawValue = browser.localStorage.getItem(this.viewPreferenceStorageKey);
        if (["auto", "show", "hide"].includes(rawValue)) {
            return rawValue;
        }
        return "auto";
    }

    persistViewPreference(value) {
        browser.localStorage.setItem(this.viewPreferenceStorageKey, value);
    }

    get viewPreferenceHelpText() {
        const labels = {
            auto: "Show open-view buttons only when the result is truncated or clearly needs drill-down.",
            show: "Always attach open-view buttons when there are matching records to inspect.",
            hide: "Hide all open-view buttons and keep the answer in chat only.",
        };
        return labels[this.state.viewPreference] || labels.auto;
    }

    get emptyStateHighlights() {
        return [
            {
                icon: "fa-database",
                title: "Query live Odoo data",
                description: "Ask about records, counts, filters, and grouped summaries from the current database.",
            },
            {
                icon: "fa-check-square-o",
                title: "Verify before you trust",
                description: "AI answers can be backed by executed queries, grouped views, and record-opening actions.",
            },
            {
                icon: "fa-external-link",
                title: "Move from answer to action",
                description: "Open the matching list, kanban, graph, or pivot view directly when records are found.",
            },
        ];
    }

    get suggestedQuestions() {
        if (this.state.demoQuestions.length) {
            return this.state.demoQuestions;
        }
        return [
            "Show contacts grouped by country.",
            "How many active users are in the system?",
            "List unpaid invoices and open the result.",
            "Show the top customers by revenue.",
        ];
    }

    get promptLibrary() {
        return [
            {
                title: "Operations",
                prompts: [
                    "Show contacts grouped by country.",
                    "Which users are active in the system?",
                ],
            },
            {
                title: "Sales",
                prompts: [
                    "How many quotations are waiting?",
                    "Show top customers by revenue.",
                ],
            },
            {
                title: "Finance",
                prompts: [
                    "List unpaid invoices.",
                    "Which customers still owe us money?",
                ],
            },
        ];
    }

    get currentProcessingStatus() {
        return this.state.processingStatus || {
            title: "Working on your request",
            detail: "Waiting for a real backend status update.",
        };
    }

    stopProcessingStatusPolling() {
        this.state.processingStatus = null;
    }

    async loadDemoQuestions() {
        if (!this.props.sessionId || this.state.isLoadingDemoQuestions || this.state.demoQuestions.length) {
            return;
        }

        this.state.isLoadingDemoQuestions = true;
        try {
            const result = await this.orm.call("sh.ai.chat.session", "get_demo_questions", [[this.props.sessionId]]);
            if (result?.success && Array.isArray(result.questions)) {
                this.state.demoQuestions = result.questions;
                this.state.demoModulesUsed = result.modules_used || [];
            }
        } catch (error) {
            console.warn("Failed to load demo questions:", error);
        } finally {
            this.state.isLoadingDemoQuestions = false;
        }
    }

    async loadSessionMessages() {
        if (!this.props.sessionId) {
            this.state.messages = [];
            this.state.isFetchLoading = false;
            return;
        }

        try {
            this.state.isFetchLoading = true;
            const messages = await this.orm.searchRead("sh.ai.chat.message",
                [["session_id", "=", this.props.sessionId]],
                [
                    "message_type",
                    "content",
                    "create_date",
                    "llm_id",
                    "write_request_id",
                    "action_data",
                    "debug_info",
                    "query_details",
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                    "tool_call_count",
                    "execution_time",
                    "sh_is_cancelled",
                    "sh_reasoning_tokens",
                    "sh_reasoning_effort",
                ],
                { order: "create_date asc" }
            );

            // Load LLM details for AI-generated messages, including failed turns.
            const llmIds = messages
                .filter(m => m.llm_id && ['assistant', 'error'].includes(m.message_type))
                .map(m => m.llm_id[0]);
            let llmDetails = {};
            let writeRequestDetails = {};

            if (llmIds.length > 0) {
                const uniqueLlmIds = [...new Set(llmIds)];
                const llms = await this.orm.searchRead(
                    "sh.ai.llm",
                    [['id', 'in', uniqueLlmIds]],
                    ['id', 'name', 'sh_company', 'image']
                );

                // Create lookup map
                llmDetails = llms.reduce((acc, llm) => {
                    acc[llm.id] = llm;
                    return acc;
                }, {});
            }

            const modelNames = [...new Set(messages.flatMap((message) => {
                const names = [];
                const actionModel = message.action_data?.model;
                const queryModel = message.query_details?.model;
                const finalQueryModel = message.debug_info?.final_query?.model;

                if (actionModel && !message.action_data?.model_display_name) {
                    names.push(actionModel);
                }
                if (queryModel && !message.query_details?.model_display_name) {
                    names.push(queryModel);
                }
                if (finalQueryModel && !message.debug_info?.final_query?.model_display_name) {
                    names.push(finalQueryModel);
                }

                return names;
            }))];

            let modelDisplayNames = {};
            if (modelNames.length) {
                modelDisplayNames = await this.orm.call(
                    "sh.ai.chat.message",
                    "get_model_display_names",
                    [modelNames]
                );
            }

            const writeRequestIds = [...new Set(
                messages
                    .filter((message) => message.write_request_id)
                    .map((message) => message.write_request_id[0])
            )];
            if (writeRequestIds.length) {
                const requests = await this.orm.searchRead(
                    "sh.ai.write.request",
                    [["id", "in", writeRequestIds]],
                    [
                        "operation",
                        "state",
                        "summary",
                        "target_model",
                        "target_model_display_name",
                        "preview_json",
                        "result_json",
                        "action_data",
                        "failure_reason",
                        "requester_id",
                        "approver_id",
                        "approved_at",
                        "executed_at",
                        "rejected_at",
                        "create_date",
                    ]
                );
                writeRequestDetails = requests.reduce((acc, request) => {
                    acc[request.id] = request;
                    return acc;
                }, {});
            }

            // Map messages to include LLM details
            this.state.messages = messages.map(m => ({
                ...m,
                action_data: m.action_data?.model && !m.action_data?.model_display_name
                    ? {
                        ...m.action_data,
                        model_display_name: modelDisplayNames[m.action_data.model] || m.action_data.model,
                    }
                    : m.action_data,
                query_details: m.query_details?.model && !m.query_details?.model_display_name
                    ? {
                        ...m.query_details,
                        model_display_name: modelDisplayNames[m.query_details.model] || m.query_details.model,
                    }
                    : m.query_details,
                debug_info: m.debug_info?.final_query?.model && !m.debug_info?.final_query?.model_display_name
                    ? {
                        ...m.debug_info,
                        final_query: {
                            ...m.debug_info.final_query,
                            model_display_name: modelDisplayNames[m.debug_info.final_query.model] || m.debug_info.final_query.model,
                        },
                    }
                    : m.debug_info,
                write_request: m.write_request_id ? writeRequestDetails[m.write_request_id[0]] || null : null,
                llm: m.llm_id ? llmDetails[m.llm_id[0]] || null : null,
                llm_details: m.llm_id ? llmDetails[m.llm_id[0]] || null : null,
            }));

            if (messages.length > 0) {
                this.shouldAutoScroll = true;
            } else {
                this.loadDemoQuestions();
            }
        } catch (error) {
            console.error("Failed to load session messages:", error);
            this.notification.add("Failed to load messages", { type: "danger" });
        } finally {
            this.state.isFetchLoading = false;
        }
    }

    async stopProcessing() {
        if (!this.state.isProcessing) return;
        const sessionId = this.props.sessionId;
        try {
            await this.orm.call("sh.ai.chat.message", "cancel_last_user_message", [sessionId]);
        } catch (error) {
            console.error("Failed to stop processing:", error);
        }
        this.notification.add("Processing stopped", { type: "info" });
        this.state.isProcessing = false;
        this.stopProcessingStatusPolling();
        await this.loadSessionMessages();
        if (this.props.onMessageCancelled) {
            this.props.onMessageCancelled();
        }
    }

    async sendMessage() {
        if (!this.state.currentQuery.trim() || this.state.isProcessing) {
            return;
        }

        if (!this.props.sessionId) {
            console.error("No session ID available for sending message");
            this.notification.add("No active session. Please create a new conversation.", { type: "warning" });
            return;
        }

        const userMessage = this.state.currentQuery.trim();
        this.state.currentQuery = "";
        this.state.isProcessing = true;
        this.state.processingStatus = {
            title: "Starting request",
            detail: "Waiting for the backend to begin processing.",
        };
        this.shouldAutoScroll = true;

        // Notify parent about processing state change
        if (this.props.onProcessingStateChanged) {
            this.props.onProcessingStateChanged(true);
        }

        // Scroll to bottom when typing indicator appears
        setTimeout(() => {
            this.scrollToBottom();
        }, 100);

        try {
            console.log('Sending message with sessionId:', this.props.sessionId);

            // Create user message
            const newUserMsg = await this.orm.create("sh.ai.chat.message", [{
                session_id: this.props.sessionId,
                message_type: "user",
                content: userMessage,
            }]);
            const messageId = newUserMsg[0];

            // Reload messages immediately to show user message
            await this.loadSessionMessages();

            // Call backend to process message with Gemini API
            try {
                const result = await this.orm.call(
                    "sh.ai.chat.message",
                    "process_user_message",
                    [this.props.sessionId, userMessage, messageId, this.state.viewPreference]
                );

                if (result.error) {
                    if (result.error !== 'Processing was cancelled') {
                        this.notification.add(result.error, { type: "danger" });
                    }
                }

                // Reload messages
                await this.loadSessionMessages();

                if (this.props.onMessageSent) {
                    this.props.onMessageSent();
                }
            } catch (error) {
                console.error("Failed to get AI response:", error);
                this.notification.add("Failed to get AI response: " + error.message, { type: "danger" });
            } finally {
                this.state.isProcessing = false;
                this.stopProcessingStatusPolling();
                this.shouldAutoScroll = true;

                // Notify parent about processing state change
                if (this.props.onProcessingStateChanged) {
                    this.props.onProcessingStateChanged(false);
                }
            }

        } catch (error) {
            console.error("Failed to send message:", error);

            let errorMessage = "Failed to send message";
            if (error.message) {
                errorMessage += ": " + error.message;
            }

            this.notification.add(errorMessage, { type: "danger" });
            this.state.isProcessing = false;
            this.stopProcessingStatusPolling();

            // Notify parent about processing state change
            if (this.props.onProcessingStateChanged) {
                this.props.onProcessingStateChanged(false);
            }
        }
    }

    onKeyDown(event) {
        if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            this.sendMessage();
        }
    }

    scrollToBottom() {
        if (this.messagesContainerRef.el) {
            this.messagesContainerRef.el.scrollTop = this.messagesContainerRef.el.scrollHeight;
        }
    }

    onInput(event) {
        this.state.currentQuery = event.target.value;
    }

    onViewPreferenceChange(event) {
        const nextValue = event.target.value;
        if (!["auto", "show", "hide"].includes(nextValue)) {
            return;
        }
        this.state.viewPreference = nextValue;
        this.persistViewPreference(nextValue);
    }

    onInputChange(event) {
        this.state.currentQuery = event.target.value;
        this.state.showShiftEnterHint = this.state.currentQuery.includes("\n");
    }

    onKeyPress(event) {
        if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            this.sendMessage();
        }
    }

    selectDemoQuestion(question) {
        this.state.currentQuery = question;
        this.state.showShiftEnterHint = false;
        if (this.textInputRef.el) {
            this.textInputRef.el.focus();
        }
    }

    initSpeechRecognition() {
        const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
        if (!SpeechRecognition) return null;

        const recognition = new SpeechRecognition();
        recognition.continuous = false;
        recognition.interimResults = true;

        // Map Odoo language to BCP 47
        const lang = session.user_context?.lang || "en_US";
        recognition.lang = lang.replace("_", "-");

        recognition.onstart = () => {
            this.state.isListening = true;
            this.state.interimTranscript = this.state.currentQuery;
        };

        recognition.onresult = (event) => {
            let finalTranscript = "";
            let interimTranscript = "";

            for (let i = event.resultIndex; i < event.results.length; ++i) {
                if (event.results[i].isFinal) {
                    finalTranscript += event.results[i][0].transcript;
                } else {
                    interimTranscript += event.results[i][0].transcript;
                }
            }

            if (finalTranscript) {
                this.state.currentQuery += (this.state.currentQuery ? " " : "") + finalTranscript;
            }
            this.state.interimTranscript = interimTranscript;

            if (interimTranscript) {
                this.state.isSpeaking = true;
                clearTimeout(this.speakingTimeout);
                this.speakingTimeout = setTimeout(() => {
                    this.state.isSpeaking = false;
                }, 500);
            }
        };

        recognition.onerror = (event) => {
            console.error("Speech Recognition Error:", event.error);
            this.state.isListening = false;
            let message = "Speech recognition error";
            if (event.error === 'not-allowed') {
                message = "Microphone access denied. Please check your browser settings.";
            } else if (event.error === 'no-speech') {
                message = "No speech detected. Please try again.";
            }
            this.notification.add(message, { type: "warning" });
        };

        recognition.onend = () => {
            this.state.isListening = false;
            // Re-focus textarea so Enter key can immediately send the message
            setTimeout(() => {
                if (this.textInputRef.el) {
                    this.textInputRef.el.focus();
                }
            }, 50);
        };

        return recognition;
    }

    toggleSpeechRecognition() {
        if (this.state.isListening) {
            if (this.recognition) {
                this.recognition.stop();
            }
        } else {
            this.state.currentQuery = ""; // Reset input text on new recording
            this.state.interimTranscript = "";
            if (!this.recognition) {
                this.recognition = this.initSpeechRecognition();
            }
            if (this.recognition) {
                try {
                    this.recognition.start();
                } catch (error) {
                    console.error("Failed to start speech recognition:", error);
                    this.state.isListening = false;
                }
            }
        }
        // Always return focus to the input box so "Enter" sends the message
        if (this.textInputRef.el) {
            this.textInputRef.el.focus();
        }
    }

    discardSpeech() {
        if (this.recognition) {
            this.recognition.stop();
        }
        this.state.currentQuery = "";
        this.state.interimTranscript = "";
        this.state.isListening = false;
    }

    async onWriteRequestChanged() {
        await this.loadSessionMessages();
        this.shouldAutoScroll = true;
    }
}
