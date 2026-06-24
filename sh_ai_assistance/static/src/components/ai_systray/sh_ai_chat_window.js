/** @odoo-module **/

import { Component, useState, onMounted } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { AiChatWindowContentComponent } from "./sh_ai_chat_window_content";

export class AiChatWindow extends Component {
    static template = "sh_ai_assistance.AiChatWindow";
    static components = {
        AiChatWindowContent: AiChatWindowContentComponent,
    };
    static props = {
        onClose: { type: Function },
    };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({
            currentSessionToken: null,
            currentSessionId: null,
            currentSessionName: "New Chat",
            isLoading: true,
            availableModels: [],
            selectedModel: null,
            recentSessions: [],
        });

        onMounted(async () => {
            await this.loadInitialData();
        });
    }

    async loadInitialData() {
        this.state.isLoading = true;
        try {
            // Fetch everything in one backend call for speed and reliability
            const data = await this.orm.call("sh.ai.chat.session", "get_initial_data", []);

            this.state.availableModels = data.models;
            this.state.recentSessions = data.sessions;

            if (this.state.recentSessions.length > 0) {
                // Select the most recent session that has messages, or the first session if all are empty
                // const sessionWithMessages = this.state.recentSessions.find(s => s.message_count > 0);
                const chat_session =  this.state.recentSessions[0];
                this.state.currentSessionId = chat_session.id;
                this.state.currentSessionToken = chat_session.access_token;
                this.state.currentSessionName = chat_session.name;

                // Set model from session if possible
                if (chat_session.llm_id) {
                    const modelId = chat_session.llm_id[0];
                    this.state.selectedModel = data.models.find(m => m.id === modelId) || data.models[0];
                } else {
                    this.state.selectedModel = data.models[0];
                }
            } else {
                // Create new session if none exists
                await this.createNewSession();
            }
        } catch (error) {
            console.error("Failed to load AI Chat data:", error);
        } finally {
            this.state.isLoading = false;
        }
    }

    async loadRecentSessions() {
        // Fetch fresh session list from backend
        try {
            // Fetch everything in one backend call for speed and reliability
            const data = await this.orm.call("sh.ai.chat.session", "get_initial_data", []);

            this.state.availableModels = data.models;
            this.state.recentSessions = data.sessions;

            if (this.state.recentSessions.length > 0) {
                // Only update current session if it's not already set or if the current session no longer exists
                const currentSessionExists = this.state.recentSessions.find(s => s.id === this.state.currentSessionId);
                if (!currentSessionExists || !this.state.currentSessionId) {
                    // Select the most recent session that has messages, or the first session if all are empty
                    const sessionWithMessages = this.state.recentSessions.find(s => s.message_count > 0);
                    const chat_session = sessionWithMessages || this.state.recentSessions[0];
                    this.state.currentSessionId = chat_session.id;
                    this.state.currentSessionToken = chat_session.access_token;
                    this.state.currentSessionName = chat_session.name;

                    // Set model from session if possible
                    if (chat_session.llm_id) {
                        const modelId = chat_session.llm_id[0];
                        this.state.selectedModel = data.models.find(m => m.id === modelId) || data.models[0];
                    } else {
                        this.state.selectedModel = data.models[0];
                    }
                } else {
                    // Refresh the name in case the backend auto-generated it after the first message
                    this.state.currentSessionName = currentSessionExists.name;
                }
            }
        } catch (error) {
            console.error("Failed to load recent sessions:", error);
        }
    }

    async createNewSession() {
        try {
            // Rely on Python model default (env.user) to set the user_id correctly
            const sessionIds = await this.orm.create("sh.ai.chat.session", [{
                'name': 'New Chat',
            }]);
            const newSessions = await this.orm.read("sh.ai.chat.session", sessionIds, ['id', 'access_token', 'name']);
            const newSession = newSessions[0];
            this.state.currentSessionId = newSession.id;
            this.state.currentSessionToken = newSession.access_token;
            this.state.currentSessionName = newSession.name;
            if (this.state.availableModels.length > 0) {
                this.state.selectedModel = this.state.availableModels.find(m => m.is_default) || this.state.availableModels[0];
            }
            await this.loadRecentSessions();
        } catch (error) {
            console.error("Failed to create new session:", error);
        }
    }

    async selectSession(chat_session) {
        if (chat_session.id === this.state.currentSessionId) return;

        this.state.currentSessionId = chat_session.id;
        this.state.currentSessionToken = chat_session.access_token;
        this.state.currentSessionName = chat_session.name;

        // Update model to match this session
        if (chat_session.llm_id) {
            const modelId = chat_session.llm_id[0];
            this.state.selectedModel = this.state.availableModels.find(m => m.id === modelId) || this.state.selectedModel;
        }
    }

    get shouldDisableNewChat() {
        if (this.state.isLoading) {
            return true;
        }

        const emptySession = this.state.recentSessions.find(s => s.message_count === 0);
        return !!emptySession;
    }

    get newChatButtonTooltip() {
        if (this.state.isLoading) {
            return "Creating new chat...";
        }

        const emptySession = this.state.recentSessions.find(s => s.message_count === 0);
        if (emptySession) {
            return "You already have an empty chat. Use it before creating a new one.";
        }

        return "Create a new chat session";
    }

    async selectModel(model) {
        this.state.selectedModel = model;
        if (this.state.currentSessionId) {
            try {
                await this.orm.write("sh.ai.chat.session", [this.state.currentSessionId], {
                    llm_id: model.id
                });
            } catch (error) {
                console.error("Failed to update model for session:", error);
            }
        }
    }

    onExpand() {
        
        const sessionParam = this.state.currentSessionToken ? `?session=${this.state.currentSessionToken}` : "";
        window.location.href = `/odoo/ai-chat${sessionParam}`;
    }

    onMessageSent() {
        // Sync recent sessions list to update names or order after new messages
        this.loadRecentSessions();
    }

    onProcessingStateChanged(isProcessing) {
        this.state.isProcessing = isProcessing;
    }
}
