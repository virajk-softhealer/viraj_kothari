/** @odoo-module **/

import { AiChatContentComponent } from "../ai_chat_content/ai_chat_content";
import { AiChatWindowMessageComponent } from "./sh_ai_chat_window_message";

export class AiChatWindowContentComponent extends AiChatContentComponent {
    static template = "sh_ai_assistance.AiChatWindowContentTemplate";
    static components = {
        ...AiChatContentComponent.components,
        ChatMessage: AiChatWindowMessageComponent,
    };
}
