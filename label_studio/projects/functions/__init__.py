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


def delete_projects_stored_files_on_commit(projects):
    """Schedule deletion of the files kept in storage for these projects, once their deletion is committed.

    Call it before deleting projects with signals disconnected, which skips the per-record file cleanup receivers.
    """
    from core.utils.db import delete_stored_file_on_commit
    from data_export.models import ConvertedFormat, Export
    from data_import.models import DocumentConversionArtifact, FileUpload

    stored_files = []
    for model, project_field in (
        (FileUpload, 'project'),
        (Export, 'project'),
        (ConvertedFormat, 'project'),
        (DocumentConversionArtifact, 'file_upload__project'),
    ):
        file_fields = [field.name for field in model._meta.fields if field.get_internal_type() == 'FileField']
        for record in model.objects.filter(**{f'{project_field}__in': projects}).only(*file_fields):
            stored_files += [getattr(record, name) for name in file_fields]
    for stored_file in stored_files:
        delete_stored_file_on_commit(stored_file)
