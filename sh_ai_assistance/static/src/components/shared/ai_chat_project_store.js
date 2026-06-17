/** @odoo-module **/

import { browser } from "@web/core/browser/browser";
import { session } from "@web/session";

const STORAGE_PREFIX = "sh_ai_assistance.chat_projects";

function getDatabaseName() {
    return session.db || session.database || "default";
}

export function getChatProjectStorageKey(userId) {
    return `${STORAGE_PREFIX}.${getDatabaseName()}.${userId || "anonymous"}`;
}

export function createDefaultChatProjectState() {
    return {
        projects: [],
        sessionProjectMap: {},
    };
}

export function normalizeChatProjectState(rawState) {
    const state = createDefaultChatProjectState();
    if (!rawState || typeof rawState !== "object") {
        return state;
    }

    if (Array.isArray(rawState.projects)) {
        state.projects = rawState.projects
            .filter((project) => project && project.id)
            .map((project) => ({
                id: String(project.id),
                name: project.name || "New Project",
                parentId: project.parentId ? String(project.parentId) : null,
                order: Number.isFinite(project.order) ? project.order : 0,
                expanded: project.expanded !== false,
            }));
    }

    if (rawState.sessionProjectMap && typeof rawState.sessionProjectMap === "object") {
        state.sessionProjectMap = Object.entries(rawState.sessionProjectMap).reduce((acc, [sessionId, projectId]) => {
            if (projectId) {
                acc[String(sessionId)] = String(projectId);
            }
            return acc;
        }, {});
    }

    return state;
}

export function loadChatProjectState(userId) {
    try {
        const rawState = browser.localStorage.getItem(getChatProjectStorageKey(userId));
        if (!rawState) {
            return createDefaultChatProjectState();
        }
        return normalizeChatProjectState(JSON.parse(rawState));
    } catch {
        return createDefaultChatProjectState();
    }
}

export function saveChatProjectState(userId, state) {
    browser.localStorage.setItem(getChatProjectStorageKey(userId), JSON.stringify(normalizeChatProjectState(state)));
}

export function createChatProjectId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
        return `project-${window.crypto.randomUUID()}`;
    }
    return `project-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

export function getProjectById(projectState, projectId) {
    return (projectState?.projects || []).find((project) => project.id === String(projectId)) || null;
}

export function getProjectAncestors(projectState, projectId) {
    const projects = [];
    let currentProject = getProjectById(projectState, projectId);
    while (currentProject) {
        projects.unshift(currentProject);
        currentProject = currentProject.parentId ? getProjectById(projectState, currentProject.parentId) : null;
    }
    return projects;
}

export function getProjectLabelPath(projectState, projectId) {
    return getProjectAncestors(projectState, projectId).map((project) => project.name).join(" / ");
}

export function flattenChatProjects(projectState) {
    const projects = Array.isArray(projectState?.projects) ? projectState.projects : [];
    const childrenByParent = new Map();

    projects.forEach((project) => {
        const parentKey = project.parentId || null;
        if (!childrenByParent.has(parentKey)) {
            childrenByParent.set(parentKey, []);
        }
        childrenByParent.get(parentKey).push(project);
    });

    const sortProjects = (items) => items.slice().sort((left, right) => {
        const orderDiff = (left.order || 0) - (right.order || 0);
        if (orderDiff !== 0) {
            return orderDiff;
        }
        return (left.name || "").localeCompare(right.name || "");
    });

    const flattened = [];
    const visit = (parentId, depth) => {
        for (const project of sortProjects(childrenByParent.get(parentId || null) || [])) {
            flattened.push({
                ...project,
                depth,
                labelPath: getProjectLabelPath(projectState, project.id),
            });
            visit(project.id, depth + 1);
        }
    };

    visit(null, 0);
    return flattened;
}

export function buildChatProjectTree(sessions, projectState) {
    const projects = Array.isArray(projectState?.projects) ? projectState.projects : [];
    const sessionProjectMap = projectState?.sessionProjectMap || {};
    const projectsById = projects.reduce((acc, project) => {
        acc[project.id] = {
            ...project,
            childrenProjects: [],
            sessions: [],
            sessionCount: 0,
        };
        return acc;
    }, {});

    const sortProjects = (items) => items.slice().sort((left, right) => {
        const orderDiff = (left.order || 0) - (right.order || 0);
        if (orderDiff !== 0) {
            return orderDiff;
        }
        return (left.name || "").localeCompare(right.name || "");
    });

    const sortSessions = (items) => items.slice().sort((left, right) => {
        const leftDate = left.last_message_date ? new Date(left.last_message_date).getTime() : 0;
        const rightDate = right.last_message_date ? new Date(right.last_message_date).getTime() : 0;
        if (rightDate !== leftDate) {
            return rightDate - leftDate;
        }
        return (left.name || "").localeCompare(right.name || "");
    });

    Object.values(projectsById).forEach((project) => {
        project.childrenProjects = [];
        project.sessions = [];
        project.sessionCount = 0;
    });

    const roots = [];
    Object.values(projectsById).forEach((project) => {
        const parentId = project.parentId && projectsById[project.parentId] ? project.parentId : null;
        if (parentId) {
            projectsById[parentId].childrenProjects.push(project);
        } else {
            roots.push(project);
        }
    });

    const unfiledSessions = [];
    for (const sessionRecord of sessions || []) {
        const projectId = sessionProjectMap[String(sessionRecord.id)];
        if (projectId && projectsById[projectId]) {
            projectsById[projectId].sessions.push(sessionRecord);
        } else {
            unfiledSessions.push(sessionRecord);
        }
    }

    const finalizeProject = (project) => {
        project.childrenProjects = sortProjects(project.childrenProjects).map((child) => finalizeProject(child));
        project.sessions = sortSessions(project.sessions);
        project.sessionCount = project.sessions.length + project.childrenProjects.reduce((total, child) => total + child.sessionCount, 0);
        project.hasChildren = project.childrenProjects.length > 0;
        return project;
    };

    return {
        roots: sortProjects(roots).map((project) => finalizeProject(project)),
        unfiledSessions: sortSessions(unfiledSessions),
        totalProjects: projects.length,
        totalSessions: (sessions || []).length,
    };
}

export function createProjectInState(projectState, name, parentId = null) {
    const nextProject = {
        id: createChatProjectId(),
        name: name || "New Project",
        parentId: parentId ? String(parentId) : null,
        order: (projectState.projects || []).filter((project) => project.parentId === (parentId ? String(parentId) : null)).length,
        expanded: true,
    };
    projectState.projects = [...(projectState.projects || []), nextProject];
    return nextProject;
}

export function renameProjectInState(projectState, projectId, newName) {
    const targetId = String(projectId);
    projectState.projects = (projectState.projects || []).map((project) => {
        if (project.id !== targetId) {
            return project;
        }
        return {
            ...project,
            name: newName || project.name,
        };
    });
}

export function toggleProjectExpanded(projectState, projectId) {
    const targetId = String(projectId);
    projectState.projects = (projectState.projects || []).map((project) => {
        if (project.id !== targetId) {
            return project;
        }
        return {
            ...project,
            expanded: !project.expanded,
        };
    });
}

export function moveSessionToProjectInState(projectState, sessionId, projectId) {
    const targetSessionId = String(sessionId);
    const nextSessionMap = {
        ...(projectState.sessionProjectMap || {}),
    };

    if (projectId) {
        nextSessionMap[targetSessionId] = String(projectId);
    } else {
        delete nextSessionMap[targetSessionId];
    }

    projectState.sessionProjectMap = nextSessionMap;
}

export function getProjectDescendantIds(projectState, projectId) {
    const targetId = String(projectId);
    const projects = Array.isArray(projectState?.projects) ? projectState.projects : [];
    const childrenByParent = new Map();

    projects.forEach((project) => {
        const parentKey = project.parentId || null;
        if (!childrenByParent.has(parentKey)) {
            childrenByParent.set(parentKey, []);
        }
        childrenByParent.get(parentKey).push(project);
    });

    const descendantIds = [];
    const visit = (parentId) => {
        for (const child of childrenByParent.get(parentId || null) || []) {
            descendantIds.push(child.id);
            visit(child.id);
        }
    };

    visit(targetId);
    return descendantIds;
}

export function deleteProjectInState(projectState, projectId) {
    const targetId = String(projectId);
    const projects = Array.isArray(projectState.projects) ? projectState.projects : [];
    const descendantIds = new Set(getProjectDescendantIds(projectState, targetId));
    descendantIds.add(targetId);

    projectState.projects = projects
        .filter((project) => !descendantIds.has(project.id));

    const updatedSessionMap = {};
    Object.entries(projectState.sessionProjectMap || {}).forEach(([sessionId, currentProjectId]) => {
        if (!descendantIds.has(String(currentProjectId))) {
            updatedSessionMap[sessionId] = currentProjectId;
        }
    });
    projectState.sessionProjectMap = updatedSessionMap;
}
