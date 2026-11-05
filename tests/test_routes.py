from app.main import app


def test_required_mvp_routes_are_registered():
    paths={(method,route.path) for route in app.routes if hasattr(route,'methods') for method in route.methods}
    expected={
        ('POST','/auth/signup'),('POST','/auth/login'),('GET','/auth/me'),
        ('POST','/companies'),('POST','/companies/{company_id}/context'),
        ('GET','/companies/{company_id}/status'),('GET','/companies/{company_id}/conversation'),
        ('POST','/companies/{company_id}/gap-questions'),('POST','/companies/{company_id}/gap-answers'),
        ('POST','/companies/{company_id}/continue'),('GET','/companies/{company_id}/products'),
        ('GET','/products/{product_id}'),('PATCH','/products/{product_id}'),
        ('POST','/products/{product_id}/verify'),('POST','/companies/{company_id}/complete'),
        ('GET','/companies/{company_id}/profile'),('PATCH','/companies/{company_id}/profile'),
    }
    assert not (expected - paths), f'missing: {expected-paths}'
