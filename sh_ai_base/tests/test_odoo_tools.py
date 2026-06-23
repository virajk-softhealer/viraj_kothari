# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies.

from odoo.tests.common import TransactionCase
from odoo.addons.sh_ai_base.provider.odoo_tools import (
    _validate_and_fix_domain,
    aggregate_records,
    fuzzy_lookup,
    search_records,
)

class TestOdooTools(TransactionCase):

    def setUp(self):
        super(TestOdooTools, self).setUp()
        # Create a test partner (Contact) with UNIQUE name
        self.partner_azure = self.env['res.partner'].create({
            'name': 'UniqueAzureInterior',
        })
        # Create a test category
        self.tag_senior = self.env['res.partner.category'].create({
            'name': 'UniqueSeniorTag',
        })
        self.tag_duplicate_one = self.env['res.partner.category'].create({
            'name': 'UniqueDuplicateTag',
        })
        self.tag_duplicate_two = self.env['res.partner.category'].create({
            'name': 'UniqueDuplicateTag',
        })
        self.partner_azure.category_id = [(4, self.tag_senior.id)]
        self.country_alpha = self.env['res.country'].create({
            'name': 'UniqueAlphaLand',
            'code': 'XA',
        })
        self.country_beta = self.env['res.country'].create({
            'name': 'UniqueBetaLand',
            'code': 'XB',
        })
        self.partner_alpha_one = self.env['res.partner'].create({
            'name': 'UniqueGroupedPartnerAlphaOne',
            'country_id': self.country_alpha.id,
        })
        self.partner_alpha_two = self.env['res.partner'].create({
            'name': 'UniqueGroupedPartnerAlphaTwo',
            'country_id': self.country_alpha.id,
        })
        self.partner_beta_one = self.env['res.partner'].create({
            'name': 'UniqueGroupedPartnerBetaOne',
            'country_id': self.country_beta.id,
        })

    def test_fuzzy_lookup_exact(self):
        """Test exact match with fuzzy_lookup"""
        result = fuzzy_lookup(self.env, 'res.partner', 'UniqueAzureInterior')
        self.assertTrue(result['success'])
        self.assertEqual(len(result['results']), 1)
        self.assertEqual(result['results'][0]['id'], self.partner_azure.id)

    def test_fuzzy_lookup_with_suffix(self):
        """Test match after stripping 'Contact' suffix"""
        result = fuzzy_lookup(self.env, 'res.partner', 'UniqueAzureInterior Contact')
        self.assertTrue(result['success'])
        self.assertEqual(result['cleaned_term'], 'UniqueAzureInterior')
        self.assertEqual(len(result['results']), 1)
        self.assertEqual(result['results'][0]['id'], self.partner_azure.id)

    def test_validate_and_fix_domain_many2one(self):
        """Test that string values in Many2one domains are converted to IDs"""
        domain = [['partner_id', '=', 'UniqueAzureInterior Contact']]
        fixed_domain, corrections = _validate_and_fix_domain(self.env, 'res.users', domain)
        
        self.assertEqual(fixed_domain, [['partner_id', '=', self.partner_azure.id]])
        self.assertTrue(any("Fixed partner_id" in c for c in corrections))

    def test_validate_and_fix_domain_many2many(self):
        """Test Many2many resolution"""
        domain = [['category_id', 'in', 'UniqueSeniorTag']]
        fixed_domain, corrections = _validate_and_fix_domain(self.env, 'res.partner', domain)
        
        self.assertEqual(fixed_domain, [['category_id', 'in', [self.tag_senior.id]]])

    def test_validate_and_fix_domain_many2one_preserves_operator(self):
        """Many2one resolution should preserve negative operators."""
        domain = [['country_id', '!=', 'UniqueAlphaLand']]
        fixed_domain, corrections = _validate_and_fix_domain(self.env, 'res.partner', domain)

        self.assertEqual(fixed_domain, [['country_id', '!=', self.country_alpha.id]])
        self.assertTrue(any("Fixed country_id" in c for c in corrections))

    def test_validate_and_fix_domain_many2many_preserves_negative_operator(self):
        """Many2many resolution should preserve negative operators."""
        domain = [['category_id', 'not in', 'UniqueSeniorTag']]
        fixed_domain, corrections = _validate_and_fix_domain(self.env, 'res.partner', domain)

        self.assertEqual(fixed_domain, [['category_id', 'not in', [self.tag_senior.id]]])
        self.assertTrue(any("Fixed category_id" in c for c in corrections))

    def test_validate_and_fix_domain_dotted_relational_path(self):
        """Dotted many2one paths should resolve to IDs dynamically."""
        domain = [['partner_id.country_id', '=', 'UniqueAlphaLand']]
        fixed_domain, corrections = _validate_and_fix_domain(self.env, 'res.users', domain)

        self.assertEqual(fixed_domain, [['partner_id.country_id', '=', self.country_alpha.id]])
        self.assertTrue(any("Fixed partner_id.country_id" in c for c in corrections))

    def test_validate_and_fix_domain_ambiguous_match_keeps_original_clause(self):
        """Ambiguous fuzzy matches should not silently rewrite the domain."""
        domain = [['category_id', 'in', 'UniqueDuplicateTag']]
        fixed_domain, corrections = _validate_and_fix_domain(self.env, 'res.partner', domain)

        self.assertEqual(fixed_domain, domain)
        self.assertTrue(any("Ambiguous match" in c for c in corrections))

    def test_validate_and_fix_domain_no_match(self):
        """Test that original domain is kept if no match is found"""
        # Use a very random string to avoid partial hits on "Partner"
        domain = [['partner_id', '=', 'XYZ_NONEXISTENT_PARTNER_ABC']]
        fixed_domain, corrections = _validate_and_fix_domain(self.env, 'res.users', domain)
        
        self.assertEqual(fixed_domain, domain)
        self.assertTrue(any("Could not resolve" in c for c in corrections))

    def test_aggregate_records_grouped_count_counts_records(self):
        """Grouped count should count records per group, not distinct group values."""
        domain = [[
            'id',
            'in',
            [
                self.partner_alpha_one.id,
                self.partner_alpha_two.id,
                self.partner_beta_one.id,
            ],
        ]]

        result = aggregate_records(
            self.env,
            'res.partner',
            operation='count',
            domain=domain,
            field='id',
            group_by='country_id',
        )

        self.assertTrue(result['success'])
        self.assertEqual(
            result['groups'],
            [
                {'group': 'UniqueAlphaLand', 'value': 2},
                {'group': 'UniqueBetaLand', 'value': 1},
            ],
        )

    def test_search_records_group_by_is_explicit_ui_hint(self):
        """search_records should be explicit that group_by does not aggregate."""
        result = search_records(
            self.env,
            'res.partner',
            domain=[['id', 'in', [self.partner_alpha_one.id, self.partner_beta_one.id]]],
            fields=['name'],
            group_by='country_id',
            limit=10,
        )

        self.assertTrue(result['success'])
        self.assertEqual(result['group_by'], 'country_id')
        self.assertEqual(result['group_by_mode'], 'ui_hint')
        self.assertIn('aggregate_records', result['note'])

    def test_search_records_respects_context_default_and_max_limits(self):
        """search_records should use configurable context limits when present."""
        limited_env = self.env(context=dict(
            self.env.context,
            sh_ai_search_default_limit=1,
            sh_ai_search_max_limit=2,
        ))
        domain = [['id', 'in', [self.partner_alpha_one.id, self.partner_alpha_two.id, self.partner_beta_one.id]]]

        default_result = search_records(
            limited_env,
            'res.partner',
            domain=domain,
            fields=['name'],
            limit=None,
            order='id asc',
        )
        capped_result = search_records(
            limited_env,
            'res.partner',
            domain=domain,
            fields=['name'],
            limit=99,
            order='id asc',
        )

        self.assertTrue(default_result['success'])
        self.assertEqual(default_result['limit'], 1)
        self.assertEqual(default_result['count'], 1)
        self.assertTrue(capped_result['success'])
        self.assertEqual(capped_result['limit'], 2)
        self.assertEqual(capped_result['count'], 2)
