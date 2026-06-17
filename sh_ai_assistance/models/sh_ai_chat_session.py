# -- coding: utf-8 --
# Copyright (C) Softhealer Technologies Pvt. Ltd.

from odoo import api, fields, models, _
import secrets
import json
import base64


class ShAiChatSession(models.Model):
    _name = 'sh.ai.chat.session'
    _description = 'AI Chat Session'
    _order = 'last_message_date desc, id desc'
    _rec_name = 'display_name'

    name = fields.Char(string="Session Title", required=True, default="New Chat")
    display_name = fields.Char(string="Display Name", compute='_compute_display_name', store=True)
    access_token = fields.Char(string="Access Token", required=True, index=True, help="Unique token for secure session access")
    user_id = fields.Many2one('res.users', string="User", required=True, default=lambda self: self.env.user, ondelete='cascade')
    project_id = fields.Many2one('sh.ai.chat.project', string="Project", ondelete='set null', index=True)
    llm_id = fields.Many2one('sh.ai.llm', string="LLM Provider")
    state = fields.Selection([
        ('active', 'Active'),
        ('archived', 'Archived'),
        ('error', 'Error')
    ], string="Status", default='active')
    is_active = fields.Boolean(string="Active", default=True)
    last_message_date = fields.Datetime(string="Last Message", compute='_compute_message_stats', store=True)
    message_count = fields.Integer(string="Message Count", compute='_compute_message_stats', store=True)
    message_ids = fields.One2many('sh.ai.chat.message', 'session_id', string="Messages")

    # Model Catalog Cache for performance and context
    model_catalog = fields.Json(string="Model Catalog Cache", help="Cached list of available Odoo models for this session")
    catalog_timestamp = fields.Datetime(string="Catalog Last Updated", help="When the model catalog was last refreshed")
    is_context_initialized = fields.Boolean(string="Context Initialized", default=False, help="Flag to track if the AI has received the full model catalog in this session")
    sh_active_worker_pid = fields.Integer(string="Active Worker PID", default=0, help="Technical field to track the PID of the worker processing this session's AI request")

    # Analytics (Cumulative for session)
    total_prompt_tokens = fields.Integer(string="Total Prompt Tokens", compute='_compute_message_stats', store=True)
    total_completion_tokens = fields.Integer(string="Total Completion Tokens", compute='_compute_message_stats', store=True)
    total_cached_tokens = fields.Integer(string="Total Cached Tokens", compute='_compute_message_stats', store=True, help="Total tokens served from provider cache")
    total_reasoning_tokens = fields.Integer(string="Total Reasoning Tokens", compute='_compute_message_stats', store=True)

    @api.model
    def check_ai_access(self):
        """Check if current user has AI Assistant access."""
        return self.env.user.has_group('sh_ai_base.group_sh_ai_user')

    @api.model
    def get_initial_data(self):
        """Fetch initial data for the AI Chat Window in one go."""
        # 1. Models
        models_data = self.env['sh.ai.llm'].search_read(
            [('active', '=', True)],
            ['id', 'name', 'sh_company', 'sh_model_code', 'image','is_default']
        )
        
        # 2. Recent Sessions (Filtered by record rules automatically)
        sessions_data = self.search_read(
            [('user_id', '=', self.env.user.id)],
            ['id', 'access_token', 'llm_id', 'project_id', 'name', 'last_message_date', 'message_count'],
            order='last_message_date desc, write_date desc',
            limit=10
        )

        projects_data = self.env['sh.ai.chat.project'].search_read(
            [('user_id', '=', self.env.user.id)],
            ['id', 'name', 'parent_id', 'sequence', 'user_id']
        )

        return {
            'models': models_data,
            'sessions': sessions_data,
            'projects': projects_data,
        }
    total_session_tokens = fields.Integer(string="Total Session Tokens", compute='_compute_message_stats', store=True)
    total_tool_calls = fields.Integer(string="Total Tool Calls", compute='_compute_message_stats', store=True)
    total_execution_time = fields.Float(string="Total Execution Time", compute='_compute_message_stats', store=True)

    @api.depends('name')
    def _compute_display_name(self):
        for session in self:
            session.display_name = session.name or "Start New Conversation"

    @api.depends('message_ids', 'message_ids.create_date', 'message_ids.prompt_tokens', 'message_ids.completion_tokens', 'message_ids.cached_tokens', 'message_ids.sh_reasoning_tokens', 'message_ids.tool_call_count', 'message_ids.execution_time')
    def _compute_message_stats(self):
        for session in self:
            messages = session.message_ids
            session.message_count = len(messages)
            session.last_message_date = max(messages.mapped('create_date')) if messages else fields.Datetime.now()
            
            # Sum up analytics
            assistant_messages = messages.filtered(lambda m: m.message_type in ['assistant', 'error'])
            session.total_prompt_tokens = sum(m.prompt_tokens or 0 for m in assistant_messages)
            session.total_completion_tokens = sum(m.completion_tokens or 0 for m in assistant_messages)
            session.total_cached_tokens = sum(m.cached_tokens or 0 for m in assistant_messages)
            session.total_reasoning_tokens = sum(m.sh_reasoning_tokens or 0 for m in assistant_messages)
            session.total_session_tokens = sum(m.total_tokens or 0 for m in assistant_messages)
            session.total_tool_calls = sum(m.tool_call_count or 0 for m in assistant_messages)
            session.total_execution_time = sum(m.execution_time or 0.0 for m in assistant_messages)

    def _generate_access_token(self):
        """Generate a unique access token for the session"""
        return secrets.token_urlsafe(32)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            # Generate unique access token
            if not vals.get('access_token'):
                vals['access_token'] = self._generate_access_token()

            # Auto-generate session name based on timestamp if not provided
            if not vals.get('name') or vals.get('name') in [_('Start New Conversation'), _('New Conversation'), 'New Chat']:
                vals['name'] = _("Chat %s") % fields.Datetime.now().strftime('%Y-%m-%d %H:%M')

            # Assign default LLM if not specified
            if not vals.get('llm_id'):
                default_llm = self.env['sh.ai.llm'].search([('is_default', '=', True)], limit=1)
                if default_llm:
                    vals['llm_id'] = default_llm.id

        return super().create(vals_list)

    @api.model
    def create_new_session(self, project_id=None):
        """API method to create a new chat session for current user"""
        vals = {
            'name': 'New Chat',
            'user_id': self.env.user.id
        }
        if project_id:
            project = self.env['sh.ai.chat.project'].browse(project_id).exists()
            if project and project.user_id == self.env.user:
                vals['project_id'] = project.id

        session = self.create(vals)

        return {
            'id': session.id,
            'name': session.name,
            'access_token': session.access_token,
            'display_name': session.display_name,
            'llm_id': [session.llm_id.id, session.llm_id.name] if session.llm_id else False,
            'project_id': [session.project_id.id, session.project_id.display_name] if session.project_id else False,
        }

    @api.model
    def find_by_token(self, access_token):
        """Find session by access token for current user"""
        session = self.search([
            ('access_token', '=', access_token),
            ('user_id', '=', self.env.user.id)
        ], limit=1)

        if session:
            return {
                'id': session.id,
                'name': session.name,
                'access_token': session.access_token,
                'message_count': session.message_count,
                'last_message_date': session.last_message_date,
                'project_id': [session.project_id.id, session.project_id.display_name] if session.project_id else False,
                'llm_id': [session.llm_id.id, session.llm_id.name] if session.llm_id else False,
            }
        return False

    @api.model
    def get_chat_project_state(self):
        """Return the persisted project tree and session-to-project mapping for the current user."""
        projects = self.env['sh.ai.chat.project'].search_read(
            [('user_id', '=', self.env.user.id)],
            ['id', 'name', 'parent_id', 'sequence', 'user_id']
        )
        sessions = self.search_read(
            [('user_id', '=', self.env.user.id)],
            ['id', 'project_id']
        )
        session_project_map = {}
        for session in sessions:
            if session.get('project_id'):
                session_project_map[str(session['id'])] = str(session['project_id'][0])
        normalized_projects = []
        for project in projects:
            normalized_projects.append({
                'id': str(project['id']),
                'name': project.get('name') or _('New Project'),
                'parentId': str(project['parent_id'][0]) if project.get('parent_id') else None,
                'order': project.get('sequence') or 0,
                'expanded': True,
            })
        return {
            'projects': normalized_projects,
            'sessionProjectMap': session_project_map,
        }

    @api.model
    def create_chat_project(self, name, parent_id=None, session_id=None):
        """Create a chat project in the database and optionally move a session into it."""
        project_name = (name or "").strip()
        if not project_name:
            return False

        project_vals = {
            'name': project_name,
            'user_id': self.env.user.id,
        }
        if parent_id:
            parent_project = self.env['sh.ai.chat.project'].browse(parent_id).exists()
            if parent_project and parent_project.user_id == self.env.user:
                project_vals['parent_id'] = parent_project.id

        project = self.env['sh.ai.chat.project'].create(project_vals)

        if session_id:
            self.move_session_to_project(session_id, project.id)

        return {
            'id': project.id,
            'name': project.name,
            'parent_id': [project.parent_id.id, project.parent_id.display_name] if project.parent_id else False,
            'sequence': project.sequence,
        }

    @api.model
    def rename_chat_project(self, project_id, name):
        """Rename an existing project owned by the current user."""
        project = self.env['sh.ai.chat.project'].browse(project_id).exists()
        if not project or project.user_id != self.env.user:
            return False
        project_name = (name or "").strip()
        if not project_name:
            return False
        if project.name == project_name:
            return True
        project.write({'name': project_name})
        return True

    @api.model
    def delete_chat_project(self, project_id):
        """Delete a project subtree and the chats inside it."""
        project = self.env['sh.ai.chat.project'].browse(project_id).exists()
        if not project or project.user_id != self.env.user:
            return False
        project.unlink()
        return True

    @api.model
    def move_chat_project(self, project_id, parent_project_id=None):
        """Move a project under another project or to the root level."""
        project = self.env['sh.ai.chat.project'].browse(project_id).exists()
        if not project or project.user_id != self.env.user:
            return False

        descendant_ids = set()
        projects_to_visit = project
        while projects_to_visit:
            descendant_ids.update(projects_to_visit.ids)
            projects_to_visit = self.env['sh.ai.chat.project'].search([('parent_id', 'in', projects_to_visit.ids)])

        new_parent = False
        if parent_project_id:
            new_parent = self.env['sh.ai.chat.project'].browse(parent_project_id).exists()
            if not new_parent or new_parent.user_id != self.env.user:
                return False
            if new_parent.id in descendant_ids:
                return False

        project.write({'parent_id': new_parent.id if new_parent else False})
        return True

    @api.model
    def move_session_to_project(self, session_id, project_id):
        """Move a chat session to a project."""
        session = self.browse(session_id).exists()
        if not session or session.user_id != self.env.user:
            return False

        project = False
        if project_id:
            project = self.env['sh.ai.chat.project'].browse(project_id).exists()
            if not project or project.user_id != self.env.user:
                return False

        session.write({'project_id': project.id if project else False})
        return True

    @api.model
    def clear_session_project(self, session_id):
        """Remove the project from a chat session."""
        return self.move_session_to_project(session_id, False)

    def auto_rename_from_message(self, message_content):
        """
        Auto-rename session based on first user message.
        Takes first 2 words from message and appends '...'
        Only renames if current name is auto-generated (starts with 'Chat 20')
        """
        self.ensure_one()

        # Only rename if current name is auto-generated timestamp format
        if not self.name or not self.name.startswith('Chat'):
            return False

        # Extract first 4 words from message
        words = message_content.strip().split()
        if len(words) == 0:
            return False

        # Take first 4 words (or just 1 if message is short)
        num_words = min(4, len(words))
        title_words = words[:num_words]
        new_name = ' '.join(title_words)

        # Add ellipsis if there are more words or if we took 4 words
        if len(words) > num_words or num_words == 4:
            new_name += '...'

        # Update session name
        self.name = new_name
        return True
    
    def get_demo_questions(self):
        """
        Get 3 random demo questions based on installed modules in the database.
        Strategy: Select ONE random question from each installed module (mixed approach).
        This ensures diversity and shows features from multiple modules.
        """
        import random

        # Dictionary of all possible questions organized by module
        questions_by_module = {
            'sale': [
                'How many sales orders do we have?',
                'What is the total number of customers?',
                'How many orders are pending?',
                'Show me the sales order list.',
                'What is the total sales amount?',
            ],
            'purchase': [
                'How many purchase orders do we have?',
                'What is the total number of vendors?',
                'How many purchase orders are pending?',
                'Show me the purchase order list.',
                'What is the total purchase amount?',
            ],
            'crm': [
                'How many leads do we have?',
                'How many opportunities are open?',
                'What is the total number of customers?',
                'Show me the leads list.',
                'How many deals are in progress?',
            ],
            'hr': [
                'What is the total number of employees?',
                'How many employees are active?',
                'How many leave requests are pending?',
                'Show me the employee list.',
                'What is the total headcount?',
            ],
            'account': [
                'How many invoices are pending?',
                'What is the total invoice amount?',
                'How many bills do we have?',
                'Show me the invoice list.',
                'How many customers owe us money?',
            ],
            'inventory': [
                'How many products do we have?',
                'What is the total stock value?',
                'How many items are out of stock?',
                'Show me the product list.',
                'How many stock movements today?',
            ],
            'project': [
                'How many projects do we have?',
                'How many active projects?',
                'How many tasks are pending?',
                'Show me the project list.',
                'How many tasks are overdue?',
            ],
            'product': [
                'How many products do we have?',
                'What is the total product count?',
                'Show me the product list.',
                'How many products are out of stock?',
                'What is the average product price?',
            ],
            'calendar': [
                'How many meetings scheduled?',
                'What events are coming up?',
                'Show me my calendar.',
                'How many meetings today?',
                'What is my schedule?',
            ],
            'contact': [
                'How many contacts do we have?',
                'How many customers are there?',
                'How many vendors do we have?',
                'Show me the contacts list.',
                'How many companies are registered?',
            ],
        }

        # Get list of installed modules
        installed_modules = self.env['ir.module.module'].search([
            ('state', '=', 'installed')
        ]).mapped('name')

        # Strategy: Select ONE random question from each installed module
        selected_questions = []
        modules_with_questions = []

        for module in installed_modules:
            if module in questions_by_module:
                # Pick ONE random question from this module
                question = random.choice(questions_by_module[module])
                selected_questions.append(question)
                modules_with_questions.append(module)

        # Limit to 3 questions
        if len(selected_questions) > 3:
            # If more than 3 modules, pick 3 random ones
            selected_indices = random.sample(range(len(selected_questions)), 3)
            selected_questions = [selected_questions[i] for i in selected_indices]
            modules_with_questions = [modules_with_questions[i] for i in selected_indices]

        # Return success only if we have questions from at least one module
        if selected_questions:
            return {
                'success': True,
                'questions': selected_questions,
                'modules_used': modules_with_questions,
                'total_modules': len([m for m in installed_modules if m in questions_by_module.keys()]),
            }
        else:
            # No relevant modules installed - return fallback questions for new databases
            fallback_questions = [
                'How can I use this AI assistant?',
                'Show me my profile information?',
                'What companies am I associated with?',
            ]

            return {
                'success': True,
                'questions': fallback_questions,
                'modules_used': ['base'],
                'total_modules': 0,
                'is_fallback': True,
                'message': 'Showing fallback questions for new database',
            }

    def create_session_snapshot(self):
        """
        Creates a snapshot of the current session's messages and stores it
        in a binary field (in the filestore).
        Returns the UUID of the snapshot.
        """
        self.ensure_one()

        # ✅ NEW (public-safe URLs)
        user_avatar_url = f"/ai/avatar/user/{self.user_id.id}"
        llm_image_url = f"/ai/avatar/llm/{self.llm_id.id}"

        messages_data = []
        for msg in self.message_ids.sorted('create_date'):
            # Basic message data
            msg_dict = {
                'id': msg.id,
                'content': msg.content,
                'create_date': msg.create_date.strftime('%Y-%m-%d %H:%M:%S'),
                'type': msg.message_type,  
                'author': 'user' if msg.message_type == 'user' else 'assistant',
                'sh_is_cancelled': msg.sh_is_cancelled,
            }

            # Set Avatar URL based on type
            if msg.message_type == 'user':
                msg_dict['avatar_url'] = user_avatar_url
            else:
                msg_dict['avatar_url'] = llm_image_url
            
            messages_data.append(msg_dict)


        messages_json_str = json.dumps(messages_data, indent=2)
        messages_base64 = base64.b64encode(messages_json_str.encode('utf-8'))

        snapshot = self.env['sh.ai.chat.session.snapshot'].create({
            'name': self.name,
            'original_session_id': self.id,
            'messages_json': messages_base64,
        })

        return snapshot.snapshot_uuid

    @api.model
    def import_session_from_json(self, import_data, project_id=None):
        """
        Creates new session(s) from imported JSON data.
        Handles:
        1. Legacy format: List of message dicts
        2. Single Session format: Dict with 'messages'
        3. Project format: Dict with 'export_type': 'project' and 'sessions' list
        """
        if not import_data:
            return False

        # Handle Project Export (Multiple Sessions)
        if isinstance(import_data, dict) and import_data.get('export_type') == 'project':
            sessions_to_import = import_data.get('sessions', [])
            if not sessions_to_import:
                return False
            
            # Use project_id if provided, otherwise use the project name from export
            target_project_id = project_id
            if not target_project_id and import_data.get('project_name'):
                existing_project = self.env['sh.ai.chat.project'].search([
                    ('name', '=', import_data.get('project_name')),
                    ('user_id', '=', self.env.user.id)
                ], limit=1)
                if existing_project:
                    target_project_id = existing_project.id
                else:
                    new_project = self.env['sh.ai.chat.project'].create({
                        'name': import_data.get('project_name'),
                        'user_id': self.env.user.id
                    })
                    target_project_id = new_project.id

            last_imported_session = False
            for session_data in sessions_to_import:
                last_imported_session = self.import_session_from_json(session_data, project_id=target_project_id)
            
            return last_imported_session

        # Handle Single Session (either legacy list or new dict)
        messages_data = []
        session_name = _("Imported Chat %s") % fields.Datetime.now().strftime('%Y-%m-%d %H:%M')
        json_project_name = False

        if isinstance(import_data, list):
            # Legacy format
            messages_data = import_data
        elif isinstance(import_data, dict):
            # New format
            messages_data = import_data.get('messages', [])
            session_name = import_data.get('session_name') or session_name
            json_project_name = import_data.get('project_name')
        else:
            return False

        if not messages_data:
            return False

        # Determine target project
        target_project_id = project_id
        if not target_project_id and json_project_name:
            # Try to find or create project by name if not explicitly provided
            existing_project = self.env['sh.ai.chat.project'].search([
                ('name', '=', json_project_name),
                ('user_id', '=', self.env.user.id)
            ], limit=1)
            if existing_project:
                target_project_id = existing_project.id
            else:
                new_project = self.env['sh.ai.chat.project'].create({
                    'name': json_project_name,
                    'user_id': self.env.user.id
                })
                target_project_id = new_project.id

        # Create a new session
        session_vals = {
            'name': session_name,
            'user_id': self.env.user.id,
        }
        if target_project_id:
            project = self.env['sh.ai.chat.project'].browse(target_project_id).exists()
            if project and project.user_id == self.env.user:
                session_vals['project_id'] = project.id
        
        # Try to detect LLM from the first assistant message if possible
        found_llm_id = False
        default_llm = self.env['sh.ai.llm'].search([('is_default', '=', True)], limit=1)
        if default_llm:
            found_llm_id = default_llm.id

        # Helper to find LLM by xml_id, code, or name
        def find_llm(xml_id, code, name):
            if xml_id:
                # Try finding by External ID first (most robust)
                rec = self.env.ref(xml_id, raise_if_not_found=False)
                if rec and rec._name == 'sh.ai.llm':
                    return rec.id
            if code:
                rec = self.env['sh.ai.llm'].search([('sh_model_code', '=', code)], limit=1)
                if rec: return rec.id
            if name:
                rec = self.env['sh.ai.llm'].search([('name', '=', name)], limit=1)
                if rec: return rec.id
            return False

        # Pre-scan messages to find common LLM
        for msg in messages_data:
            detected = find_llm(msg.get('llm_xml_id'), msg.get('llm_code'), msg.get('llm'))
            if detected:
                found_llm_id = detected
                break
        
        session_vals['llm_id'] = found_llm_id
        session = self.create(session_vals)

        # Create messages
        Message = self.env['sh.ai.chat.message']
        for msg in messages_data:
            content = msg.get('content', '')
            if not content:
                continue

            # Determine type
            msg_type = msg.get('author') or msg.get('type')
            if msg_type not in ['user', 'assistant', 'system', 'error']:
                msg_type = 'user' # Default fallback

            # Determine specific LLM for this message
            msg_llm_id = found_llm_id
            detected_specific = find_llm(msg.get('llm_xml_id'), msg.get('llm_code'), msg.get('llm'))
            if detected_specific:
                msg_llm_id = detected_specific

            # Get action_data, but only if it's a valid dict (not False or None)
            action_data = msg.get('action_data')
            if action_data and isinstance(action_data, dict):
                action_data_value = action_data
            else:
                action_data_value = None

            Message.create({
                'session_id': session.id,
                'content': content,
                'message_type': msg_type,
                'llm_id': msg_llm_id if msg_type != 'user' else False, # Only assign LLM to AI messages
                'action_data': action_data_value,  # Preserve action icons functionality
                'debug_info': msg.get('debug_info'),
                'sh_is_cancelled': msg.get('sh_is_cancelled', False),
                'prompt_tokens': msg.get('prompt_tokens', 0),
                'completion_tokens': msg.get('completion_tokens', 0),
                'total_tokens': msg.get('total_tokens', 0),
                'tool_call_count': msg.get('tool_call_count', 0),
                'execution_time': msg.get('execution_time', 0),
            })


        return {
            'id': session.id,
            'name': session.name,
            'access_token': session.access_token,
        }
