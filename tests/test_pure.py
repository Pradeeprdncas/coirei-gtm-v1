from app.services.rag.core import chunk_text
from app.services.scrape.core import canonical, within, parse_html, type_of
from app.services.extract.core import pricing_lines, exact_source
from app.services.common import safe_name
from app.routers.products import blockers


class MockProduct:
    candidate_kind='standalone_product'
    name='Example AI'
    description='Real description'
    key_features=['Feature 1']
    main_use_case='Support customers'
    field_meta={'description':{'review':'accepted'},'key_features':{'review':'accepted'},
                'main_use_case':{'review':'accepted'}}


def test_chunks():
    chunks=chunk_text('A useful heading\n\n'+'Some sentence. '*200)
    assert chunks and all(len(x)<1300 for x in chunks)


def test_link_guard():
    assert within('https://www.example.com/products/a','example.com')
    assert not within('https://example.com.evil.tld/pricing','example.com')
    assert canonical('https://EXAMPLE.com/products?utm=foo#section')=='https://example.com/products'


def test_html():
    page=parse_html('<html><head><title>Acme Product</title></head><body><h1>Acme Product</h1>'
                    '<a href="/pricing">Pricing</a><p>Acme helps teams build agents.</p></body></html>',
                    'https://example.com/')
    assert page.title=='Acme Product'
    assert ('https://example.com/pricing','Pricing') in page.links


def test_pricing_rejects_comparison():
    assert pricing_lines('Competitor comparison\nRival plan $29/month')==[]


def test_no_numeric_product_and_no_faq():
    assert not safe_name('0')
    assert not safe_name('FAQ')
    assert safe_name('Coirei GTM')


def test_verification_requires_review():
    p=MockProduct()
    assert not blockers(p)
    p.field_meta['key_features']={'review':'pending'}
    assert 'review_key_features' in blockers(p)
    p.candidate_kind='suite'
    assert 'not_standalone_product' in blockers(p)
