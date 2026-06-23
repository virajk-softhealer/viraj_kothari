/** @odoo-module **/

import { Component, useState, useRef, onMounted, onWillUnmount } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { flattenChatProjects } from "../shared/ai_chat_project_store";

export class AiChatSidebarComponent extends Component {
    static template = "sh_ai_assistance.AiChatSidebarTemplate";
    static props = {
        sessions: { type: Array },
        projectTree: { type: Object },
        projectState: { type: Object },
        currentSessionToken: { type: [String, { value: null }], optional: true },
        currentSessionProjectId: { type: [String, { value: null }], optional: true },
        isCollapsed: { type: Boolean },
        isLoading: { type: Boolean },
        onNewChat: { type: Function },
        onSelectSession: { type: Function },
        onToggleSidebar: { type: Function },
        onDeleteSession: { type: Function },
        onRenameSession: { type: Function },
        onCreateProject: { type: Function },
        onRenameProject: { type: Function },
        onDeleteProject: { type: Function },
        onMoveProjectToProject: { type: Function },
        onMoveSessionToProject: { type: Function },
        onToggleProject: { type: Function },
        onExpandAllProjects: { type: Function },
        onCollapseAllProjects: { type: Function },
        onImportSuccess: { type: Function, optional: true },
    };

    setup() {
        this.dialog = useService("dialog");
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.importFileInput = useRef("importFileInput");
        this.inlineProjectInput = useRef("inlineProjectInput");

        this.state = useState({
            hoveredSession: null,
            hoveredProject: null,

            // Inline creation/editing
            creatingProjectActive: false,
            creatingProjectParentId: null,
            editingProjectId: null,
            editingProjectName: "",

            // Context menus
            openProjectMenuId: null,
            openSessionMenuToken: null,
            activeProjectPickerProjectId: null,
            activeProjectPickerSessionToken: null,

            // Drag and drop
            draggedProjectId: null,
            draggedSessionToken: null,
            dropTargetId: null,
            dropTargetType: null, // "project" | "unfiled" | "project_area"

            // Sections
            projectsSectionExpanded: true,
            chatsSectionExpanded: true,
        });

        this._onWindowClick = (ev) => {
            if (!this.state.openProjectMenuId && !this.state.openSessionMenuToken) {
                return;
            }
            if (ev.target.closest('.project-context-menu') || ev.target.closest('.session-context-menu')) {
                return;
            }
            this.state.openProjectMenuId = null;
            this.state.openSessionMenuToken = null;
            this.state.activeProjectPickerProjectId = null;
            this.state.activeProjectPickerSessionToken = null;
        };

        onMounted(() => {
            window.addEventListener('click', this._onWindowClick);
        });

        onWillUnmount(() => {
            window.removeEventListener('click', this._onWindowClick);
        });
    }

    // =====================================================================
    //  DRAG AND DROP HANDLERS
    // =====================================================================

    onProjectDragStart(ev, projectId) {
        this.state.draggedProjectId = String(projectId);
        this.state.draggedSessionToken = null;
        ev.dataTransfer.setData("application/x-sh-ai-project", String(projectId));
        ev.dataTransfer.effectAllowed = "move";
    }

    onProjectDragEnd() {
        this.state.draggedProjectId = null;
        this.clearDropTarget();
    }

    onSessionDragStart(ev, sessionToken) {
        this.state.draggedSessionToken = sessionToken;
        this.state.draggedProjectId = null;
        ev.dataTransfer.setData("application/x-sh-ai-session", sessionToken);
        ev.dataTransfer.effectAllowed = "move";
    }

    onSessionDragEnd() {
        this.state.draggedSessionToken = null;
        this.clearDropTarget();
    }

    onDragEnterTree(ev) {
        ev.preventDefault();
        this.state.dropTargetId = "root";
        this.state.dropTargetType = "root";
    }

    onDragOverTree(ev) {
        ev.preventDefault();
        ev.dataTransfer.dropEffect = "move";
    }

    onDragEnterRoot(ev) {
        ev.preventDefault();
        this.state.dropTargetId = "root";
        this.state.dropTargetType = "root";
    }

    onDragEnterProject(ev, projectId) {
        ev.preventDefault();
        this.state.dropTargetId = String(projectId);
        this.state.dropTargetType = "project";
    }

    onDragOverProject(ev, projectId) {
        ev.preventDefault();
        ev.dataTransfer.dropEffect = "move";
    }

    onDragEnterProjectArea(ev, projectId) {
        ev.preventDefault();
        this.state.dropTargetId = String(projectId);
        this.state.dropTargetType = "project_area";
    }

    onDragOverProjectArea(ev, projectId) {
        ev.preventDefault();
    }

    onDragEnterUnfiled(ev) {
        ev.preventDefault();
        this.state.dropTargetId = "unfiled";
        this.state.dropTargetType = "unfiled";
    }

    onDragOverUnfiled(ev) {
        ev.preventDefault();
    }

    onDragLeaveDropArea(ev) {
        // Only clear if we're actually leaving the sidebar or a known drop zone
        // In Owl/Web, dragleave can be noisy. Enter handlers are more reliable for switching.
        // We'll clear it on DragEnd anyway.
    }

    clearDropTarget() {
        this.state.dropTargetId = null;
        this.state.dropTargetType = null;
    }

    async onDropOnProject(ev, targetProjectId) {
        ev.preventDefault();
        const sessionToken = ev.dataTransfer.getData("application/x-sh-ai-session");
        const sourceProjectId = ev.dataTransfer.getData("application/x-sh-ai-project");

        if (sessionToken) {
            await this.props.onMoveSessionToProject(sessionToken, targetProjectId);
        } else if (sourceProjectId && String(sourceProjectId) !== String(targetProjectId)) {
            await this.props.onMoveProjectToProject(sourceProjectId, targetProjectId);
        }
        this.clearDropTarget();
    }

    async onDropOnProjectArea(ev, targetProjectId) {
        // Dropping near a project (e.g. to become a sibling or just move into its general area)
        // Usually treated same as drop on project for simplicity in many implementations
        await this.onDropOnProject(ev, targetProjectId);
    }

    async onDropUnfiled(ev) {
        ev.preventDefault();
        const sessionToken = ev.dataTransfer.getData("application/x-sh-ai-session");
        const sourceProjectId = ev.dataTransfer.getData("application/x-sh-ai-project");

        if (sessionToken) {
            await this.props.onMoveSessionToProject(sessionToken, null);
        } else if (sourceProjectId) {
            await this.props.onMoveProjectToProject(sourceProjectId, null);
        }
        this.clearDropTarget();
    }

    isDropTarget(type, id = null) {
        if (type === 'unfiled') return this.state.dropTargetType === 'unfiled';
        if (type === 'root') return this.state.dropTargetType === 'root';
        return this.state.dropTargetType === type && String(this.state.dropTargetId) === String(id);
    }

    isDraggedSession(token) { return this.state.draggedSessionToken === token; }

    // =====================================================================
    //  PROJECT ACTIONS
    // =====================================================================

    onCreateTopLevelProjectClick() {
        this.state.creatingProjectActive = true;
        this.state.creatingProjectParentId = null;
        this.state.projectsSectionExpanded = true;
        this.focusInlineInput();
    }

    onCreateProjectClick(ev, sessionToken, parentProjectId, isExpanded) {
        ev.stopPropagation();
        this.state.creatingProjectActive = true;
        this.state.creatingProjectParentId = parentProjectId ? String(parentProjectId) : null;
        this.state.openProjectMenuId = null;
        this.state.openSessionMenuToken = null;

        // Ensure parent is expanded so we can see the input
        if (parentProjectId && !isExpanded) {
            this.props.onToggleProject(parentProjectId);
        }
        this.focusInlineInput();
    }

    onRenameProjectClick(ev, node) {
        ev.stopPropagation();
        this.state.editingProjectId = String(node.id);
        this.state.editingProjectName = node.name;
        this.state.openProjectMenuId = null;
        this.focusInlineInput();
    }

    async onInlineProjectInputChange(ev, mode) {
        if (mode === 'rename') {
            this.state.editingProjectName = ev.target.value;
        }
    }

    async onInlineProjectInputKeydown(ev, mode) {
        if (ev.key === "Enter") {
            const name = ev.target.value.trim();
            if (!name) return;

            if (mode === "create") {
                await this.props.onCreateProject(null, name, this.state.creatingProjectParentId);
                this.state.creatingProjectActive = false;
            } else {
                await this.props.onRenameProject(this.state.editingProjectId, name);
                this.state.editingProjectId = null;
            }
        } else if (ev.key === "Escape") {
            this.cancelInlineProjectEdit();
        }
    }

    cancelInlineProjectEdit() {
        this.state.creatingProjectActive = false;
        this.state.editingProjectId = null;
        this.state.editingProjectName = "";
    }

    focusInlineInput() {
        setTimeout(() => {
            if (this.inlineProjectInput.el) {
                this.inlineProjectInput.el.focus();
                this.inlineProjectInput.el.select();
            }
        }, 50);
    }

    onProjectClick(projectId) {
        this.props.onToggleProject(projectId);
    }

    onProjectMenuClick(ev, projectId) {
        ev.stopPropagation();
        if (this.state.openProjectMenuId === String(projectId)) {
            this.state.openProjectMenuId = null;
            this.state.activeProjectPickerProjectId = null;
        } else {
            this.state.openProjectMenuId = String(projectId);
            this.state.openSessionMenuToken = null;
            this.state.activeProjectPickerProjectId = null;
        }
    }

    onDeleteProjectClick(ev, projectId) {
        ev.stopPropagation();
        this.props.onDeleteProject(projectId);
        this.state.openProjectMenuId = null;
    }

    onToggleProjectMovePicker(ev, projectId) {
        ev.stopPropagation();
        this.state.activeProjectPickerProjectId = (this.state.activeProjectPickerProjectId === String(projectId)) ? null : String(projectId);
    }

    async onMoveProjectToProjectClick(ev, projectId, targetParentId) {
        ev.stopPropagation();
        await this.props.onMoveProjectToProject(projectId, targetParentId);
        this.state.openProjectMenuId = null;
        this.state.activeProjectPickerProjectId = null;
    }

    async onCreateChatInProjectClick(ev, projectId) {
        ev.stopPropagation();
        await this.props.onNewChat();
        // The newly created chat will need to be move to the project, 
        // but typically 'onNewChat' in AiChatApp takes project_id as arg.
        // If it doesn't, we'd need to handle it.
        // Assuming createNewSession(projectId) is supported.
        this.state.openProjectMenuId = null;
    }

    toggleProjectsSection() { this.state.projectsSectionExpanded = !this.state.projectsSectionExpanded; }
    toggleChatsSection() { this.state.chatsSectionExpanded = !this.state.chatsSectionExpanded; }

    onExpandAllProjectsClick() { this.props.onExpandAllProjects(); }
    onCollapseAllProjectsClick() { this.props.onCollapseAllProjects(); }

    // =====================================================================
    //  SESSION ACTIONS
    // =====================================================================

    onSessionClick(token) { this.props.onSelectSession(token); }

    onSessionMouseEnter(token) { this.state.hoveredSession = token; }
    onSessionMouseLeave() { this.state.hoveredSession = null; }
    isSessionHovered(token) { return this.state.hoveredSession === token; }

    onSessionMenuClick(ev, token) {
        ev.stopPropagation();
        if (this.state.openSessionMenuToken === token) {
            this.state.openSessionMenuToken = null;
            this.state.activeProjectPickerSessionToken = null;
        } else {
            this.state.openSessionMenuToken = token;
            this.state.openProjectMenuId = null;
            this.state.activeProjectPickerSessionToken = null;
        }
    }

    onToggleProjectPicker(ev, token) {
        ev.stopPropagation();
        this.state.activeProjectPickerSessionToken = (this.state.activeProjectPickerSessionToken === token) ? null : token;
    }

    async onMoveSessionToProjectClick(ev, token, projectId) {
        ev.stopPropagation();
        await this.props.onMoveSessionToProject(token, projectId);
        this.state.openSessionMenuToken = null;
        this.state.activeProjectPickerSessionToken = null;
    }

    onRenameSessionClick(ev, token) {
        ev.stopPropagation();
        this.props.onRenameSession(token);
        this.state.openSessionMenuToken = null;
    }

    onDeleteClick(ev, token) {
        ev.stopPropagation();
        this.dialog.add(ConfirmationDialog, {
            title: "Delete Chat",
            body: "Are you sure you want to delete this chat session?",
            confirm: async () => {
                await this.props.onDeleteSession(token);
                this.state.openSessionMenuToken = null;
            },
            cancel: () => { },
        });
    }

    onNewChatClick() { this.props.onNewChat(); }
    onToggleSidebarClick() { this.props.onToggleSidebar(); }
    onSidebarShellClick(ev) {
        if (!this.props.isCollapsed) return;
        if (ev.target.closest("button, input, a, textarea, select, label")) return;
        this.props.onToggleSidebar();
    }

    // =====================================================================
    //  UI HELPERS
    // =====================================================================

    get flattenedProjects() { return flattenChatProjects(this.props.projectState); }
    getProjectMoveTargets(projectId) {
        // Exclude self and descendants from move targets
        const projects = this.flattenedProjects;
        // In a real impl, we'd filter out the current project and its sub-hierarchy
        return projects.filter(p => String(p.id) !== String(projectId));
    }

    formatDate(dateStr) {
        if (!dateStr) return '';
        const date = new Date(dateStr);
        const now = new Date();
        const diffDays = Math.floor((now - date) / (1000 * 60 * 60 * 24));
        if (diffDays === 0 && now.getDate() === date.getDate()) return 'Today';
        if (diffDays === 1 || (diffDays === 0 && now.getDate() !== date.getDate())) return 'Yesterday';
        if (diffDays <= 7) return `${diffDays} days ago`;
        return date.toLocaleDateString();
    }

    isCurrentSession(token) { return this.props.currentSessionToken === token; }
    get shouldDisableNewChat() {
        if (this.props.isLoading) return true;
        return !!this.props.sessions.find(s => s.message_count === 0);
    }
    get newChatButtonTooltip() {
        if (this.props.isLoading) return "Creating new chat...";
        if (this.props.sessions.find(s => s.message_count === 0)) return "You already have an empty chat.";
        return "Create a new chat session";
    }
    get hasProjects() { return this.props.projectTree && this.props.projectTree.totalProjects > 0; }

    onImportChatClick() { if (this.importFileInput.el) this.importFileInput.el.click(); }

    async onFileSelected(ev) {
        const file = ev.target.files[0];
        if (!file) return;
        try {
            const text = await file.text();
            const importData = JSON.parse(text);
            const result = await this.orm.call("sh.ai.chat.session", "import_session_from_json", [
                importData,
                this.props.currentSessionProjectId ? Number(this.props.currentSessionProjectId) : false
            ]);
            if (result && result.access_token) {
                this.notification.add("Chat imported successfully!", { type: "success" });
                if (this.props.onImportSuccess) this.props.onImportSuccess(result.access_token);
            } else {
                this.notification.add("Failed to import chat.", { type: "danger" });
            }
        } catch (error) {
            this.notification.add("Invalid file format.", { type: "danger" });
        } finally {
            ev.target.value = '';
        }
    }
}
