/** @odoo-module **/

import { Component, useState, onMounted, onWillUnmount } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { registry } from "@web/core/registry";
import { session } from "@web/session";
import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { AiChatSidebarComponent } from "../ai_chat_sidebar/ai_chat_sidebar";
import { AiChatMainComponent } from "../ai_chat_main/ai_chat_main";
import { AiOnboardingWizard } from "../ai_onboarding_wizard/ai_onboarding_wizard";
import {
    buildChatProjectTree,
    getProjectAncestors,
    getProjectDescendantIds,
    toggleProjectExpanded,
} from "../shared/ai_chat_project_store";

export class AiChatAppComponent extends Component {
    static template = "sh_ai_assistance.AiChatAppTemplate";
    static components = {
        AiChatSidebar: AiChatSidebarComponent,
        AiChatMain: AiChatMainComponent,
        AiOnboardingWizard,
    };
    static props = {
        "*": true, // Accept any additional props from Odoo action system
    };

    setup() {
        this.orm = useService("orm");
        this.dialog = useService("dialog");
        this.notification = useService("notification");
        this.user = useService("user");
        this.urlMonitorInterval = null;

        // --- SCENARIO A: Component is re-mounted (navigating from another Odoo page) ---
        const navEntry = performance.getEntriesByType("navigation")[0];
        this._isFreshNavigation = !navEntry || navEntry.type !== "reload";
        this._lastSeenHash = window.location.hash;

        this.state = useState({
            sidebarCollapsed: false,
            currentSessionToken: null,
            sessions: [],
            isLoading: false,
            projectState: {
                projects: [],
                sessionProjectMap: {},
            },
            // Onboarding state
            onboardingChecked: false,
            showOnboarding: false,
            showBlocker: false,
            onboardingState: null,
        });

        // Load sessions and check current URL for session token
        this.initializeApp();

        // Monitor URL integrity to ensure session parameter stays
        onMounted(() => {
            this.startUrlMonitoring();
            
            // --- SCENARIO B: Same action re-triggered while component is already mounted ---
            this._onHashChange = () => {
                const newHash = window.location.hash;
                if (newHash === this._lastSeenHash) {
                    return;
                }
                this._lastSeenHash = newHash;

                if (this.state.currentSessionToken) {
                    console.log('[AI Chat] Hash changed while mounted — resetting to Welcome screen.');
                    this.clearUrlSession();
                    this._isFreshNavigation = true;
                }
            };
            window.addEventListener('hashchange', this._onHashChange);
        });

        onWillUnmount(() => {
            if (this._onHashChange) {
                window.removeEventListener('hashchange', this._onHashChange);
            }
            this.stopUrlMonitoring();
            
            // Clear the ?session= from the URL when navigating away from AI Chat.
            const url = new URL(window.location);
            if (url.searchParams.has('session')) {
                url.searchParams.delete('session');
                window.history.replaceState({}, '', url);
            }
        });
    }

    get currentUser() {
        return {
            id: session.uid || this.user.userId,
            name: session.partner_display_name || this.user.name || 'User',
        };
    }

    get currentLlmCompany() {
        // Get LLM company from the current session
        if (this.state.currentSessionToken) {
            const currentSession = this.state.sessions.find(s => s.access_token === this.state.currentSessionToken);
            if (currentSession && currentSession.llm_details) {
                return currentSession.llm_details.sh_company;
            }
        }
        return null;
    }

    get projectTreeData() {
        return buildChatProjectTree(this.state.sessions, this.state.projectState);
    }

    get currentSessionRecord() {
        if (!this.state.currentSessionToken) {
            return null;
        }
        return this.state.sessions.find((sessionRecord) => sessionRecord.access_token === this.state.currentSessionToken) || null;
    }

    get currentSessionProjectId() {
        const currentSession = this.currentSessionRecord;
        if (!currentSession) {
            return null;
        }
        if (Array.isArray(currentSession.project_id)) {
            return String(currentSession.project_id[0]);
        }
        return currentSession.project_id ? String(currentSession.project_id) : null;
    }

    expandProjectPath(projectId) {
        if (!projectId) {
            return;
        }

        const ancestors = getProjectAncestors(this.state.projectState, projectId);
        if (!ancestors.length) {
            return;
        }

        const expandedProjectIds = new Set(ancestors.map((project) => String(project.id)));
        this.state.projectState.projects = (this.state.projectState.projects || []).map((project) => {
            if (!expandedProjectIds.has(String(project.id))) {
                return project;
            }
            return {
                ...project,
                expanded: true,
            };
        });
    }

    normalizeProjectState(projectState) {
        const projects = Array.isArray(projectState?.projects) ? projectState.projects : [];
        const sessionProjectMap = projectState?.sessionProjectMap || {};
        const expandedById = new Map((this.state.projectState.projects || []).map((project) => [
            String(project.id),
            project.expanded !== false,
        ]));

        return {
            projects: projects.map((project) => ({
                id: String(project.id),
                name: project.name || "New Project",
                parentId: project.parentId ? String(project.parentId) : null,
                order: Number.isFinite(project.order) ? project.order : 0,
                expanded: expandedById.has(String(project.id)) ? expandedById.get(String(project.id)) : true,
            })),
            sessionProjectMap: Object.entries(sessionProjectMap).reduce((acc, [sessionId, projectId]) => {
                if (projectId) {
                    acc[String(sessionId)] = String(projectId);
                }
                return acc;
            }, {}),
        };
    }

    async loadProjectState() {
        try {
            if (!this.currentUser.id) {
                this.state.projectState = this.normalizeProjectState({
                    projects: [],
                    sessionProjectMap: {},
                });
                return;
            }
            const projectState = await this.orm.call("sh.ai.chat.session", "get_chat_project_state", []);
            this.state.projectState = this.normalizeProjectState(projectState);
        } catch (error) {
            console.error("Failed to load project state:", error);
            this.state.projectState = this.normalizeProjectState({
                projects: [],
                sessionProjectMap: {},
            });
        }
    }

    async createProject(sessionToken = null, projectName = null, parentProjectId = null) {
        const trimmedName = (projectName || "").trim();
        if (!trimmedName) {
            return null;
        }

        const sessionRecord = sessionToken ? this.state.sessions.find((item) => item.access_token === sessionToken) : null;
        const project = await this.orm.call("sh.ai.chat.session", "create_chat_project", [
            trimmedName,
            parentProjectId ? Number(parentProjectId) : false,
            sessionRecord ? sessionRecord.id : false,
        ]);

        await this.loadProjectState();
        await this.loadSessions();

        if (project && project.id) {
            this.expandProjectPath(project.id);
        }
        if (parentProjectId) {
            this.expandProjectPath(parentProjectId);
        }
        return project;
    }

    async renameProject(projectId, projectName = null) {
        const trimmedName = (projectName || "").trim();
        if (!trimmedName) {
            return;
        }

        await this.orm.call("sh.ai.chat.session", "rename_chat_project", [
            Number(projectId),
            trimmedName,
        ]);
        await this.loadProjectState();
    }

    async moveProjectToProject(projectId, parentProjectId = null) {
        await this.orm.call("sh.ai.chat.session", "move_chat_project", [
            Number(projectId),
            parentProjectId ? Number(parentProjectId) : false,
        ]);
        await this.loadProjectState();
        await this.loadSessions();
    }

    async renameSession(sessionToken) {
        const sessionRecord = this.state.sessions.find((item) => item.access_token === sessionToken);
        if (!sessionRecord) {
            return;
        }

        const newName = window.prompt("Rename chat", sessionRecord.name || "Chat");
        const trimmedName = (newName || "").trim();
        if (!trimmedName || trimmedName === sessionRecord.name) {
            return;
        }

        await this.orm.write("sh.ai.chat.session", [sessionRecord.id], {
            name: trimmedName,
        });

        await this.loadSessions();
    }

    async deleteProject(projectId) {
        const project = this.state.projectState.projects.find((item) => item.id === String(projectId));
        if (!project) {
            return;
        }

        const projectIdsToDelete = [String(projectId), ...getProjectDescendantIds(this.state.projectState, projectId)];
        const sessionIdsToDelete = this.state.sessions
            .filter((sessionRecord) => {
                const sessionProjectId = Array.isArray(sessionRecord.project_id)
                    ? String(sessionRecord.project_id[0])
                    : sessionRecord.project_id ? String(sessionRecord.project_id) : null;
                return sessionProjectId && projectIdsToDelete.includes(sessionProjectId);
            })
            .map((sessionRecord) => sessionRecord.id);

        const chatLabel = sessionIdsToDelete.length === 1 ? "1 chat" : `${sessionIdsToDelete.length} chats`;
        const projectLabel = projectIdsToDelete.length === 1 ? "1 project" : `${projectIdsToDelete.length} projects`;

        this.dialog.add(ConfirmationDialog, {
            title: "Delete Project",
            body: `Delete "${project.name}"? This will permanently delete ${chatLabel} in ${projectLabel}.`,
            confirm: async () => {
                await this.performProjectDelete(projectId);
            },
            cancel: () => { },
        });
    }

    async performProjectDelete(projectId) {
        const project = this.state.projectState.projects.find((item) => item.id === String(projectId));
        if (!project) {
            return;
        }

        const projectIdsToDelete = [String(projectId), ...getProjectDescendantIds(this.state.projectState, projectId)];
        const sessionIdsToDelete = this.state.sessions
            .filter((sessionRecord) => {
                const sessionProjectId = Array.isArray(sessionRecord.project_id)
                    ? String(sessionRecord.project_id[0])
                    : sessionRecord.project_id ? String(sessionRecord.project_id) : null;
                return sessionProjectId && projectIdsToDelete.includes(sessionProjectId);
            })
            .map((sessionRecord) => sessionRecord.id);

        try {
            await this.orm.call("sh.ai.chat.session", "delete_chat_project", [Number(projectId)]);

            if (this.state.currentSessionToken && sessionIdsToDelete.some((sessionId) => {
                const sessionRecord = this.state.sessions.find((item) => item.id === sessionId);
                return sessionRecord && sessionRecord.access_token === this.state.currentSessionToken;
            })) {
                this.clearUrlSession();
            }

            await this.loadProjectState();
            await this.loadSessions();
            this.notification.add("Project deleted successfully", {
                type: "success",
            });
        } catch (error) {
            console.error("Failed to delete project:", error);
            this.notification.add("Failed to delete project.", {
                type: "danger",
            });
            throw error;
        }
    }

    async moveSessionToProject(sessionToken, projectId) {
        const sessionRecord = this.state.sessions.find((item) => item.access_token === sessionToken);
        if (!sessionRecord) {
            return;
        }

        const normalizedProjectId = projectId ? String(projectId) : null;
        const currentProjectId = Array.isArray(sessionRecord.project_id)
            ? String(sessionRecord.project_id[0])
            : sessionRecord.project_id ? String(sessionRecord.project_id) : null;
        if (String(currentProjectId || "") === String(normalizedProjectId || "")) {
            return;
        }

        await this.orm.call("sh.ai.chat.session", "move_session_to_project", [
            sessionRecord.id,
            normalizedProjectId ? Number(normalizedProjectId) : false,
        ]);
        await this.loadProjectState();
        await this.loadSessions();
        this.expandProjectPath(normalizedProjectId);
    }

    async clearSessionProject(sessionId) {
        await this.orm.call("sh.ai.chat.session", "clear_session_project", [sessionId]);
        await this.loadProjectState();
        await this.loadSessions();
    }

    toggleProject(projectId) {
        toggleProjectExpanded(this.state.projectState, projectId);
    }

    setAllProjectsExpanded(expanded) {
        this.state.projectState.projects = (this.state.projectState.projects || []).map((project) => ({
            ...project,
            expanded,
        }));
    }

    expandAllProjects() {
        this.setAllProjectsExpanded(true);
    }

    collapseAllProjects() {
        this.setAllProjectsExpanded(false);
    }

    async initializeApp() {
        this.state.isLoading = true;
        try {
            const urlParams = new URLSearchParams(window.location.search);
            const sessionToken = urlParams.get('session');

            await this.checkOnboardingState();

            if (this.state.showOnboarding || this.state.showBlocker) {
                this.state.isLoading = false;
                return;
            }

            await this.loadProjectState();
            await this.loadSessions();

            if (sessionToken) {
                const sessionExists = this.state.sessions.find(s => s.access_token === sessionToken);
                if (sessionExists) {
                    this.state.currentSessionToken = sessionToken;
                    this._isFreshNavigation = false;
                    console.log('Restored session from URL:', sessionToken);
                    this.updateUrlWithSession(sessionToken);
                } else {
                    console.log('Session from URL not found, clearing:', sessionToken);
                    this.clearUrlSession();
                }
            }
        } catch (error) {
            console.error("Failed to initialize app:", error);
        } finally {
            this.state.isLoading = false;
        }
    }

    async checkOnboardingState() {
        try {
            const result = await this.orm.call("sh.ai.llm", "get_onboarding_state", []);
            this.state.onboardingState = result;
            this.state.onboardingChecked = true;

            const state = result.state;
            const isManager = result.is_manager;

            if (state === 'completed') {
                this.state.showOnboarding = false;
                this.state.showBlocker = false;
            } else if (isManager) {
                this.state.showOnboarding = true;
                this.state.showBlocker = false;
            } else {
                this.state.showOnboarding = false;
                this.state.showBlocker = true;
            }

            console.log('Onboarding state:', state, 'isManager:', isManager,
                        'showOnboarding:', this.state.showOnboarding,
                        'showBlocker:', this.state.showBlocker);
        } catch (error) {
            console.error('Failed to check onboarding state:', error);
            this.state.showOnboarding = false;
            this.state.showBlocker = false;
            this.state.onboardingChecked = true;
        }
    }

    async onOnboardingComplete() {
        this.state.showOnboarding = false;
        this.state.showBlocker = false;
        this.state.isLoading = true;

        try {
            await this.loadProjectState();
            await this.loadSessions();
        } catch (error) {
            console.error('Failed to load sessions after onboarding:', error);
        } finally {
            this.state.isLoading = false;
        }
    }

    async onOnboardingSkip() {
        try {
            await this.orm.call("sh.ai.llm", "skip_onboarding", []);
        } catch (error) {
            console.error('Failed to skip onboarding:', error);
        }
        this.state.showOnboarding = false;
        this.state.showBlocker = false;
        this.state.isLoading = true;

        try {
            await this.loadProjectState();
            await this.loadSessions();
        } catch (error) {
            console.error('Failed to load sessions after skip:', error);
        } finally {
            this.state.isLoading = false;
        }
    }

    updateUrlWithSession(sessionToken) {
        if (!sessionToken) return;

        const url = new URL(window.location);
        const currentSessionParam = url.searchParams.get('session');

        if (currentSessionParam !== sessionToken) {
            url.searchParams.set('session', sessionToken);
            window.history.replaceState({}, '', url);
        }
    }

    clearUrlSession() {
        const url = new URL(window.location);
        url.searchParams.delete('session');
        window.history.replaceState({}, '', url);
        this.state.currentSessionToken = null;
    }

    async loadSessions() {
        try {
            if (!this.currentUser.id) {
                this.state.sessions = [];
                return;
            }

            const sessions = await this.orm.searchRead(
                "sh.ai.chat.session",
                [['user_id', '=', this.currentUser.id]],
                ['id', 'name', 'access_token', 'last_message_date', 'message_count', 'llm_id', 'project_id'],
                { order: 'last_message_date desc' }
            );

            const llmIds = sessions.filter(s => s.llm_id).map(s => s.llm_id[0]);
            let llmDetails = {};

            if (llmIds.length > 0) {
                const uniqueLlmIds = [...new Set(llmIds)];
                const llms = await this.orm.searchRead(
                    "sh.ai.llm",
                    [['id', 'in', uniqueLlmIds]],
                    ['id', 'name', 'sh_company', 'image']
                );

                llmDetails = llms.reduce((acc, llm) => {
                    acc[llm.id] = llm;
                    return acc;
                }, {});
            }

            this.state.sessions = sessions.map(session => ({
                ...session,
                llm_details: session.llm_id ? llmDetails[session.llm_id[0]] : null
            }));

        } catch (error) {
            console.error("Failed to load sessions:", error);
        }
    }

    toggleSidebar() {
        this.state.sidebarCollapsed = !this.state.sidebarCollapsed;
    }

    async createNewSession(projectId = null) {
        try {
            if (this.state.isLoading) {
                return;
            }

            const sourceProjectId = projectId ? String(projectId) : this.currentSessionProjectId;

            const emptySession = this.state.sessions.find(s => s.message_count === 0);
            if (emptySession) {
                this.notification.add("Some Blank Chat already opened", {
                    type: 'info',
                });
                return;
            }

            this.state.isLoading = true;
            this._isFreshNavigation = false;

            const newSession = await this.orm.call("sh.ai.chat.session", "create_new_session", [
                sourceProjectId ? Number(sourceProjectId) : false,
            ]);

            const url = new URL(window.location);
            url.searchParams.set('session', newSession.access_token);
            window.history.pushState({}, '', url);

            this.state.currentSessionToken = newSession.access_token;
            console.log('Created new session with token:', newSession.access_token);

            await this.loadProjectState();
            await this.loadSessions();

        } catch (error) {
            console.error("Failed to create new session:", error);
        } finally {
            this.state.isLoading = false;
        }
    }

    async selectSession(sessionToken) {
        if (sessionToken === this.state.currentSessionToken) return;

        this.state.isLoading = true;
        this._isFreshNavigation = false;

        const url = new URL(window.location);
        url.searchParams.set('session', sessionToken);
        window.history.pushState({}, '', url);

        this.state.currentSessionToken = sessionToken;
        console.log('Selected session:', sessionToken);
        this.state.isLoading = false;
    }

    async deleteSession(sessionToken) {
        try {
            this.state.isLoading = true;

            const sessionToDelete = this.state.sessions.find(s => s.access_token === sessionToken);
            if (!sessionToDelete) {
                console.error('Session not found for deletion:', sessionToken);
                return;
            }

            await this.orm.unlink("sh.ai.chat.session", [sessionToDelete.id]);
            await this.loadProjectState();

            if (this.state.currentSessionToken === sessionToken) {
                this.clearUrlSession();
            }

            await this.loadSessions();

            this.notification.add("Chat deleted successfully", {
                type: 'success',
            });

            console.log('Deleted session:', sessionToken);

        } catch (error) {
            console.error("Failed to delete session:", error);
            this.notification.add("Failed to delete chat", {
                type: 'danger',
            });
        } finally {
            this.state.isLoading = false;
        }
    }


    onSessionRenamed() {
        this.loadSessions();
    }

    onMessageSent() {
        this.loadSessions();
    }

    onModelChanged() {
        this.loadSessions();
    }

    async onImportSuccess(accessToken) {
        await this.loadSessions();
        await this.loadProjectState();

        if (accessToken) {
            await this.selectSession(accessToken);
        }
    }

    startUrlMonitoring() {
        this.urlMonitorInterval = setInterval(() => {
            this.checkUrlIntegrity();
        }, 2000);
    }

    stopUrlMonitoring() {
        if (this.urlMonitorInterval) {
            clearInterval(this.urlMonitorInterval);
            this.urlMonitorInterval = null;
        }
    }

    checkUrlIntegrity() {
        if (this._isFreshNavigation) {
            return;
        }

        const urlParams = new URLSearchParams(window.location.search);
        const urlSessionToken = urlParams.get('session');

        if (this.state.currentSessionToken && !urlSessionToken) {
            console.log('URL lost session parameter, restoring:', this.state.currentSessionToken);
            this.updateUrlWithSession(this.state.currentSessionToken);
        }
        else if (urlSessionToken && urlSessionToken !== this.state.currentSessionToken) {
            console.log('URL session differs from state, syncing:', urlSessionToken);
            const sessionExists = this.state.sessions.find(s => s.access_token === urlSessionToken);
            if (sessionExists) {
                this.state.currentSessionToken = urlSessionToken;
            } else {
                this.clearUrlSession();
            }
        }
    }
}

registry.category("actions").add("sh_ai_assistance", AiChatAppComponent);
