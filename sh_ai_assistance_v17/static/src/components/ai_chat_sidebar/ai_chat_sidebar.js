/** @odoo-module **/

import { Component, useState, useRef, onPatched, onMounted, onWillUnmount } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { flattenChatProjects, getProjectDescendantIds } from "../shared/ai_chat_project_store";

export class AiChatSidebarComponent extends Component {
    static template = "sh_ai_assistance.AiChatSidebarTemplate";
    static props = {
        sessions: { type: Array },
        projectTree: { type: Object, optional: true },
        projectState: { type: Object, optional: true },
        currentSessionToken: { type: [String, { value: null }], optional: true },
        currentSessionProjectId: { type: [String, { value: null }], optional: true },
        isCollapsed: { type: Boolean },
        isLoading: { type: Boolean },
        onNewChat: { type: Function },
        onSelectSession: { type: Function },
        onToggleSidebar: { type: Function },
        onDeleteSession: { type: Function },
        onRenameSession: { type: Function, optional: true },
        onCreateChatInProject: { type: Function, optional: true },
        onCreateProject: { type: Function, optional: true },
        onRenameProject: { type: Function, optional: true },
        onDeleteProject: { type: Function, optional: true },
        onMoveProjectToProject: { type: Function, optional: true },
        onMoveSessionToProject: { type: Function, optional: true },
        onToggleProject: { type: Function, optional: true },
        onExpandAllProjects: { type: Function, optional: true },
        onCollapseAllProjects: { type: Function, optional: true },
        onImportSuccess: { type: Function, optional: true },
    };

    setup() {
        this.dialog = useService("dialog");
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.importFileInput = useRef("importFileInput");
        this.inlineProjectInput = useRef("inlineProjectInput");
        this.chatTreeScroll = useRef("chatTreeScroll");
        this.state = useState({
            hoveredSession: null,
            hoveredProject: null,
            openSessionMenuToken: null,
            openProjectMenuId: null,
            activeProjectPickerProjectId: null,
            activeProjectPickerSessionToken: null,
            draggedSessionToken: null,
            draggedProjectId: null,
            dropTargetType: null,
            dropTargetId: null,
            projectsSectionExpanded: true,
            chatsSectionExpanded: true,
            creatingProjectActive: false,
            creatingProjectParentId: null,
            creatingProjectSessionToken: null,
            creatingProjectName: "",
            editingProjectId: null,
            editingProjectName: "",
        });
        this.lastInlineEditorKey = null;
        this.autoScrollFrame = null;
        this.lastDragClientY = null;
        this.boundDocumentClick = (event) => this.onDocumentClick(event);

        onMounted(() => {
            document.addEventListener("click", this.boundDocumentClick, true);
        });

        onWillUnmount(() => {
            document.removeEventListener("click", this.boundDocumentClick, true);
            this.stopAutoScroll();
        });

        onPatched(() => {
            const editorKey = this.state.editingProjectId
                ? `rename:${this.state.editingProjectId}`
                : this.state.creatingProjectActive
                    ? `create:${this.state.creatingProjectParentId || "root"}`
                    : null;

            if (!editorKey || editorKey === this.lastInlineEditorKey) {
                return;
            }

            this.lastInlineEditorKey = editorKey;
            if (this.inlineProjectInput.el) {
                this.inlineProjectInput.el.focus();
            }
        });
    }


    onNewChatClick() {
        this.closeMenus();
        this.props.onNewChat();
    }

    getSessionProjectId(sessionToken) {
        const sessionRecord = this.props.sessions.find((item) => item.access_token === sessionToken);
        if (!sessionRecord) {
            return null;
        }
        if (Array.isArray(sessionRecord.project_id)) {
            return String(sessionRecord.project_id[0]);
        }
        if (sessionRecord.project_id) {
            return String(sessionRecord.project_id);
        }
        if (!this.props.projectState) {
            return null;
        }
        return this.props.projectState.sessionProjectMap[String(sessionRecord.id)] || null;
    }

    onSessionClick(sessionToken) {
        this.closeMenus();
        this.props.onSelectSession(sessionToken);
    }

    onDeleteClick(event, sessionToken) {
        event.stopPropagation(); // Prevent session selection
        this.closeMenus();

        this.dialog.add(ConfirmationDialog, {
            title: "Delete Chat",
            body: "This will permanently delete this chat session and all its messages. This action cannot be undone.",
            confirm: () => {
                this.props.onDeleteSession(sessionToken);
            },
            cancel: () => { },
        });
    }

    onRenameSessionClick(event, sessionToken) {
        event.stopPropagation();
        if (this.props.onRenameSession) {
            this.props.onRenameSession(sessionToken);
        }
        this.closeMenus();
    }

    onSessionMouseEnter(sessionToken) {
        this.state.hoveredSession = sessionToken;
    }

    onSessionMouseLeave() {
        this.state.hoveredSession = null;
    }

    isSessionHovered(sessionToken) {
        return this.state.hoveredSession === sessionToken;
    }

    isProjectHovered(projectId) {
        return this.state.hoveredProject === projectId;
    }

    closeMenus() {
        this.state.openSessionMenuToken = null;
        this.state.openProjectMenuId = null;
        this.state.activeProjectPickerProjectId = null;
        this.state.activeProjectPickerSessionToken = null;
    }

    stopAutoScroll() {
        if (this.autoScrollFrame) {
            window.cancelAnimationFrame(this.autoScrollFrame);
            this.autoScrollFrame = null;
        }
        this.lastDragClientY = null;
    }

    scheduleAutoScroll() {
        if (this.autoScrollFrame) {
            return;
        }

        const tick = () => {
            const container = this.chatTreeScroll.el;
            const clientY = this.lastDragClientY;
            const hasDraggedItem = !!(this.state.draggedSessionToken || this.state.draggedProjectId);

            if (!container || !hasDraggedItem || typeof clientY !== "number") {
                this.stopAutoScroll();
                return;
            }

            const rect = container.getBoundingClientRect();
            const threshold = 56;
            const step = 18;
            let direction = 0;

            if (clientY < rect.top + threshold) {
                direction = -1;
            } else if (clientY > rect.bottom - threshold) {
                direction = 1;
            }

            if (!direction) {
                this.stopAutoScroll();
                return;
            }

            container.scrollTop += step * direction;
            this.autoScrollFrame = window.requestAnimationFrame(tick);
        };

        this.autoScrollFrame = window.requestAnimationFrame(tick);
    }

    updateAutoScroll(event) {
        this.lastDragClientY = event.clientY;
        this.scheduleAutoScroll();
    }

    onSessionMenuClick(event, sessionToken) {
        event.stopPropagation();
        this.state.openProjectMenuId = null;
        this.state.activeProjectPickerSessionToken = null;
        this.state.openSessionMenuToken = this.state.openSessionMenuToken === sessionToken ? null : sessionToken;
    }

    onProjectMenuClick(event, projectId) {
        event.stopPropagation();
        this.state.openSessionMenuToken = null;
        this.state.activeProjectPickerSessionToken = null;
        this.state.activeProjectPickerProjectId = null;
        this.state.openProjectMenuId = this.state.openProjectMenuId === projectId ? null : projectId;
    }

    onProjectClick(projectId) {
        this.closeMenus();
        if (this.props.onToggleProject) {
            this.props.onToggleProject(projectId);
        }
    }

    onProjectDragStart(event, projectId) {
        event.stopPropagation();
        this.closeMenus();
        this.state.draggedProjectId = String(projectId);
        this.state.dropTargetType = null;
        this.state.dropTargetId = null;
        this.lastDragClientY = event.clientY;

        if (event.dataTransfer) {
            event.dataTransfer.effectAllowed = "move";
            event.dataTransfer.setData("text/plain", String(projectId));
            event.dataTransfer.setData("application/x-sh-ai-project", String(projectId));
        }
    }

    onProjectDragEnd() {
        this.state.draggedProjectId = null;
        this.state.dropTargetType = null;
        this.state.dropTargetId = null;
        this.stopAutoScroll();
    }

    onCreateProjectClick(event, sessionToken, parentProjectId = null, parentExpanded = true) {
        event.stopPropagation();
        if (parentProjectId && !parentExpanded && this.props.onToggleProject) {
            this.props.onToggleProject(parentProjectId);
        }
        this.beginCreateProject(parentProjectId, sessionToken);
    }

    onCreateChatInProjectClick(event, projectId) {
        event.stopPropagation();
        if (this.props.onCreateChatInProject) {
            this.props.onCreateChatInProject(projectId);
        }
        this.closeMenus();
    }

    onRenameProjectClick(event, project) {
        event.stopPropagation();
        this.beginRenameProject(project);
        this.closeMenus();
    }

    onDeleteProjectClick(event, projectId) {
        event.stopPropagation();
        if (this.props.onDeleteProject) {
            this.props.onDeleteProject(projectId);
        }
        this.closeMenus();
    }

    onToggleProjectMovePicker(event, projectId) {
        event.stopPropagation();
        this.state.activeProjectPickerSessionToken = null;
        this.state.activeProjectPickerProjectId = this.state.activeProjectPickerProjectId === projectId ? null : projectId;
    }

    onMoveProjectToProjectClick(event, projectId, parentProjectId) {
        event.stopPropagation();
        if (this.props.onMoveProjectToProject) {
            this.props.onMoveProjectToProject(projectId, parentProjectId);
        }
        this.state.activeProjectPickerProjectId = null;
        this.state.openProjectMenuId = null;
    }

    onMoveSessionToProjectClick(event, sessionToken, projectId) {
        event.stopPropagation();
        if (this.props.onMoveSessionToProject) {
            this.props.onMoveSessionToProject(sessionToken, projectId);
        }
        this.state.activeProjectPickerSessionToken = null;
        this.state.openSessionMenuToken = null;
    }

    onSessionDragStart(event, sessionToken) {
        event.stopPropagation();
        this.state.draggedSessionToken = sessionToken;
        this.state.dropTargetType = null;
        this.state.dropTargetId = null;
        this.lastDragClientY = event.clientY;

        if (event.dataTransfer) {
            event.dataTransfer.effectAllowed = "move";
            event.dataTransfer.setData("text/plain", sessionToken);
        }
    }

    onSessionDragEnd() {
        this.state.draggedSessionToken = null;
        this.state.dropTargetType = null;
        this.state.dropTargetId = null;
        this.stopAutoScroll();
    }

    onDragOverProject(event, projectId) {
        if (!this.state.draggedSessionToken && !this.state.draggedProjectId) {
            return;
        }
        event.preventDefault();
        event.dataTransfer.dropEffect = "move";
        this.updateAutoScroll(event);
        this.state.dropTargetType = "project";
        this.state.dropTargetId = String(projectId);
    }

    onDragEnterProject(event, projectId) {
        if (!this.state.draggedSessionToken && !this.state.draggedProjectId) {
            return;
        }
        event.preventDefault();
        this.updateAutoScroll(event);
        this.state.dropTargetType = "project";
        this.state.dropTargetId = String(projectId);
    }

    onDragEnterProjectArea(event, projectId) {
        if (!this.state.draggedSessionToken && !this.state.draggedProjectId) {
            return;
        }
        const hoveredProjectNode = event.target?.closest?.(".project-node");
        if (hoveredProjectNode !== event.currentTarget) {
            return;
        }
        event.preventDefault();
        event.dataTransfer.dropEffect = "move";
        this.state.dropTargetType = "project";
        this.state.dropTargetId = String(projectId);
    }

    onDragOverProjectArea(event, projectId) {
        if (!this.state.draggedSessionToken && !this.state.draggedProjectId) {
            return;
        }
        const hoveredProjectNode = event.target?.closest?.(".project-node");
        if (hoveredProjectNode !== event.currentTarget) {
            return;
        }
        event.preventDefault();
        event.dataTransfer.dropEffect = "move";
        this.updateAutoScroll(event);
        this.state.dropTargetType = "project";
        this.state.dropTargetId = String(projectId);
    }

    onDropOnProject(event, projectId) {
        if (!this.state.draggedSessionToken && !this.state.draggedProjectId) {
            return;
        }
        event.preventDefault();
        event.stopPropagation();
        if (this.state.draggedProjectId) {
            if (this.props.onMoveProjectToProject) {
                this.props.onMoveProjectToProject(this.state.draggedProjectId, projectId);
            }
        } else if (this.props.onMoveSessionToProject) {
            this.props.onMoveSessionToProject(this.state.draggedSessionToken, projectId);
        }
        this.onProjectDragEnd();
        this.onSessionDragEnd();
    }

    onDropOnProjectArea(event, projectId) {
        if (!this.state.draggedSessionToken && !this.state.draggedProjectId) {
            return;
        }
        const hoveredProjectNode = event.target?.closest?.(".project-node");
        if (hoveredProjectNode !== event.currentTarget) {
            return;
        }
        event.preventDefault();
        event.stopPropagation();
        if (this.state.draggedProjectId) {
            if (this.props.onMoveProjectToProject) {
                this.props.onMoveProjectToProject(this.state.draggedProjectId, projectId);
            }
        } else if (this.props.onMoveSessionToProject) {
            this.props.onMoveSessionToProject(this.state.draggedSessionToken, projectId);
        }
        this.onProjectDragEnd();
        this.onSessionDragEnd();
    }

    onDragEnterUnfiled(event) {
        if (!this.state.draggedSessionToken && !this.state.draggedProjectId) {
            return;
        }
        event.preventDefault();
        this.updateAutoScroll(event);
        this.state.dropTargetType = "unfiled";
        this.state.dropTargetId = null;
    }

    onDragOverUnfiled(event) {
        if (!this.state.draggedSessionToken && !this.state.draggedProjectId) {
            return;
        }
        event.preventDefault();
        event.dataTransfer.dropEffect = "move";
        this.updateAutoScroll(event);
        this.state.dropTargetType = "unfiled";
        this.state.dropTargetId = null;
    }

    onDragOverTree(event) {
        if (!this.state.draggedSessionToken && !this.state.draggedProjectId) {
            return;
        }
        if (event.target.closest(".project-node, .unfiled-drop-zone")) {
            return;
        }
        event.preventDefault();
        event.dataTransfer.dropEffect = "move";
        this.updateAutoScroll(event);
        this.state.dropTargetType = "unfiled";
        this.state.dropTargetId = null;
    }

    onDragEnterTree(event) {
        if (!this.state.draggedSessionToken && !this.state.draggedProjectId) {
            return;
        }
        if (event.target.closest(".project-node, .unfiled-drop-zone")) {
            return;
        }
        event.preventDefault();
        this.updateAutoScroll(event);
        this.state.dropTargetType = "unfiled";
        this.state.dropTargetId = null;
    }

    onDropUnfiled(event) {
        if (!this.state.draggedSessionToken && !this.state.draggedProjectId) {
            return;
        }
        event.preventDefault();
        event.stopPropagation();
        if (this.state.draggedProjectId) {
            if (this.props.onMoveProjectToProject) {
                this.props.onMoveProjectToProject(this.state.draggedProjectId, null);
            }
        } else if (this.props.onMoveSessionToProject) {
            this.props.onMoveSessionToProject(this.state.draggedSessionToken, null);
        }
        this.onProjectDragEnd();
        this.onSessionDragEnd();
    }

    onDragLeaveDropArea(event) {
        if (!this.state.draggedSessionToken && !this.state.draggedProjectId) {
            return;
        }
        const relatedTarget = event.relatedTarget;
        if (!relatedTarget || !event.currentTarget.contains(relatedTarget)) {
            this.state.dropTargetType = null;
            this.state.dropTargetId = null;
            this.stopAutoScroll();
        }
    }

    onToggleProjectPicker(event, sessionToken) {
        event.stopPropagation();
        this.state.openProjectMenuId = null;
        this.state.activeProjectPickerSessionToken = this.state.activeProjectPickerSessionToken === sessionToken ? null : sessionToken;
    }

    onCreateTopLevelProjectClick() {
        this.closeMenus();
        this.beginCreateProject(null, null);
    }

    onExpandAllProjectsClick() {
        if (this.props.onExpandAllProjects) {
            this.props.onExpandAllProjects();
        }
    }

    onCollapseAllProjectsClick() {
        if (this.props.onCollapseAllProjects) {
            this.props.onCollapseAllProjects();
        }
    }

    toggleProjectsSection() {
        this.state.projectsSectionExpanded = !this.state.projectsSectionExpanded;
    }

    toggleChatsSection() {
        this.state.chatsSectionExpanded = !this.state.chatsSectionExpanded;
    }

    get flattenedProjects() {
        return flattenChatProjects(this.props.projectState || { projects: [] });
    }

    getProjectMoveTargets(projectId) {
        const targetId = String(projectId);
        const excludedIds = new Set([targetId, ...getProjectDescendantIds(this.props.projectState || { projects: [] }, targetId)]);
        return this.flattenedProjects.filter((project) => !excludedIds.has(String(project.id)));
    }

    get hasProjects() {
        return !!(this.props.projectTree && this.props.projectTree.totalProjects > 0);
    }

    beginCreateProject(parentProjectId = null, sessionToken = null) {
        this.closeMenus();
        this.state.projectsSectionExpanded = true;
        this.state.creatingProjectActive = true;
        this.state.creatingProjectParentId = parentProjectId ? String(parentProjectId) : null;
        this.state.creatingProjectSessionToken = sessionToken || null;
        this.state.creatingProjectName = "";
        this.state.editingProjectId = null;
        this.state.editingProjectName = "";
    }

    beginRenameProject(project) {
        if (!project) {
            return;
        }
        this.closeMenus();
        this.state.creatingProjectActive = false;
        this.state.editingProjectId = String(project.id);
        this.state.editingProjectName = project.name || "";
        this.state.creatingProjectParentId = null;
        this.state.creatingProjectSessionToken = null;
        this.state.creatingProjectName = "";
    }

    cancelInlineProjectEdit() {
        this.state.creatingProjectActive = false;
        this.state.creatingProjectParentId = null;
        this.state.creatingProjectSessionToken = null;
        this.state.creatingProjectName = "";
        this.state.editingProjectId = null;
        this.state.editingProjectName = "";
        this.lastInlineEditorKey = null;
    }

    async saveInlineProjectCreate() {
        const name = (this.state.creatingProjectName || "").trim();
        if (!name) {
            return;
        }
        if (this.props.onCreateProject) {
            await this.props.onCreateProject(
                this.state.creatingProjectSessionToken,
                name,
                this.state.creatingProjectParentId
            );
        }
        this.cancelInlineProjectEdit();
    }

    async saveInlineProjectRename() {
        const projectId = this.state.editingProjectId;
        const name = (this.state.editingProjectName || "").trim();
        if (!projectId || !name) {
            return;
        }
        if (this.props.onRenameProject) {
            await this.props.onRenameProject(projectId, name);
        }
        this.cancelInlineProjectEdit();
    }

    onInlineProjectInputKeydown(event, mode) {
        if (event.key === "Enter") {
            if (mode === "create") {
                this.saveInlineProjectCreate();
            } else {
                this.saveInlineProjectRename();
            }
        } else if (event.key === "Escape") {
            this.cancelInlineProjectEdit();
        }
    }

    onInlineProjectInputChange(event, mode) {
        const value = event.target.value;
        if (mode === "create") {
            this.state.creatingProjectName = value;
        } else {
            this.state.editingProjectName = value;
        }
    }

    isCreatingProjectFor(parentProjectId) {
        return String(this.state.creatingProjectParentId || "") === String(parentProjectId || "");
    }



    onToggleSidebarClick() {
        this.props.onToggleSidebar();
    }

    onSidebarShellClick(ev) {
        if (this.props.isCollapsed) {
            // Keep native button/input interactions intact (e.g., toggle button)
            if (ev.target.closest("button, input, a, textarea, select, label")) {
                return;
            }
            this.props.onToggleSidebar();
            return;
        }

        if (!ev.target.closest(".session-context-menu, .project-context-menu, .project-action-trigger, .session-actions")) {
            this.closeMenus();
        }
    }

    onDocumentClick(event) {
        if (this.props.isCollapsed) {
            return;
        }

        const clickedInsideSidebar = event.target.closest(".o-ai-ChatSidebar");
        const clickedMenu = event.target.closest(".session-context-menu, .project-context-menu");
        const clickedTrigger = event.target.closest(".project-action-trigger, .session-actions button");

        if (!clickedInsideSidebar || (!clickedMenu && !clickedTrigger)) {
            this.closeMenus();
        }
    }

    isSessionInProject(sessionToken, projectId) {
        return this.getSessionProjectId(sessionToken) === String(projectId);
    }

    isDraggedSession(sessionToken) {
        return this.state.draggedSessionToken === sessionToken;
    }

    isDropTarget(type, id = null) {
        return this.state.dropTargetType === type && String(this.state.dropTargetId || "") === String(id || "");
    }

    formatDate(dateStr) {
        const date = new Date(dateStr);
        const now = new Date();
        const diffTime = Math.abs(now - date);
        const diffDays = Math.ceil(diffTime / (1000 * 60 * 60 * 24));

        if (diffDays === 1) {
            return 'Today';
        } else if (diffDays === 2) {
            return 'Yesterday';
        } else if (diffDays <= 7) {
            return `${diffDays - 1} days ago`;
        } else {
            return date.toLocaleDateString();
        }
    }

    isCurrentSession(sessionToken) {
        return this.props.currentSessionToken === sessionToken;
    }

    get shouldDisableNewChat() {
        // Disable if loading
        if (this.props.isLoading) {
            return true;
        }

        // Disable if ANY session is empty (has no messages)
        const emptySession = this.props.sessions.find(s => s.message_count === 0);
        if (emptySession) {
            return true;
        }

        return false;
    }

    get newChatButtonTooltip() {
        if (this.props.isLoading) {
            return "Creating new chat...";
        }

        const emptySession = this.props.sessions.find(s => s.message_count === 0);
        if (emptySession) {
            return "You already have an empty chat. Use it before creating a new one.";
        }

        return "Create a new chat session";
    }

    onImportChatClick() {
        if (this.importFileInput.el) {
            this.importFileInput.el.click();
        }
    }

    async onFileSelected(ev) {
        const file = ev.target.files[0];
        if (!file) return;

        try {
            const text = await file.text();
            const importData = JSON.parse(text);

            if (typeof importData !== 'object' || importData === null) {
                this.notification.add("Invalid file format. Expected a JSON object or array.", { type: "danger" });
                return;
            }

            // Validate that there is actually content to import
            let hasMessages = false;
            if (Array.isArray(importData)) {
                hasMessages = importData.length > 0;
            } else if (importData.export_type === 'project') {
                hasMessages = Array.isArray(importData.sessions) && importData.sessions.some(s => Array.isArray(s.messages) && s.messages.length > 0);
            } else {
                const messages = importData.messages || [];
                hasMessages = Array.isArray(messages) && messages.length > 0;
            }

            if (!hasMessages) {
                this.notification.add("The selected file contains an empty chat session and cannot be imported.", { type: "warning" });
                return;
            }

            // Send to backend
            const result = await this.orm.call("sh.ai.chat.session", "import_session_from_json", [
                importData,
                this.props.currentSessionProjectId ? Number(this.props.currentSessionProjectId) : false,
            ]);

            if (result && result.access_token) {
                this.notification.add("Chat imported successfully!", { type: "success" });

                // Notify parent to reload sessions and select the new one
                if (this.props.onImportSuccess) {
                    this.props.onImportSuccess(result.access_token);
                }
            } else {
                this.notification.add("Failed to import chat.", { type: "danger" });
            }

        } catch (error) {
            console.error("Failed to read file:", error);
            this.notification.add("Failed to read file. Please ensure it is a valid JSON.", { type: "danger" });
        } finally {
            // Reset input so same file can be selected again if needed
            ev.target.value = '';
        }
    }
}
