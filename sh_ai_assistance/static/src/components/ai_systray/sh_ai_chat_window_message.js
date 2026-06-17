/** @odoo-module **/

import { ChatMessageComponent } from "../chat_message/chat_message";

export class AiChatWindowMessageComponent extends ChatMessageComponent {
    static template = "sh_ai_assistance.AiChatWindowMessageTemplate";

    get isErrorMessage() {
        const message = this.props.message || {};
        return message.message_type === 'error' ;     
    }
}
