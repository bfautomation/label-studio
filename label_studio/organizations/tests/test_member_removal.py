import pytest
from jwt_auth.models import LSAPIToken
from rest_framework.test import APIClient
from tests.jwt_auth.utils import create_user_with_token_settings
from tests.utils import mock_feature_flag
from users.models import User

pytestmark = pytest.mark.django_db


@mock_feature_flag(flag_name='fflag__feature_develop__prompts__dia_1829_jwt_token_auth', value=True)
def test_removed_member_is_deactivated_and_loses_api_access():
    """Removing a member offboards them: they are deactivated and their existing API credentials stop working."""
    owner = create_user_with_token_settings(api_tokens_enabled=True, legacy_api_tokens_enabled=False)
    organization = owner.active_organization
    member = User.objects.create(email='member@example.com', active_organization=organization)
    organization.add_user(member)
    member_client = APIClient()
    member_client.credentials(HTTP_AUTHORIZATION=f'Bearer {LSAPIToken.for_user(member).access_token}')
    assert member_client.get('/api/projects/').status_code == 200

    owner_client = APIClient()
    owner_client.force_authenticate(user=owner)
    response = owner_client.delete(f'/api/organizations/{organization.id}/memberships/{member.id}/')

    assert response.status_code == 204
    member.refresh_from_db()
    assert not member.is_active
    assert member_client.get('/api/projects/').status_code == 401
