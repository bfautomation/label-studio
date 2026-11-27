from contextlib import contextmanager
from typing import Iterator

from core.feature_flags import flag_set
from core.utils.db import SQCount
from django.db.models import Count, OuterRef, Q
from tasks.models import Annotation, Prediction, Task


def annotate_task_number(queryset):
    tasks = Task.objects.filter(project=OuterRef('id')).values_list('id')
    return queryset.annotate(task_number=SQCount(tasks))


def annotate_finished_task_number(queryset):
    if flag_set('fflag_fix_back_plt_811_finished_task_number_01072025_short', user='auto'):
        return queryset.annotate(finished_task_number=Count('tasks', filter=Q(tasks__is_labeled=True)))
    else:
        tasks = Task.objects.filter(project=OuterRef('id'), is_labeled=True).values_list('id')
        return queryset.annotate(finished_task_number=SQCount(tasks))


def annotate_total_predictions_number(queryset):
    predictions = Prediction.objects.filter(project=OuterRef('id')).values('id')
    return queryset.annotate(total_predictions_number=SQCount(predictions))


def annotate_total_annotations_number(queryset):
    subquery = Annotation.objects.filter(Q(project=OuterRef('pk')) & Q(was_cancelled=False)).values('id')
    return queryset.annotate(total_annotations_number=SQCount(subquery))


def annotate_num_tasks_with_annotations(queryset):
    # @todo: check do we really need this counter?
    # this function is very slow because of tasks__id and distinct
    subquery = (
        Annotation.objects.filter(
            Q(project=OuterRef('pk')) & Q(ground_truth=False) & Q(was_cancelled=False) & Q(result__isnull=False)
        )
        .values('task__id')
        .distinct()
    )
    return queryset.annotate(num_tasks_with_annotations=SQCount(subquery))


def annotate_useful_annotation_number(queryset):
    subquery = Annotation.objects.filter(
        Q(project=OuterRef('pk')) & Q(was_cancelled=False) & Q(ground_truth=False) & Q(result__isnull=False)
    ).values('id')
    return queryset.annotate(useful_annotation_number=SQCount(subquery))


def annotate_ground_truth_number(queryset):
    subquery = Annotation.objects.filter(Q(project=OuterRef('pk')) & Q(ground_truth=True)).values('id')
    return queryset.annotate(ground_truth_number=SQCount(subquery))


def annotate_skipped_annotations_number(queryset):
    subquery = Annotation.objects.filter(Q(project=OuterRef('pk')) & Q(was_cancelled=True)).values('id')
    return queryset.annotate(skipped_annotations_number=SQCount(subquery))


@contextmanager
def signals_disconnected_except_file_cleanup() -> Iterator[None]:
    """Disconnect every model signal, as bulk deletions do, except the receivers that delete a record's stored files.

    Django then schedules the files of exactly the records it deletes, so a record added while the deletion runs is
    never deleted without its file. Use it inside the transaction that deletes: the files go once that commits.
    """
    from core.utils.common import temporary_disconnect_all_signals
    from data_export.models import (
        ConvertedFormat,
        Export,
        cleanup_converted_format_file,
        cleanup_export_file,
    )
    from data_import.models import (
        DocumentConversionArtifact,
        FileUpload,
        cleanup_document_conversion_artifact_files,
        cleanup_file_upload_file,
    )
    from django.db.models.signals import post_delete

    with temporary_disconnect_all_signals():
        for receiver, sender in (
            (cleanup_file_upload_file, FileUpload),
            (cleanup_document_conversion_artifact_files, DocumentConversionArtifact),
            (cleanup_export_file, Export),
            (cleanup_converted_format_file, ConvertedFormat),
        ):
            post_delete.connect(receiver, sender=sender)
        yield
