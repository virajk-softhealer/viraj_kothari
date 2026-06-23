# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies.

from odoo.http import Controller, request, route
import json
import base64
import markdown
from markupsafe import Markup


class ShareAiChat(Controller):

    @route('/web/snapshot/<string:snapshot_uuid>', type='http', auth='public', website=True)
    def share_snapshot(self, snapshot_uuid, **kwargs):
        """Public page to view a shared AI chat session from a snapshot."""
        snapshot = request.env['sh.ai.chat.session.snapshot'].sudo().search([
            ('snapshot_uuid', '=', snapshot_uuid)
        ], limit=1)

        if not snapshot:
            return request.not_found()

        # Decode and parse messages from the binary field
        messages = []
        if snapshot.messages_json:
            try:
                # messages_json is stored as base64 encoded string of JSON
                decoded_json = base64.b64decode(snapshot.messages_json).decode('utf-8')
                messages = json.loads(decoded_json)
                
                for msg in messages:
                    if msg.get('content'):
                        # Convert markdown to html with tables support
                        html_content = markdown.markdown(
                            msg['content'], 
                            extensions=['tables', 'fenced_code']
                        )
                        msg['content'] = Markup(html_content)

            except Exception as e:
                request.env.cr.rollback() # Rollback if JSON decoding fails
                return request.not_found("Failed to decode snapshot messages: %s" % e)

        return request.render('sh_ai_assistance.share_page_template', {
            'snapshot': snapshot,
            'messages': messages,
            'session_name': snapshot.name, # For easier access in template
        })

    @route('/ai/chat/export/<int:session_id>', type='http', auth='user')
    def export_chat(self, session_id, **kwargs):
        """Export chat session messages as JSON file."""
        session = request.env['sh.ai.chat.session'].browse(session_id)
        
        # Security check: Ensure session exists and belongs to current user
        if not session.exists() or session.user_id != request.env.user:
             return request.not_found()

        messages_data = []
        for msg in session.message_ids.sorted('create_date'):
            llm_xml_id = False
            if msg.llm_id:
                # get_external_id returns {record_id: 'complete.xml_id'}
                ext_ids = msg.llm_id.get_external_id()
                llm_xml_id = ext_ids.get(msg.llm_id.id)

            messages_data.append({
                'author': msg.message_type,
                'content': msg.content,
                'create_date': msg.create_date.isoformat(),
                'llm': msg.llm_id.name if msg.llm_id else False,
                'llm_code': msg.llm_id.sh_model_code if msg.llm_id else False,
                'llm_xml_id': llm_xml_id,
                'action_data': msg.action_data,
                'query_details': msg.query_details,
                'debug_info': msg.debug_info,
                'sh_is_cancelled': msg.sh_is_cancelled,
                'prompt_tokens': msg.prompt_tokens,
                'completion_tokens': msg.completion_tokens,
                'total_tokens': msg.total_tokens,
                'tool_call_count': msg.tool_call_count,
                'execution_time': msg.execution_time,
            })
            
        export_data = {
            'session_name': session.name,
            'project_name': session.project_id.name if session.project_id else False,
            'messages': messages_data,
        }

        json_content = json.dumps(export_data, indent=2)
        
        # Create safe filename
        safe_name = "".join([c for c in session.name if c.isalnum() or c in (' ', '-', '_')]).strip()
        filename = f"chat_export_{safe_name}.json"
        
        return request.make_response(
            json_content,
            headers=[
                ('Content-Type', 'application/json'),
                ('Content-Disposition', f'attachment; filename="{filename}"')
            ]
        )

    @route('/ai/avatar/user/<int:user_id>', type='http', auth='public')
    def get_user_avatar(self, user_id):
        """Public route for user avatar in snapshots"""
        record = request.env['res.users'].sudo().browse(user_id)
        if not record.exists() or not record.avatar_128:
             return request.redirect('/web/static/src/img/placeholder.png')
        
        return request.env['ir.binary']._get_image_stream_from(
            record, 'avatar_128'
        ).get_response()

    @route('/ai/avatar/llm/<int:llm_id>', type='http', auth='public')
    def get_llm_avatar(self, llm_id):
        """Public route for LLM avatar in snapshots"""
        record = request.env['sh.ai.llm'].sudo().browse(llm_id)
        if not record.exists() or not record.image:
             return request.redirect('/sh_ai_assistance/static/description/icon.png')
        
        return request.env['ir.binary']._get_image_stream_from(
            record, 'image'
        ).get_response()