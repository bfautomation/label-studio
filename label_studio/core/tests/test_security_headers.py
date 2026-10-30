import pytest

pytestmark = pytest.mark.django_db

ONE_YEAR = 'max-age=31536000'


def test_plain_http_response_forbids_cross_site_framing_without_hsts(client):
    """Over plain HTTP the app still blocks other sites from framing it, but sends no HSTS."""
    response = client.get('/health/')

    assert response['X-Frame-Options'] == 'SAMEORIGIN'
    assert 'Strict-Transport-Security' not in response


def test_https_response_carries_hsts_and_frame_protection(client):
    """Over HTTPS the app tells browsers to stay on HTTPS and still blocks cross-site framing."""
    response = client.get('/health/', secure=True)

    assert response['Strict-Transport-Security'] == ONE_YEAR
    assert response['X-Frame-Options'] == 'SAMEORIGIN'
