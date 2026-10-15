"""This file and its contents are licensed under the Apache License 2.0. Please see the included NOTICE for copyright information and LICENSE for a copy of the license."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from data_manager.serializers import DataManagerTaskSerializer
from django.db import connection
from django.test.utils import CaptureQueriesContext
from fsm.state_choices import TaskStateChoices
from fsm.tests.factories import TaskStateFactory
from organizations.models import OrganizationMember
from projects.models import Project
from users.models import User
from users.tests.factories import UserFactory

from ..utils import make_annotation, make_prediction, make_task, mock_feature_flag, project_id  # noqa


def _task_state_point_lookup_queries(captured_queries):
    return [
        query['sql']
        for query in captured_queries
        if 'FROM "fsm_taskstate"' in query['sql'] and 'WHERE "fsm_taskstate"."task_id" = ' in query['sql']
    ]


@pytest.mark.django_db
def test_views_tasks_api(business_client, project_id):
    # create
    payload = dict(project=project_id, data={'test': 1})
    response = business_client.post(
        '/api/dm/views/',
        data=json.dumps(payload),
        content_type='application/json',
    )

    assert response.status_code == 201, response.content
    view_id = response.json()['id']

    # no tasks
    response = business_client.get(f'/api/tasks?fields=all&view={view_id}')

    assert response.status_code == 200, response.content
    assert response.json()['total'] == 0
    assert len(response.json()['tasks']) == 0

    project = Project.objects.get(pk=project_id)
    task_data = {'text': 'bbb'}
    task_id = make_task({'data': task_data}, project).id

    annotation_result = {'from_name': 'my_class', 'to_name': 'text', 'type': 'choices', 'value': {'choices': ['pos']}}
    make_annotation({'result': [annotation_result]}, task_id)
    make_annotation(
        {
            'result': [annotation_result],
            'was_cancelled': True,
        },
        task_id,
    )
    prediction_result = {'from_name': 'my_class', 'to_name': 'text', 'type': 'choices', 'value': {'choices': ['pos']}}
    make_prediction(
        {
            'result': [prediction_result],
        },
        task_id,
    )

    response = business_client.get(f'/api/tasks?fields=all&view={view_id}')

    assert response.status_code == 200, response.content
    response_data = response.json()
    assert response_data['total'] == 1
    assert len(response_data['tasks']) == 1
    assert response_data['tasks'][0]['id'] == task_id
    assert response_data['tasks'][0]['data'] == task_data
    assert response_data['tasks'][0]['total_annotations'] == 1
    assert 'annotations_results' in response_data['tasks'][0]
    assert response_data['tasks'][0]['cancelled_annotations'] == 1
    assert response_data['tasks'][0]['total_predictions'] == 1
    assert 'predictions_results' in response_data['tasks'][0]

    num_anno1 = response_data['tasks'][0]['annotations'][0]['id']
    num_anno2 = response_data['tasks'][0]['annotations'][1]['id']
    num_pred = response_data['tasks'][0]['predictions'][0]['id']

    # delete annotations and check counters

    business_client.delete(f'/api/annotations/{num_anno1}')
    business_client.delete(f'/api/annotations/{num_anno2}')

    response = business_client.get(f'/api/tasks?fields=all&view={view_id}')
    assert response.status_code == 200, response.content
    response_data = response.json()
    assert response_data['tasks'][0]['cancelled_annotations'] == 0
    assert response_data['tasks'][0]['total_annotations'] == 0

    # delete prediction and check counters
    business_client.delete(f'/api/predictions/{num_pred}')

    response = business_client.get(f'/api/tasks?fields=all&view={view_id}')
    assert response.status_code == 200, response.content
    response_data = response.json()
    assert response_data['tasks'][0]['cancelled_annotations'] == 0
    assert response_data['tasks'][0]['total_annotations'] == 0
    assert response_data['tasks'][0]['total_predictions'] == 0


@pytest.mark.django_db
def test_tasks_api_annotates_state_when_state_field_flags_enabled(business_client, project_id):
    project = Project.objects.get(pk=project_id)
    task_1 = make_task({'data': {'text': 'one'}}, project)
    task_2 = make_task({'data': {'text': 'two'}}, project)
    TaskStateFactory(task=task_1, state=TaskStateChoices.IN_PROGRESS)
    TaskStateFactory(task=task_2, state=TaskStateChoices.COMPLETED)

    with (
        patch('data_manager.api.flag_set', return_value=True),
        patch('data_manager.serializers.flag_set', return_value=True),
        patch('fsm.queryset_mixins.flag_set', return_value=True),
        patch('fsm.serializer_fields.flag_set', return_value=True),
        CaptureQueriesContext(connection) as queries,
    ):
        response = business_client.get(f'/api/tasks?fields=all&project={project_id}')

    assert response.status_code == 200, response.content
    rows_by_id = {row['id']: row for row in response.json()['tasks']}
    assert rows_by_id[task_1.id]['state'] == TaskStateChoices.IN_PROGRESS
    assert rows_by_id[task_2.id]['state'] == TaskStateChoices.COMPLETED
    assert _task_state_point_lookup_queries(queries) == []


@pytest.mark.parametrize(
    'tasks_count, annotations_count, predictions_count',
    [
        [0, 0, 0],
        [1, 0, 0],
        [1, 1, 1],
        [2, 2, 2],
    ],
)
@pytest.mark.django_db
def test_views_total_counters(tasks_count, annotations_count, predictions_count, business_client, project_id):
    # create
    payload = dict(project=project_id, data={'test': 1})
    response = business_client.post(
        '/api/dm/views/',
        data=json.dumps(payload),
        content_type='application/json',
    )

    assert response.status_code == 201, response.content
    view_id = response.json()['id']

    project = Project.objects.get(pk=project_id)
    for _ in range(0, tasks_count):
        task_id = make_task({'data': {}}, project).id
        print('TASK_ID: %s' % task_id)
        for _ in range(0, annotations_count):
            make_annotation({'result': []}, task_id)

        for _ in range(0, predictions_count):
            make_prediction({'result': []}, task_id)

    response = business_client.get(f'/api/tasks?fields=all&view={view_id}')

    response_data = response.json()

    assert response_data['total'] == tasks_count, response_data
    assert response_data['total_annotations'] == tasks_count * annotations_count, response_data
    assert response_data['total_predictions'] == tasks_count * predictions_count, response_data


@pytest.mark.django_db
@mock_feature_flag(
    'fflag_all_feat_utc_204_users_performance_improvements_in_dm_for_large_orgs',
    True,
    parent_module='data_manager.serializers',
)
def test_tasks_api_embeds_assignee_users_when_global_user_fetching_disabled(business_client, project_id):
    project = Project.objects.get(pk=project_id)
    task = make_task({'data': {'text': 'annotated task'}}, project)

    annotation_result = {'from_name': 'my_class', 'to_name': 'text', 'type': 'choices', 'value': {'choices': ['pos']}}
    make_annotation({'result': [annotation_result], 'completed_by': business_client.user}, task.id)
    task.updated_by = business_client.user
    task.save(update_fields=['updated_by'])

    response = business_client.get(f'/api/tasks?fields=all&project={project_id}')

    assert response.status_code == 200, response.content
    payload = response.json()['tasks'][0]
    annotator = payload['annotators'][0]
    updated_by = payload['updated_by'][0]

    assert annotator['user_id'] == business_client.user.id
    assert annotator['email'] == business_client.user.email
    assert annotator['username'] == business_client.user.username
    assert 'last_activity' in annotator
    assert updated_by['user_id'] == business_client.user.id
    assert updated_by['email'] == business_client.user.email


@pytest.mark.django_db
@mock_feature_flag(
    'fflag_all_feat_utc_204_users_performance_improvements_in_dm_for_large_orgs',
    False,
    parent_module='data_manager.serializers',
)
def test_tasks_api_embeds_annotators_when_global_user_fetching_enabled(business_client, project_id):
    project = Project.objects.get(pk=project_id)
    task = make_task({'data': {'text': 'annotated task'}}, project)

    annotation_result = {'from_name': 'my_class', 'to_name': 'text', 'type': 'choices', 'value': {'choices': ['pos']}}
    make_annotation({'result': [annotation_result], 'completed_by': business_client.user}, task.id)
    task.updated_by = business_client.user
    task.save(update_fields=['updated_by'])

    response = business_client.get(f'/api/tasks?fields=all&project={project_id}')

    assert response.status_code == 200, response.content
    payload = response.json()['tasks'][0]

    assert payload['annotators'][0]['user_id'] == business_client.user.id
    assert payload['annotators'][0]['email'] == business_client.user.email
    assert payload['updated_by'] == [{'user_id': business_client.user.id}]


@pytest.mark.django_db
@mock_feature_flag(
    'fflag_all_feat_utc_204_users_performance_improvements_in_dm_for_large_orgs',
    True,
    parent_module='data_manager.serializers',
)
def test_tasks_api_masks_deleted_embedded_annotators(business_client, project_id):
    project = Project.objects.get(pk=project_id)
    business_client.user.refresh_from_db()
    organization = business_client.user.active_organization
    deleted_user = User.objects.create(
        email='deleted.annotator@example.com',
        username='deleted.annotator',
        first_name='Deleted',
        last_name='Annotator',
    )
    organization_member = organization.add_user(deleted_user)
    deleted_user.active_organization = organization
    deleted_user.save(update_fields=['active_organization'])
    OrganizationMember.objects.get(pk=organization_member.pk).soft_delete()

    task = make_task({'data': {'text': 'annotated task'}}, project)
    annotation_result = {'from_name': 'my_class', 'to_name': 'text', 'type': 'choices', 'value': {'choices': ['pos']}}
    make_annotation({'result': [annotation_result], 'completed_by': deleted_user}, task.id)
    task.updated_by = deleted_user
    task.save(update_fields=['updated_by'])

    response = business_client.get(f'/api/tasks?fields=all&project={project_id}')

    assert response.status_code == 200, response.content
    payload = response.json()['tasks'][0]
    annotator = payload['annotators'][0]
    updated_by = payload['updated_by'][0]

    assert annotator['user_id'] == deleted_user.id
    assert annotator['first_name'] == 'Deleted'
    assert annotator['last_name'] == f'User {deleted_user.id}'
    assert annotator['username'] == f'deleted-{deleted_user.id}'
    assert annotator['email'] == f'deleted-{deleted_user.id}-user@example.com'
    assert updated_by['user_id'] == deleted_user.id
    assert updated_by['email'] == f'deleted-{deleted_user.id}-user@example.com'


@pytest.mark.django_db
@mock_feature_flag(
    'fflag_all_feat_utc_204_users_performance_improvements_in_dm_for_large_orgs',
    True,
    parent_module='data_manager.serializers',
)
def test_embedded_assignee_queries_are_batched_and_have_an_independent_cache(django_assert_num_queries):
    users = UserFactory.create_batch(3)
    tasks = [SimpleNamespace(annotators=[user.id], updated_by_id=user.id) for user in users]
    # Other nested user serializers can cache a smaller representation first.
    context = {'project': None, 'user_cache': {user.id: {'id': user.id} for user in users}}
    serializer = DataManagerTaskSerializer(tasks, many=True, context=context)
    for field in list(serializer.child.fields):
        if field not in {'annotators', 'updated_by'}:
            serializer.child.fields.pop(field)

    with django_assert_num_queries(2):
        result = serializer.data

    for item, user in zip(result, users):
        assert item['annotators'][0]['email'] == user.email
        assert item['updated_by'][0]['email'] == user.email


@pytest.mark.django_db
def test_fit_1658_dm_project_data_lists_embedded_annotator_profiles(business_client, project_id):
    """FIT-1658: same path as Project > Data tab — DM loads tasks for a saved view.

    Production request::

        GET /api/tasks?fields=all&view=<view_id>

    Before the FIT-1658 behavior change, ``annotators`` was a list of integer user
    ids; the Data Manager client expects each item to be an object with profile
    fields (so MobX-State-Tree can build Assignee/User without a global org user
    fetch). This test fails on that old API shape and passes once annotators are
    embedded dicts including ``username`` / ``last_activity`` from
    ``CompletedByDMSerializer``.
    """
    view_resp = business_client.post(
        '/api/dm/views/',
        data=json.dumps({'project': project_id, 'data': {}}),
        content_type='application/json',
    )
    assert view_resp.status_code == 201, view_resp.content
    view_id = view_resp.json()['id']

    project = Project.objects.get(pk=project_id)
    task = make_task({'data': {'text': 'fit-1658-dm'}}, project)

    annotator = business_client.user
    annotation_result = {
        'from_name': 'test_batch_predictions',
        'to_name': 'text',
        'type': 'choices',
        'value': {'choices': ['class_A']},
    }
    make_annotation({'result': [annotation_result], 'completed_by': annotator}, task.id)

    response = business_client.get(f'/api/tasks?fields=all&view={view_id}')
    assert response.status_code == 200, response.content
    body = response.json()
    task_row = next(row for row in body['tasks'] if row['id'] == task.id)
    ann = task_row['annotators']
    assert len(ann) >= 1
    row = ann[0]
    assert isinstance(row, dict), 'annotators must be objects for Data Manager (not bare user id ints)'
    assert row['user_id'] == annotator.id
    assert row['email'] == annotator.email
    for key in ('username', 'last_activity'):
        assert key in row, f'embedded annotator must include {key} for DM MST User'
