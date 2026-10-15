import logging
import time
import traceback
from typing import Callable, Optional

from core.feature_flags import flag_set
from core.redis import get_job_health
from core.utils.common import load_func
from core.utils.exceptions import extract_message
from data_import.document_preprocessing import (
    DocumentPreprocessingError,
    cleanup_persisted_document_artifact_files,
    persist_document_conversion_artifact,
    preprocess_uploaded_document,
    validate_document_extension,
    validate_document_file_size,
)
from data_import.uploader import load_tasks_for_async_import_streaming
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from label_studio_sdk.label_interface import LabelInterface
from projects.functions.utils import recalculate_created_annotations_and_labels_from_scratch
from projects.import_health import (
    claim_import_job_in_progress,
    mark_import_job_finished,
    update_import_job_progress,
)
from projects.models import ProjectImport, ProjectReimport, ProjectSummary
from rest_framework.exceptions import ValidationError
from tasks.models import Annotation, Prediction, Task
from tasks.serializers import (
    FF_BROS_1092_IMPORT_UNKNOWN_COMPLETED_BY,
    FF_IMPORT_BULK_TASK_STATES,
    TaskSerializerBulk,
    sanitize_prediction_import_payload,
)
from users.models import User
from webhooks.models import WebhookAction
from webhooks.utils import emit_webhooks_for_instance

from .models import FileUpload
from .serializers import ImportApiSerializer
from .uploader import load_tasks_for_async_import

logger = logging.getLogger(__name__)

DOCUMENT_UPLOAD_QUEUE_NAME = 'high'


def async_import_background(
    import_id, user_id, recalculate_stats_func: Optional[Callable[..., None]] = None, **kwargs
):
    with transaction.atomic():
        try:
            project_import = ProjectImport.objects.get(id=import_id)
        except ProjectImport.DoesNotExist:
            logger.error(f'ProjectImport with id {import_id} not found, import processing failed')
            return
        # Claim CREATED, or reclaim a false health-check FAILED so a live worker can run.
        if not claim_import_job_in_progress(project_import):
            logger.error(f'Processing import with id {import_id} already started')
            return

    user = User.objects.get(id=user_id)

    if flag_set('fflag_fix_back_plt_902_async_import_background_oom_fix_22092025_short', user='auto'):
        logger.info(f'Using streaming import for project {project_import.project.id}')
        _async_import_background_streaming(project_import, user)
        return

    start = time.time()
    project = project_import.project
    tasks = None
    # upload files from request, and parse all tasks
    # TODO: Stop passing request to load_tasks function, make all validation before
    tasks, file_upload_ids, found_formats, data_columns = load_tasks_for_async_import(project_import, user)

    if project_import.preannotated_from_fields:
        # turn flat task JSONs {"column1": value, "column2": value} into {"data": {"column1"..}, "predictions": [{..."column2"}]
        raise_errors = flag_set(
            'fflag_feat_utc_210_prediction_validation_15082025', user=project.organization.created_by
        )
        logger.info(f'Reformatting predictions with raise_errors: {raise_errors}')
        tasks = reformat_predictions(tasks, project_import.preannotated_from_fields, project, raise_errors)

    # Always validate predictions regardless of commit_to_project setting
    if project.label_config_is_not_default and flag_set(
        'fflag_feat_utc_210_prediction_validation_15082025', user=project.organization.created_by
    ):
        validation_errors = []
        li = LabelInterface(project.label_config)

        for i, task in enumerate(tasks):
            if 'predictions' in task:
                for j, prediction in enumerate(task['predictions']):
                    try:
                        prediction = sanitize_prediction_import_payload(prediction)
                        validation_errors_list = li.validate_prediction(prediction, return_errors=True)
                        if validation_errors_list:
                            for error in validation_errors_list:
                                validation_errors.append(f'Task {i}, prediction {j}: {error}')
                    except Exception as e:
                        error_msg = f'Task {i}, prediction {j}: Error validating prediction - {str(e)}'
                        validation_errors.append(error_msg)
                        logger.error(f'Exception during validation: {error_msg}')

        if validation_errors:
            error_message = f'Prediction validation failed ({len(validation_errors)} errors):\n'
            for error in validation_errors:
                error_message += f'- {error}\n'

            if flag_set('fflag_feat_utc_210_prediction_validation_15082025', user=project.organization.created_by):
                mark_import_job_finished(project_import, ProjectImport.Status.FAILED, error=error_message)
                return
            else:
                logger.error(
                    f'Prediction validation failed, not raising error - ({len(validation_errors)} errors):\n{error_message}'
                )

    if project_import.commit_to_project:
        with transaction.atomic():
            # Lock summary for update to avoid race conditions
            summary = ProjectSummary.objects.select_for_update().get(project=project)

            # Immediately create project tasks and update project states and counters
            # BROS-1092: pass user so BaseTaskSerializerBulk can attribute unknown
            # completed_by entries back to the importer instead of project.created_by.
            ctx = {'project': project}
            if flag_set(FF_BROS_1092_IMPORT_UNKNOWN_COMPLETED_BY, user=user):
                ctx['user'] = user
            serializer = ImportApiSerializer(data=tasks, many=True, context=ctx)
            serializer.is_valid(raise_exception=True)

            try:
                tasks = serializer.save(project_id=project.id)
                emit_webhooks_for_instance(user.active_organization, project, WebhookAction.TASKS_CREATED, tasks)

                task_count = len(tasks)
                annotation_count = len(serializer.db_annotations)
                prediction_count = len(serializer.db_predictions)
                # Update counters (like total_annotations) for new tasks and after bulk update tasks stats. It should be a
                # single operation as counters affect bulk is_labeled update

                recalculate_stats_counts = {
                    'task_count': task_count,
                    'annotation_count': annotation_count,
                    'prediction_count': prediction_count,
                }

                project.update_tasks_counters_and_task_states(
                    tasks_queryset=tasks,
                    maximum_annotations_changed=False,
                    overlap_cohort_percentage_changed=False,
                    tasks_number_changed=True,
                    recalculate_stats_counts=recalculate_stats_counts,
                )
                logger.info('Tasks bulk_update finished (async import)')

                summary.update_data_columns(tasks)
                # TODO: summary.update_created_annotations_and_labels
            except Exception as e:
                # Handle any other unexpected errors during task creation
                mark_import_job_finished(
                    project_import, ProjectImport.Status.FAILED, error=f'Error creating tasks: {str(e)}'
                )
                return
    else:
        # Do nothing - just output file upload ids for further use
        task_count = len(tasks)
        annotation_count = None
        prediction_count = None

    duration = time.time() - start

    finish_kwargs = {
        'task_count': task_count or 0,
        'annotation_count': annotation_count or 0,
        'prediction_count': prediction_count or 0,
        'file_upload_ids': file_upload_ids,
        'found_formats': found_formats,
        'data_columns': data_columns,
    }
    if project_import.return_task_ids:
        finish_kwargs['task_ids'] = [task.id for task in tasks]

    # Single conditional UPDATE — avoids a second save that could race with job_id writes.
    mark_import_job_finished(project_import, ProjectImport.Status.COMPLETED, duration=duration, **finish_kwargs)


def set_import_background_failure(job, connection, type, value, _):
    import_id = job.args[0]
    try:
        project_import = ProjectImport.objects.get(id=import_id)
    except ProjectImport.DoesNotExist:
        return
    mark_import_job_finished(
        project_import,
        ProjectImport.Status.FAILED,
        error=str(value),
        traceback_text=traceback.format_exc(),
    )


def set_reimport_background_failure(job, connection, type, value, _):
    reimport_id = job.args[0]
    try:
        reimport = ProjectReimport.objects.get(id=reimport_id)
    except ProjectReimport.DoesNotExist:
        return
    mark_import_job_finished(
        reimport,
        ProjectReimport.Status.FAILED,
        error=str(value),
        traceback_text=traceback.format_exc(),
    )


def reformat_predictions(tasks, preannotated_from_fields, project=None, raise_errors=False):
    """
    Transform flat task JSON objects into proper format with separate data and predictions fields.
    Also validates the predictions to ensure they are properly formatted using LabelInterface.

    Args:
        tasks: List of task data
        preannotated_from_fields: List of field names to convert to predictions
        project: Optional project instance to determine correct to_name and type from label config
    """
    new_tasks = []
    validation_errors = []

    # If project is provided, create LabelInterface to determine correct mappings
    li = None
    if project:
        try:
            li = LabelInterface(project.label_config)
        except Exception as e:
            logger.warning(f'Could not create LabelInterface for project {project.id}: {e}')

    for task_index, task in enumerate(tasks):
        if 'data' in task:
            task_data = task['data']
        else:
            task_data = task

        predictions = []
        for field in preannotated_from_fields:
            if field not in task_data:
                validation_errors.append(f"Task {task_index}: Preannotated field '{field}' not found in task data")
                continue

            value = task_data[field]
            if value is not None:
                # Try to determine correct to_name and type from project configuration
                to_name = 'text'  # Default fallback
                prediction_type = 'choices'  # Default fallback

                if li:
                    # Find a control tag that matches the field name
                    try:
                        control_tag = li.get_control(field)
                        # Use the control's to_name and determine type
                        if hasattr(control_tag, 'to_name') and control_tag.to_name:
                            to_name = (
                                control_tag.to_name[0]
                                if isinstance(control_tag.to_name, list)
                                else control_tag.to_name
                            )
                            prediction_type = control_tag.tag.lower()
                    except Exception:
                        # Control not found, use defaults
                        pass

                # Create prediction from preannotated field
                # Handle different types of values
                if isinstance(value, dict):
                    # For complex structures like bounding boxes, use the value directly
                    prediction_value = value
                else:
                    # For simple values, use the prediction_type as the key
                    # Handle cases where the type doesn't match the expected key
                    value_key = prediction_type
                    if prediction_type == 'textarea':
                        value_key = 'text'

                    # Most types expect lists, but some expect single values
                    if prediction_type in ['rating', 'number', 'datetime']:
                        prediction_value = {value_key: value}
                    else:
                        # Wrap in list for most types
                        prediction_value = {value_key: [value] if not isinstance(value, list) else value}

                prediction = {
                    'result': [
                        {
                            'from_name': field,
                            'to_name': to_name,
                            'type': prediction_type,
                            'value': prediction_value,
                        }
                    ],
                    'score': 1.0,
                    'model_version': 'preannotated',
                }

                predictions.append(prediction)

        # Create new task structure
        new_task = {'data': task_data, 'predictions': predictions}
        new_tasks.append(new_task)

    # If there are validation errors, raise them
    if validation_errors and raise_errors:
        raise ValidationError({'preannotated_fields': validation_errors})

    return new_tasks


post_process_reimport = load_func(settings.POST_PROCESS_REIMPORT)


def _counters_set_on_create(project, annotation_count, prediction_count):
    """Task counters are set on bulk create; they only need recalculation when annotations or predictions were imported"""
    return (
        annotation_count == 0
        and prediction_count == 0
        and flag_set(FF_IMPORT_BULK_TASK_STATES, organization=project.organization)
    )


def _async_reimport_background_streaming(reimport, project, organization_id, user):
    """Streaming version of reimport that processes tasks in batches to reduce memory usage"""
    start = time.time()
    try:
        # Get batch size from settings or use default
        batch_size = settings.REIMPORT_BATCH_SIZE

        # Initialize counters
        total_task_count = 0
        total_annotation_count = 0
        total_prediction_count = 0
        all_found_formats = {}
        all_data_columns = set()
        all_created_task_ids = []

        # Remove old tasks once before starting
        with transaction.atomic():
            project.remove_tasks_by_file_uploads(reimport.file_upload_ids)

        # Process tasks in batches
        batch_number = 0
        for batch_tasks, batch_formats, batch_columns in FileUpload.load_tasks_from_uploaded_files_streaming(
            project, reimport.file_upload_ids, files_as_tasks_list=reimport.files_as_tasks_list, batch_size=batch_size
        ):
            if not batch_tasks:
                logger.info(f'Empty batch received for reimport {reimport.id}')
                continue

            batch_number += 1
            logger.info(f'Processing batch {batch_number} with {len(batch_tasks)} tasks for reimport {reimport.id}')

            # Process batch in transaction
            with transaction.atomic():
                # Lock summary for update to avoid race conditions
                summary = ProjectSummary.objects.select_for_update().get(project=project)

                # Serialize and save batch
                serializer = ImportApiSerializer(
                    data=batch_tasks,
                    many=True,
                    context={'project': project, 'user': user, 'defer_post_process_custom_callback': True},
                )
                serializer.is_valid(raise_exception=True)
                batch_db_tasks = serializer.save(project_id=project.id)

                # Collect task IDs for later use
                all_created_task_ids.extend([t.id for t in batch_db_tasks])

                # Update batch counters
                batch_task_count = len(batch_db_tasks)
                batch_annotation_count = len(serializer.db_annotations)
                batch_prediction_count = len(serializer.db_predictions)

                total_task_count += batch_task_count
                total_annotation_count += batch_annotation_count
                total_prediction_count += batch_prediction_count

                # Update formats and columns
                all_found_formats.update(batch_formats)
                if batch_columns:
                    if not all_data_columns:
                        all_data_columns = batch_columns
                    else:
                        all_data_columns &= batch_columns

                # Update data columns in summary
                summary.update_data_columns(batch_db_tasks)

            update_import_job_progress(
                reimport,
                task_count=total_task_count,
                annotation_count=total_annotation_count,
                prediction_count=total_prediction_count,
            )

            logger.info(
                f'Batch {batch_number} processed successfully: {batch_task_count} tasks, '
                f'{batch_annotation_count} annotations, {batch_prediction_count} predictions'
            )

        # After all batches are processed, emit webhooks and update task states once
        if all_created_task_ids:
            logger.info(
                f'Finalizing reimport: emitting webhooks and updating task states for {len(all_created_task_ids)} tasks'
            )

            # Emit webhooks for all tasks at once (passing list of IDs)
            emit_webhooks_for_instance(organization_id, project, WebhookAction.TASKS_CREATED, all_created_task_ids)

            # Update task states for all tasks at once
            all_tasks_queryset = Task.objects.filter(id__in=all_created_task_ids)
            recalculate_stats_counts = {
                'task_count': total_task_count,
                'annotation_count': total_annotation_count,
                'prediction_count': total_prediction_count,
            }

            project.update_tasks_counters_and_task_states(
                tasks_queryset=all_tasks_queryset,
                maximum_annotations_changed=False,
                overlap_cohort_percentage_changed=False,
                tasks_number_changed=True,
                recalculate_stats_counts=recalculate_stats_counts,
                update_counters=not _counters_set_on_create(project, total_annotation_count, total_prediction_count),
            )
            logger.info('Tasks bulk_update finished (async streaming reimport)')

            TaskSerializerBulk.post_process_custom_callback(project.id, user)

        # Update reimport with final statistics in the same terminal UPDATE.
        mark_import_job_finished(
            reimport,
            ProjectReimport.Status.COMPLETED,
            duration=time.time() - start,
            task_count=total_task_count,
            annotation_count=total_annotation_count,
            prediction_count=total_prediction_count,
            found_formats=all_found_formats,
            data_columns=list(all_data_columns),
        )

        logger.info(f'Streaming reimport {reimport.id} completed: {total_task_count} tasks imported')

        # Run post-processing
        post_process_reimport(reimport)

    except Exception as e:
        logger.error(f'Error in streaming reimport {reimport.id}: {str(e)}', exc_info=True)
        mark_import_job_finished(
            reimport,
            ProjectReimport.Status.FAILED,
            error=str(e),
            traceback_text=traceback.format_exc(),
        )
        raise


def _async_import_background_streaming(project_import, user):
    try:
        batch_size = settings.IMPORT_BATCH_SIZE

        total_task_count = 0
        total_annotation_count = 0
        total_prediction_count = 0
        all_created_task_ids = []

        project = project_import.project
        start = time.time()

        batch_number = 0
        streaming_generator = load_tasks_for_async_import_streaming(project_import, user, batch_size)

        final_file_upload_ids = []
        final_found_formats = {}
        final_data_columns = set()

        for batch_tasks, file_upload_ids, found_formats, data_columns in streaming_generator:
            if not batch_tasks:
                logger.info(f'Empty batch received for import {project_import.id}')
                continue

            batch_number += 1
            logger.info(
                f'Processing batch {batch_number} with {len(batch_tasks)} tasks for import {project_import.id}'
            )

            if file_upload_ids and file_upload_ids not in final_file_upload_ids:
                final_file_upload_ids = file_upload_ids
            final_found_formats.update(found_formats)
            final_data_columns.update(data_columns)

            if project_import.preannotated_from_fields:
                raise_errors = flag_set(
                    'fflag_feat_utc_210_prediction_validation_15082025', user=project.organization.created_by
                )
                logger.info(f'Reformatting predictions with raise_errors: {raise_errors}')
                batch_tasks = reformat_predictions(
                    batch_tasks, project_import.preannotated_from_fields, project, raise_errors
                )

            if project.label_config_is_not_default and flag_set(
                'fflag_feat_utc_210_prediction_validation_15082025', user=project.organization.created_by
            ):
                validation_errors = []
                li = LabelInterface(project.label_config)

                for i, task in enumerate(batch_tasks):
                    if 'predictions' in task:
                        for j, prediction in enumerate(task['predictions']):
                            try:
                                prediction = sanitize_prediction_import_payload(prediction)
                                validation_errors_list = li.validate_prediction(prediction, return_errors=True)
                                if validation_errors_list:
                                    for error in validation_errors_list:
                                        validation_errors.append(
                                            f'Task {total_task_count + i}, prediction {j}: {error}'
                                        )
                            except Exception as e:
                                error_msg = f'Task {total_task_count + i}, prediction {j}: Error validating prediction - {str(e)}'
                                validation_errors.append(error_msg)
                                logger.error(f'Exception during validation: {error_msg}')

                if validation_errors:
                    error_message = f'Prediction validation failed ({len(validation_errors)} errors):\n'
                    for error in validation_errors:
                        error_message += f'- {error}\n'

                    if flag_set(
                        'fflag_feat_utc_210_prediction_validation_15082025', user=project.organization.created_by
                    ):
                        mark_import_job_finished(project_import, ProjectImport.Status.FAILED, error=error_message)
                        return
                    else:
                        logger.error(
                            f'Prediction validation failed, not raising error - ({len(validation_errors)} errors):\n{error_message}'
                        )

            if project_import.commit_to_project:
                with transaction.atomic():
                    summary = ProjectSummary.objects.select_for_update().get(project=project)

                    # BROS-1092: see async_import_background above for rationale.
                    ctx = {'project': project, 'defer_post_process_custom_callback': True}
                    if flag_set(FF_BROS_1092_IMPORT_UNKNOWN_COMPLETED_BY, user=user):
                        ctx['user'] = user
                    serializer = ImportApiSerializer(data=batch_tasks, many=True, context=ctx)
                    serializer.is_valid(raise_exception=True)
                    batch_db_tasks = serializer.save(project_id=project.id)

                    all_created_task_ids.extend([t.id for t in batch_db_tasks])

                    batch_task_count = len(batch_db_tasks)
                    batch_annotation_count = len(serializer.db_annotations)
                    batch_prediction_count = len(serializer.db_predictions)

                    total_task_count += batch_task_count
                    total_annotation_count += batch_annotation_count
                    total_prediction_count += batch_prediction_count

                    summary.update_data_columns(batch_db_tasks)

            else:
                total_task_count += len(batch_tasks)

            update_import_job_progress(
                project_import,
                task_count=total_task_count,
                annotation_count=total_annotation_count,
                prediction_count=total_prediction_count,
            )

            logger.info(f'Batch {batch_number} processed successfully: {len(batch_tasks)} tasks')

        final_data_columns = list(final_data_columns)

        if project_import.commit_to_project and all_created_task_ids:
            logger.info(
                f'Finalizing import: emitting webhooks and updating task states for {len(all_created_task_ids)} tasks'
            )

            emit_webhooks_for_instance(
                user.active_organization, project, WebhookAction.TASKS_CREATED, all_created_task_ids
            )

            recalculate_stats_counts = {
                'task_count': total_task_count,
                'annotation_count': total_annotation_count,
                'prediction_count': total_prediction_count,
            }

            all_tasks_queryset = Task.objects.filter(id__in=all_created_task_ids)
            project.update_tasks_counters_and_task_states(
                tasks_queryset=all_tasks_queryset,
                maximum_annotations_changed=False,
                overlap_cohort_percentage_changed=False,
                tasks_number_changed=True,
                recalculate_stats_counts=recalculate_stats_counts,
                update_counters=not _counters_set_on_create(project, total_annotation_count, total_prediction_count),
            )
            logger.info('Tasks bulk_update finished (async streaming import)')

            TaskSerializerBulk.post_process_custom_callback(project.id, user)

        duration = time.time() - start

        finish_kwargs = {
            'task_count': total_task_count or 0,
            'annotation_count': total_annotation_count or 0,
            'prediction_count': total_prediction_count or 0,
            'file_upload_ids': final_file_upload_ids,
            'found_formats': final_found_formats,
            'data_columns': final_data_columns,
        }
        if project_import.return_task_ids:
            finish_kwargs['task_ids'] = all_created_task_ids

        mark_import_job_finished(project_import, ProjectImport.Status.COMPLETED, duration=duration, **finish_kwargs)

        logger.info(f'Streaming import {project_import.id} completed: {total_task_count} tasks imported')

    except Exception as e:
        logger.error(f'Error in streaming import {project_import.id}: {str(e)}', exc_info=True)
        mark_import_job_finished(
            project_import,
            ProjectImport.Status.FAILED,
            error=str(e),
            traceback_text=traceback.format_exc(),
        )
        raise


def async_reimport_background(reimport_id, organization_id, user, **kwargs):
    with transaction.atomic():
        try:
            reimport = ProjectReimport.objects.get(id=reimport_id)
        except ProjectReimport.DoesNotExist:
            logger.error(f'ProjectReimport with id {reimport_id} not found, import processing failed')
            return
        if not claim_import_job_in_progress(reimport):
            logger.error(f'Processing reimport with id {reimport_id} already started')
            return

    project = reimport.project
    start = time.time()

    # Check feature flag for memory improvement
    if flag_set('fflag_fix_back_plt_838_reimport_memory_improvement_05082025_short', user='auto'):
        logger.info(f'Using streaming reimport for project {project.id}')
        _async_reimport_background_streaming(reimport, project, organization_id, user)
    else:
        # Original implementation
        tasks, found_formats, data_columns = FileUpload.load_tasks_from_uploaded_files(
            reimport.project, reimport.file_upload_ids, files_as_tasks_list=reimport.files_as_tasks_list
        )

        with transaction.atomic():
            # Lock summary for update to avoid race conditions
            summary = ProjectSummary.objects.select_for_update().get(project=project)

            project.remove_tasks_by_file_uploads(reimport.file_upload_ids)
            serializer = ImportApiSerializer(data=tasks, many=True, context={'project': project, 'user': user})
            serializer.is_valid(raise_exception=True)
            tasks = serializer.save(project_id=project.id)
            emit_webhooks_for_instance(organization_id, project, WebhookAction.TASKS_CREATED, tasks)

            task_count = len(tasks)
            annotation_count = len(serializer.db_annotations)
            prediction_count = len(serializer.db_predictions)

            recalculate_stats_counts = {
                'task_count': task_count,
                'annotation_count': annotation_count,
                'prediction_count': prediction_count,
            }

            # Update counters (like total_annotations) for new tasks and after bulk update tasks stats. It should be a
            # single operation as counters affect bulk is_labeled update
            project.update_tasks_counters_and_task_states(
                tasks_queryset=tasks,
                maximum_annotations_changed=False,
                overlap_cohort_percentage_changed=False,
                tasks_number_changed=True,
                recalculate_stats_counts=recalculate_stats_counts,
            )
            logger.info('Tasks bulk_update finished (async reimport)')

            summary.update_data_columns(tasks)
            # TODO: summary.update_created_annotations_and_labels

        mark_import_job_finished(
            reimport,
            ProjectReimport.Status.COMPLETED,
            duration=time.time() - start,
            task_count=task_count,
            annotation_count=annotation_count,
            prediction_count=prediction_count,
            found_formats=found_formats,
            data_columns=list(data_columns),
        )

        post_process_reimport(reimport)


def get_document_upload_file_results(project_import):
    file_upload_ids = project_import.file_upload_ids or []
    if not file_upload_ids:
        return []

    file_uploads = FileUpload.objects.filter(
        id__in=file_upload_ids, project_id=project_import.project_id
    ).select_related('project', 'conversion_artifact')
    file_uploads_by_id = {file_upload.id: file_upload for file_upload in file_uploads}
    ordered_results = []

    for file_upload_id in file_upload_ids:
        file_upload = file_uploads_by_id.get(file_upload_id)
        if not file_upload:
            continue

        terminal = file_upload.preprocessing_status in {
            FileUpload.PreprocessingStatus.COMPLETED,
            FileUpload.PreprocessingStatus.FAILED,
        }
        job_health = (
            None
            if terminal
            else get_job_health(file_upload.preprocessing_job_id, queue_name=DOCUMENT_UPLOAD_QUEUE_NAME)
        )

        if file_upload.preprocessing_status == FileUpload.PreprocessingStatus.COMPLETED:
            status = 'success'
        elif file_upload.preprocessing_status == FileUpload.PreprocessingStatus.FAILED:
            status = 'error'
        elif file_upload.preprocessing_status == FileUpload.PreprocessingStatus.PROCESSING:
            status = 'processing' if job_health == 'running' else 'queued'
        else:
            status = 'processing' if job_health == 'running' else 'queued'

        result = {
            'file_upload_id': file_upload.id,
            'filename': file_upload.original_filename,
            'status': status,
            'tasks_created': len(file_upload.created_task_ids or []),
            'task_ids': file_upload.created_task_ids or [],
        }
        if job_health:
            result['job_health'] = job_health

        artifact = getattr(file_upload, 'conversion_artifact', None)
        if artifact:
            result['artifact_id'] = artifact.id
        if file_upload.preprocessing_error:
            result['error'] = file_upload.preprocessing_error

        ordered_results.append(result)

    return ordered_results


def summarize_document_upload_results(project_import):
    results = get_document_upload_file_results(project_import)
    success_count = sum(1 for result in results if result['status'] == 'success')
    error_count = sum(1 for result in results if result['status'] == 'error')
    processed_files = success_count + error_count
    total_files = len(results)

    job_health = None
    stalled_reason = None
    unfinished_results = [result for result in results if result['status'] not in {'success', 'error'}]
    has_running_jobs = any(result.get('job_health') == 'running' for result in unfinished_results)
    has_queued_jobs = any(result.get('job_health') == 'queued' for result in unfinished_results)
    has_failed_jobs = any(
        result['status'] not in {'success', 'error'} and result.get('job_health') == 'failed' for result in results
    )
    has_stalled_jobs = any(
        result['status'] not in {'success', 'error'} and result.get('job_health') in {'stalled', 'finished'}
        for result in results
    )
    has_missing_jobs = any(
        result['status'] not in {'success', 'error'} and result.get('job_health') is None for result in results
    )

    if has_running_jobs:
        job_health = 'running'
    elif has_queued_jobs:
        job_health = 'queued'
    elif has_failed_jobs:
        job_health = 'failed'
        stalled_reason = 'One or more background file jobs failed before the file state could be reconciled.'
    elif has_stalled_jobs:
        job_health = 'stalled'
        stalled_reason = 'One or more background file jobs are queued but no active workers are available.'
    elif has_missing_jobs and project_import.status == ProjectImport.Status.IN_PROGRESS:
        job_health = 'stalled'
        stalled_reason = 'One or more background file jobs are missing from the queue or worker registries.'
    elif project_import.job_id:
        job_health = get_job_health(project_import.job_id, queue_name=DOCUMENT_UPLOAD_QUEUE_NAME)
        if project_import.status == ProjectImport.Status.IN_PROGRESS and job_health is None:
            job_health = 'stalled'
            stalled_reason = 'The background finalization job is missing from the queue or worker registries.'

    return {
        'results': results,
        'total_files': total_files,
        'processed_files': processed_files,
        'success_count': success_count,
        'error_count': error_count,
        'all_succeeded': total_files > 0 and success_count == total_files,
        'has_errors': error_count > 0,
        'job_health': job_health,
        'stalled_reason': stalled_reason,
    }


def _set_file_upload_failed(file_upload_id, error_message, expected_job_id):
    FileUpload.objects.filter(
        id=file_upload_id,
        preprocessing_job_id=expected_job_id,
        preprocessing_status=FileUpload.PreprocessingStatus.PROCESSING,
    ).update(
        preprocessing_status=FileUpload.PreprocessingStatus.FAILED,
        preprocessing_error=error_message,
        created_task_ids=[],
    )


def _claim_document_upload_import(import_id):
    with transaction.atomic():
        project_import = ProjectImport.objects.select_for_update().get(id=import_id)
        if project_import.import_type != ProjectImport.Type.DOCUMENT_UPLOAD:
            raise ValidationError('Project import is not a document upload batch.')
        if project_import.status == ProjectImport.Status.COMPLETED:
            return None
        if project_import.status == ProjectImport.Status.CREATED:
            project_import.status = ProjectImport.Status.IN_PROGRESS
            project_import.save(update_fields=['status'])
        return project_import


def _claim_file_upload(project_import, file_upload_id, expected_job_id=None):
    with transaction.atomic():
        if file_upload_id not in project_import.file_upload_ids:
            raise ValidationError('File upload does not belong to this document upload batch.')
        file_upload = FileUpload.objects.select_for_update().get(
            id=file_upload_id, project_id=project_import.project_id
        )
        if expected_job_id is not None and file_upload.preprocessing_job_id != expected_job_id:
            return None
        if file_upload.preprocessing_status != FileUpload.PreprocessingStatus.PENDING:
            return None
        file_upload.preprocessing_status = FileUpload.PreprocessingStatus.PROCESSING
        file_upload.preprocessing_error = ''
        file_upload.save(update_fields=['preprocessing_status', 'preprocessing_error'])
        return file_upload


def _process_document_upload_file(project_import, file_upload_id, user_id, expected_job_id=None):
    file_upload = _claim_file_upload(project_import, file_upload_id, expected_job_id)
    if not file_upload:
        return

    expected_job_id = file_upload.preprocessing_job_id
    artifact = None
    try:
        validate_document_extension(file_upload.original_filename)
        validate_document_file_size(file_upload.file)
        preprocessed_result = preprocess_uploaded_document(
            project_id=project_import.project_id,
            file_upload=file_upload,
            user_id=user_id,
            metadata=file_upload.preprocessing_metadata,
        )

        with transaction.atomic():
            file_upload = FileUpload.objects.select_for_update().get(id=file_upload_id)
            if (
                file_upload.preprocessing_status != FileUpload.PreprocessingStatus.PROCESSING
                or file_upload.preprocessing_job_id != expected_job_id
            ):
                return

            artifact = persist_document_conversion_artifact(
                file_upload=file_upload,
                artifact_draft=preprocessed_result.artifact_draft,
            )
            tasks_payload = []
            for task_payload in preprocessed_result.tasks_payload:
                task_payload = dict(task_payload)
                task_meta = dict(task_payload.get('meta') or {})
                task_meta['artifact_id'] = artifact.id
                task_payload['meta'] = task_meta
                tasks_payload.append(task_payload)

            serializer = ImportApiSerializer(
                data=tasks_payload, many=True, context={'project': project_import.project}
            )
            serializer.is_valid(raise_exception=True)
            saved_tasks = serializer.save(project_id=project_import.project_id)
            task_ids = [task.id for task in saved_tasks]

            file_upload.preprocessing_status = FileUpload.PreprocessingStatus.COMPLETED
            file_upload.preprocessing_error = ''
            file_upload.preprocessor_name = preprocessed_result.preprocessor_name
            file_upload.created_task_ids = task_ids
            file_upload.save(
                update_fields=[
                    'preprocessing_status',
                    'preprocessing_error',
                    'preprocessor_name',
                    'created_task_ids',
                ]
            )

        if saved_tasks:
            try:
                emit_webhooks_for_instance(
                    file_upload.user.active_organization,
                    project_import.project,
                    WebhookAction.TASKS_CREATED,
                    saved_tasks,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    'Webhook emission failed after document upload task creation for project=%s file_upload=%s',
                    project_import.project_id,
                    file_upload_id,
                )
    except (ValidationError, DocumentPreprocessingError) as exc:
        cleanup_persisted_document_artifact_files(artifact)
        error_message = extract_message(exc)
        logger.warning(
            'Document upload preprocessing failed for project=%s filename=%s user=%s: %s',
            project_import.project_id,
            file_upload.original_filename,
            user_id,
            error_message,
        )
        _set_file_upload_failed(file_upload_id, error_message, expected_job_id)
    except Exception as exc:  # noqa: BLE001
        cleanup_persisted_document_artifact_files(artifact)
        error_message = extract_message(exc)
        logger.exception(
            'Unexpected document upload preprocessing failure for project=%s filename=%s user=%s',
            project_import.project_id,
            file_upload.original_filename,
            user_id,
        )
        _set_file_upload_failed(file_upload_id, error_message, expected_job_id)


def _finalize_document_upload_import_locked(project_import):
    successful_uploads = FileUpload.objects.filter(
        id__in=project_import.file_upload_ids,
        project_id=project_import.project_id,
        preprocessing_status=FileUpload.PreprocessingStatus.COMPLETED,
    )
    all_task_ids = []
    for file_upload in successful_uploads:
        all_task_ids.extend(file_upload.created_task_ids or [])
    all_task_ids = list(dict.fromkeys(all_task_ids))

    if all_task_ids:
        project_import.project._update_tasks_counters_and_task_states(
            all_task_ids,
            maximum_annotations_changed=False,
            overlap_cohort_percentage_changed=False,
            tasks_number_changed=True,
            from_scratch=True,
            recalculate_stats_counts=None,
        )
        summary = ProjectSummary.objects.select_for_update().get(project=project_import.project)
        recalculate_created_annotations_and_labels_from_scratch(
            project_import.project,
            summary,
            project_import.project.organization_id,
        )

    project_import.task_count = len(all_task_ids)
    project_import.annotation_count = Annotation.objects.filter(task_id__in=all_task_ids).count()
    project_import.prediction_count = Prediction.objects.filter(task_id__in=all_task_ids).count()
    project_import.task_ids = all_task_ids
    project_import.duration = max(
        0,
        int((timezone.now() - (project_import.created_at or timezone.now())).total_seconds()),
    )
    project_import.status = ProjectImport.Status.COMPLETED
    project_import.error = None
    project_import.traceback = None
    project_import.finished_at = timezone.now()
    project_import.job_id = ''
    project_import.save(
        update_fields=[
            'task_count',
            'annotation_count',
            'prediction_count',
            'task_ids',
            'duration',
            'status',
            'error',
            'traceback',
            'finished_at',
            'job_id',
        ]
    )


def maybe_finalize_document_upload_import(import_id):
    try:
        with transaction.atomic():
            project_import = ProjectImport.objects.select_for_update().get(id=import_id)
            if project_import.import_type != ProjectImport.Type.DOCUMENT_UPLOAD:
                raise ValidationError('Project import is not a document upload batch.')
            if project_import.status == ProjectImport.Status.COMPLETED:
                return
            if project_import.status == ProjectImport.Status.CREATED:
                project_import.status = ProjectImport.Status.IN_PROGRESS
                project_import.save(update_fields=['status'])

            if FileUpload.objects.filter(
                id__in=project_import.file_upload_ids,
                project_id=project_import.project_id,
                preprocessing_status__in=[
                    FileUpload.PreprocessingStatus.PENDING,
                    FileUpload.PreprocessingStatus.PROCESSING,
                ],
            ).exists():
                return

            _finalize_document_upload_import_locked(project_import)
    except Exception as exc:
        logger.exception('Document upload finalization failed for import %s', import_id)
        # Record the failure after the finalization transaction has rolled back.
        ProjectImport.objects.filter(id=import_id, import_type=ProjectImport.Type.DOCUMENT_UPLOAD).exclude(
            status=ProjectImport.Status.COMPLETED,
        ).update(
            status=ProjectImport.Status.FAILED,
            error=extract_message(exc),
            traceback=traceback.format_exc(),
            finished_at=timezone.now(),
        )
        raise


def finalize_document_upload_import(import_id):
    maybe_finalize_document_upload_import(import_id)


def async_document_upload_file(import_id, file_upload_id, user_id, expected_job_id=None, **kwargs):
    try:
        project_import = _claim_document_upload_import(import_id)
    except ProjectImport.DoesNotExist:
        logger.error('ProjectImport with id %s not found, document upload processing failed', import_id)
        return

    if not project_import:
        logger.error('Processing document upload import with id %s cannot proceed', import_id)
        return

    _process_document_upload_file(project_import, file_upload_id, user_id, expected_job_id)
    maybe_finalize_document_upload_import(import_id)


def async_document_upload_background(import_id, user_id, **kwargs):
    project_import = ProjectImport.objects.get(id=import_id)
    for file_upload_id in project_import.file_upload_ids:
        file_upload = FileUpload.objects.get(id=file_upload_id)
        if file_upload.preprocessing_status == FileUpload.PreprocessingStatus.PENDING:
            async_document_upload_file(import_id, file_upload_id, user_id, **kwargs)
    maybe_finalize_document_upload_import(import_id)


def set_document_upload_file_background_failure(job, connection, type, value, _):
    import_id = job.args[0]
    file_upload_id = job.args[1]
    error_message = str(value)
    FileUpload.objects.filter(
        id=file_upload_id,
        preprocessing_job_id=job.id,
        preprocessing_status__in=[
            FileUpload.PreprocessingStatus.PENDING,
            FileUpload.PreprocessingStatus.PROCESSING,
        ],
    ).update(
        preprocessing_status=FileUpload.PreprocessingStatus.FAILED,
        preprocessing_error=error_message,
        created_task_ids=[],
    )
    try:
        maybe_finalize_document_upload_import(import_id)
    except Exception:
        logger.exception('Document upload finalization failed after file job failure for import %s', import_id)


def set_document_upload_background_failure(job, connection, type, value, _):
    import_id = job.args[0]
    ProjectImport.objects.filter(id=import_id, job_id=job.id).exclude(
        status=ProjectImport.Status.COMPLETED,
    ).update(
        status=ProjectImport.Status.FAILED,
        traceback=traceback.format_exc(),
        error=str(value),
        finished_at=timezone.now(),
    )
