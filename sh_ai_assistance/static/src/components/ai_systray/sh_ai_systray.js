/** @odoo-module **/

import { Component, useState, onWillStart, onWillUnmount, onMounted } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { AiChatWindow } from "./sh_ai_chat_window";

export class ShAiChatSystray extends Component {
    static template = "sh_ai_assistance.AiChatSystray";
    static components = {
        AiChatWindow,
    };

    setup() {
        this.orm = useService("orm");
        this.state = useState({
            isOpen: false,
            hasAccess: false,
            isLoading: true,
        });
        this.handleKeydown = (event) => {
            if (event.key === 'Escape') {
                this.onClose();
            }
        };

        onMounted(() => {
            document.addEventListener('keydown', this.handleKeydown);

            // Odoo 16 Dark Mode Polyfill for AI Chat Styles
            const cookies = document.cookie;
            const isDark = /(?:^|;\s*)(color_scheme|configured_color_scheme)=dark/.test(cookies);
            if (isDark) {
                document.body.classList.add('o_dark');
            } else {
                document.body.classList.remove('o_dark');
            }
        });

        onWillUnmount(() => {
            document.removeEventListener('keydown', this.handleKeydown);
        });


        onWillStart(async () => {
            try {
                // Fetch access status from backend to avoid issues with frontend services or missing UIDs
                // Calling a specialized method on sh.ai.chat.session ensures we use server-side environment
                const hasAccess = await this.orm.call("sh.ai.chat.session", "check_ai_access", []);
                this.state.hasAccess = hasAccess;
            } catch (error) {
                console.error("AI Access check failed:", error);
                this.state.hasAccess = false;
            } finally {
                this.state.isLoading = false;
            }
        });
    }

    toggleChat(ev) {
        if (!this.state.hasAccess) return;

        // Safety check: if clicking inside the chat window container, 
        // we allow the event to bubble for dropdowns but we don't toggle the window state.
        if (ev && ev.target.closest('.o_sh_ai_chat_window_container')) {
            return;
        }

        this.state.isOpen = !this.state.isOpen;
    }

    onClose() {
        this.state.isOpen = false;
    }

    // onKeyDown(event) {
    //     this.state.isOpen = false;
    // }
}

registry.category("systray").add("sh_ai_assistance.ai_chat_systray", { Component: ShAiChatSystray }, { sequence: 25 });
