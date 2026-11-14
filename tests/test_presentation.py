"""Covers app/services/presentation.py — the layer that maps stored product
data onto the names the verification UI binds to."""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services import presentation as pres


def plan(**kw):
    base = dict(id=1, name='Starting at', price_amount=Decimal('36000'), currency='USD',
                unit=None, period='annual_or_monthly_unconfirmed', is_custom=False,
                quote='Starting at $36,000 / yr', source_id=1)
    return SimpleNamespace(**{**base, **kw})


def product(**kw):
    base = dict(id=12, name='Cartesia', website_url='https://cartesia.ai/', category=None,
                product_type=None, description='Voices callers love.', key_features=['Low latency'],
                main_use_case='Build voice agents.', positioning='World-class voice.',
                business_model={}, value_proposition={}, target_market={}, icp={},
                status='detected', field_meta={}, verified_at=None,
                updated_at=datetime(2026, 11, 14, 16, 15, tzinfo=timezone.utc))
    return SimpleNamespace(**{**base, **kw})


@pytest.mark.parametrize('stamp, expected', [
    (datetime(2026, 11, 14, 16, 15, tzinfo=timezone.utc), '14 Nov 2026, 04:15 PM'),
    # Single-digit day and hour: the case that tempted a %-d/%#d directive,
    # which is platform-specific and raised ValueError on Windows.
    (datetime(2026, 1, 4, 9, 5, tzinfo=timezone.utc), '4 Jan 2026, 09:05 AM'),
])
def test_last_updated_is_platform_independent(stamp, expected):
    assert pres.last_updated(product(updated_at=stamp)) == expected


def test_last_updated_without_a_timestamp_is_blank_not_none():
    assert pres.last_updated(product(updated_at=None)) == ''


@pytest.mark.parametrize('quote, expected', [
    ('Starting at $36,000 / yr', '/year'),
    ('Starting at $5,333 / mo', '/month'),
    ('billed per year', '/year'),
])
def test_billing_period_reads_the_quote_not_the_unconfirmed_sentinel(quote, expected):
    # 'annual_or_monthly_unconfirmed' contains the substring "monthly", so
    # matching against it labelled every yearly plan /month.
    assert pres.billing_period(plan(quote=quote)) == expected


def test_custom_plan_has_no_billing_period():
    assert pres.billing_period(plan(is_custom=True)) == ''


def test_money_formats_thousands_and_custom():
    assert pres.money(Decimal('36000'), 'USD', False) == '$36,000'
    assert pres.money(Decimal('19.99'), 'USD', False) == '$19.99'
    assert pres.money(None, 'USD', True) == 'Custom'


def test_icp_aliases_resolve_and_extras_are_kept():
    p = product(icp={'target_audience': 'RevOps teams', 'company_size': 'Mid-market',
                     'compliance_needs': 'HIPAA, SOC 2'})
    view = pres.icp_view(p, p.field_meta)
    # snake_case from the extractor must reach the camelCase name the UI binds.
    assert view['targetAudience'] == 'RevOps teams'
    assert view['companySize'] == 'Mid-market'
    # A key with no fixed UI slot is surfaced, not dropped.
    assert view['additional'] == {'compliance_needs': 'HIPAA, SOC 2'}


def test_empty_sections_use_blanks_never_none():
    p = product()
    for view in (pres.icp_view(p, {}), pres.target_market_view(p, {}),
                 pres.value_proposition_view(p, {})):
        assert None not in view.values()


def test_overview_reports_the_strongest_state_of_the_columns_it_renders():
    # An accepted description must not read as not_found just because
    # category is empty.
    p = product(field_meta={'description': {'status': 'found_on_site', 'review': 'accepted',
                                            'confidence': 90, 'source_id': 1, 'quote': 'x'}})
    view = pres.overview_view(p, p.field_meta)
    assert view['status'] == 'verified'
    assert view['confidence'] == 90


def test_confidence_falls_back_to_the_score_its_status_implies():
    # A populated field with no tracked entry should not read as 0%.
    p = product(positioning='World-class voice.')
    assert pres.positioning_view(p, {})['confidence'] == 75


def test_tags_prefer_classification_over_plan_names():
    p = product(business_model={'pricing_tiers': ['Free', 'Pro'], 'model': 'SaaS Subscription'},
                target_market={'industries': ['Healthcare', 'Fintech']})
    tags = pres.product_tags(p, [])
    assert 'Free' not in tags and 'Pro' not in tags
    assert tags[:2] == ['Healthcare', 'Fintech']


def test_pricing_falls_back_to_business_model_tiers_when_no_plan_rows():
    # The real failing case: pricing_items empty, but business_model knows the tiers.
    p = product(business_model={'payment_model': 'monthly',
                                'pricing_tiers': ['Free', 'Pro', 'Enterprise']})
    view = pres.pricing_view(p, {}, [])
    assert [r['name'] for r in view['plans']] == ['Free', 'Pro', 'Enterprise']
    assert view['derived'] is True
    # Rebuilt, not read off a pricing page, so it must still be confirmed.
    assert view['status'] == 'needs_confirmation'


def test_pricing_pairs_tier_names_with_published_amounts():
    p = product(business_model={'plan_name': ['Launch', 'Growth'],
                                'price': ['Starts at $185/mo', 'starting at $495/mo']})
    plans = pres.pricing_view(p, {}, [])['plans']
    assert [(r['name'], r['price'], r['billingPeriod']) for r in plans] == [
        ('Launch', '$185', '/month'), ('Growth', '$495', '/month')]


def test_mismatched_tier_and_price_counts_are_not_paired():
    # Guessing which price belongs to which tier would invent a fact.
    p = product(business_model={'pricing_tiers': ['A', 'B', 'C'], 'price': ['$10/mo']})
    assert [r['price'] for r in pres.pricing_view(p, {}, [])['plans']] == ['', '', '']


def test_company_model_beats_a_product_billing_cycle():
    p = product(business_model={'payment_model': 'monthly'})
    c = SimpleNamespace(business_model={'model': 'SaaS Subscription'}, domain='x.io', industry=None)
    assert pres.pricing_model_label(p, [], c) == 'SaaS Subscription'


def test_tags_fall_back_past_empty_category_and_trim_prose():
    # category/product_type null and target_market has use_cases, not industries.
    p = product(category=None, product_type=None,
                icp={'industry': 'Regulated sectors (healthcare, finance, legal, etc.)'},
                target_market={'use_cases': ['customer support', 'sales outreach']})
    tags = pres.product_tags(p, [])
    assert tags == ['Regulated sectors', 'customer support', 'sales outreach']
