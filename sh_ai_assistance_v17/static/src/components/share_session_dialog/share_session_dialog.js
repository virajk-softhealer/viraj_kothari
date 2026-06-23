/** @odoo-module **/

import { Component, useRef } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import { useService } from "@web/core/utils/hooks";

export class ShareSessionDialog extends Component {
    static template = "sh_ai_assistance.ShareSessionDialog";
    static components = { Dialog };
    static props = {
        close: { type: Function },
        shareLink: { type: String },
    };

    setup() {
        this.shareLinkInput = useRef("shareLinkInput");
        this.notification = useService("notification");
    }

    async onCopyLink() {
        // Fallback for non-secure (HTTP) contexts where navigator.clipboard is undefined
        const textToCopy = this.props.shareLink;
        try {
            if (navigator.clipboard && window.isSecureContext) {
                await navigator.clipboard.writeText(textToCopy);
            } else {
                // Fallback: Create a temporary textarea element
                const textArea = document.createElement("textarea");
                textArea.value = textToCopy;
                textArea.style.position = "fixed";
                textArea.style.left = "-9999px";
                textArea.style.top = "0";
                document.body.appendChild(textArea);
                textArea.focus();
                textArea.select();
                try {
                    document.execCommand('copy');
                } catch (err) {
                    throw new Error('Fallback copy failed');
                }
                document.body.removeChild(textArea);
            }
            this.notification.add("Link copied to clipboard!", { type: "success" });
        } catch (err) {
            console.error('Failed to copy text: ', err);
            this.notification.add("Failed to copy link.", { type: "danger" });
        }
        this.props.close();
    }
}
