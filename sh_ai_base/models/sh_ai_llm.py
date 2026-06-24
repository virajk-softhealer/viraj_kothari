# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies Pvt. Ltd.

import base64
import json
import ssl
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import certifi
from odoo import fields, models, api
from odoo.exceptions import ValidationError
from odoo.tools.misc import file_path

class ShAiLlm(models.Model):
    _name = 'sh.ai.llm'
    _description = 'SH AI LLM Provider'
    _order = 'name'

    name = fields.Char(string="LLM Name", required=True,
                      help="Display name for this AI model (e.g. 'Gemini 2.5 Pro')")
    sh_company = fields.Char(string="Provider", required=False,
                            help="AI provider company (e.g. 'Google', 'OpenAI', 'Anthropic')")
    sh_model_code = fields.Char(string="Model Code", required=False,
                               help="Technical model identifier for API calls (e.g. 'gemini-2.5-pro' or 'gpt-4o')")
    provider_type = fields.Selection(
        [
            ('direct', 'Direct'),
            ('openrouter', 'OpenRouter'),
        ],
        string="Provider Type",
        required=True,
        default='direct',
        help="Choose whether to call the provider directly or through OpenRouter.",
    )
    openrouter_model_id = fields.Many2one(
        'sh.ai.openrouter.model',
        string="OpenRouter Model",
        ondelete='set null',
        help="Select a tool-capable OpenRouter model when Provider Type is OpenRouter.",
    )

    def _detect_provider_type(self):
        """Auto-detect provider type based on model code"""
        self.ensure_one()
        if self.provider_type == 'openrouter':
            return 'openrouter'

        model_code = (self.sh_model_code or '').lower()

        # DeepSeek model patterns
        if any(pattern in model_code for pattern in ['deepseek']):
            return 'deepseek'

        # Claude model patterns
        if any(pattern in model_code for pattern in ['claude']):
            return 'claude'

        # OpenAI model patterns
        if any(pattern in model_code for pattern in ['gpt-', 'o1', 'o3', 'chatgpt']):
            return 'openai'

        # Default to Gemini
        return 'gemini'
    sh_api_key = fields.Char(string="API Key", groups="sh_ai_base.group_sh_ai_manager",
                            help="Your API key from the provider. This will be used to authenticate API requests.")
    image = fields.Image(string="LLM Icon",
                        help="Upload an icon/logo for this LLM provider. This will be displayed in the chat interface.")
    active = fields.Boolean(string="Active", default=True,
                           help="Uncheck to disable this provider temporarily")
    is_default = fields.Boolean(string="Default LLM", default=False,
                               help="Mark this as the default LLM for new chat sessions")
    temperature = fields.Selection([
        ('precise', 'Precise (0.2) - Best for data queries and accuracy'),
        ('balanced', 'Balanced (0.5) - Good for general use'),
        ('creative', 'Creative (0.8) - Best for text generation'),
    ], string="Response Style", default='precise',
       help="Controls AI creativity vs consistency. Use Precise for accurate data queries.")
    
    
    is_gpt_5_model = fields.Boolean(string="Is GPT-5", compute="_compute_is_gpt_5_model")

    @api.depends('sh_model_code')
    def _compute_is_gpt_5_model(self):
        for llm in self : 
            if llm.sh_model_code and 'gpt-5' in llm.sh_model_code :
                llm.is_gpt_5_model = True 
            else : 
                llm.is_gpt_5_model = False 

    is_openai_model = fields.Boolean(string="Is OpenAI Model", compute="_compute_is_openai_model")
    sh_reasoning_effort = fields.Selection([
        ('minimal', 'Minimal'),
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
    ], string="Reasoning Effort", default='low',
       help="For reasoning models (like o1, o3, gpt-5), controls how much effort the model spends on reasoning.")

    @api.depends('sh_model_code')
    def _compute_is_openai_model(self):
        for llm in self:
            llm.is_openai_model = llm._detect_provider_type() == 'openai'

    is_reasoning_model = fields.Boolean(string="Is Reasoning Model", compute="_compute_is_reasoning_model")

    @api.depends('sh_model_code')
    def _compute_is_reasoning_model(self):
        for llm in self:
            model_code = (llm.sh_model_code or '').lower()
            # Reasoning models: OpenAI o1, o3, GPT-5, or Gemini "thinking" models
            llm.is_reasoning_model = True

    @api.model
    def _get_openrouter_image_data(self):
        image_path = file_path('sh_ai_base/static/img/openrouter.png', filter_ext=('.png',))
        with open(image_path, 'rb') as image_file:
            return base64.b64encode(image_file.read())

    @api.onchange('provider_type')
    def _onchange_provider_type(self):
        for llm in self:
            if llm.provider_type == 'direct':
                llm.openrouter_model_id = False
                continue
            llm.image = self._get_openrouter_image_data()

    @api.onchange('openrouter_model_id')
    def _onchange_openrouter_model_id(self):
        for llm in self:
            if llm.provider_type != 'openrouter' or not llm.openrouter_model_id:
                continue
            llm.sh_company = llm.openrouter_model_id.provider_name
            llm.sh_model_code = llm.openrouter_model_id.model_code
            llm.image = self._get_openrouter_image_data()

    @api.constrains('provider_type', 'sh_company', 'sh_model_code', 'openrouter_model_id')
    def _check_provider_configuration(self):
        for llm in self:
            if llm.provider_type == 'openrouter':
                continue
            if not (llm.sh_company or '').strip():
                raise ValidationError("Provider is required when Provider Type is Direct.")
            if not (llm.sh_model_code or '').strip():
                raise ValidationError("Model Code is required when Provider Type is Direct.")

    def _apply_openrouter_selection_vals(self, vals):
        provider_type = vals.get('provider_type')
        if provider_type is None and len(self) == 1:
            provider_type = self.provider_type

        openrouter_model_id = vals.get('openrouter_model_id')
        if openrouter_model_id is None and len(self) == 1:
            openrouter_model_id = self.openrouter_model_id.id

        if provider_type != 'openrouter':
            return vals

        if openrouter_model_id:
            openrouter_model = self.env['sh.ai.openrouter.model'].browse(openrouter_model_id)
            if openrouter_model.exists():
                vals['sh_company'] = openrouter_model.provider_name
                vals['sh_model_code'] = openrouter_model.model_code
        if not vals.get('image'):
            vals['image'] = self._get_openrouter_image_data()
        return vals

    @api.model_create_multi
    def create(self, vals_list):
        normalized_vals_list = []
        for vals in vals_list:
            normalized_vals_list.append(self._apply_openrouter_selection_vals(dict(vals)))
        records = super().create(normalized_vals_list)
        defaults = records.filtered('is_default')
        for rec in defaults:
            rec._unset_others_default()
        return records

    def write(self, vals):
        update_provider_fields = 'provider_type' in vals or 'openrouter_model_id' in vals
        if len(self) > 1 and update_provider_fields:
            res = True
            for record in self:
                normalized_vals = record._apply_openrouter_selection_vals(dict(vals))
                res = super(ShAiLlm, record).write(normalized_vals) and res
        else:
            normalized_vals = self._apply_openrouter_selection_vals(dict(vals))
            res = super().write(normalized_vals)
        if 'is_default' in vals and vals['is_default'] is True:
            for rec in self:
                rec._unset_others_default()            
        return res

    def _fetch_openrouter_models_payload(self):
        self.ensure_one()
        api_key = (self.sh_api_key or '').strip()
        if not api_key:
            raise ValidationError("Please configure the OpenRouter API key before fetching models.")

        request = Request(
            "https://openrouter.ai/api/v1/models?supported_parameters=tools",
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer %s" % api_key,
                "User-Agent": "Odoo19 SH AI Base",
            },
        )
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        try:
            with urlopen(request, timeout=30, context=ssl_context) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            raise ValueError("OpenRouter request failed with status %s." % error.code) from error
        except URLError as error:
            raise ValueError("OpenRouter request failed: %s" % error.reason) from error
        except Exception as error:
            raise ValueError("Unable to fetch OpenRouter models: %s" % error) from error

        models_payload = response_payload.get("data") or []
        if not isinstance(models_payload, list):
            raise ValueError("OpenRouter returned an invalid models payload.")
        return models_payload

    def action_fetch_openrouter_models(self):
        self.ensure_one()
        try:
            payloads = self._fetch_openrouter_models_payload()
            sync_result = self.env['sh.ai.openrouter.model'].sudo().sync_from_openrouter_payload(payloads)
        except ValidationError:
            raise
        except ValueError as error:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "OpenRouter Sync Failed",
                    "message": str(error),
                    "type": "danger",
                    "sticky": False,
                },
            }

        message = "Added %s new function-calling model(s)." % sync_result["created_count"]
        if sync_result["existing_count"]:
            message += " Skipped %s existing model(s)." % sync_result["existing_count"]
        if sync_result["skipped_count"]:
            message += " Skipped %s unsupported model(s)." % sync_result["skipped_count"]

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "OpenRouter Sync Complete",
                "message": message,
                "type": "success",
                "sticky": False,
            },
        }

    def _unset_others_default(self):
        """Ensure this is the only default"""
        self.search([
            ('id', '!=', self.id),
        ]).write({'is_default': False})
