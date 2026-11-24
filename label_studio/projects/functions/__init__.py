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

    Call it inside the transaction that deletes the projects with signals disconnected, which skips the per-record
    file cleanup receivers. Outside a transaction the files would be deleted at once, even if the deletion then fails.
    """
    from core.utils.db import delete_stored_file_on_commit
    from core.utils.iterators import iterate_queryset
    from data_export.models import ConvertedFormat, Export
    from data_import.models import DocumentConversionArtifact, FileUpload

    for model, belongs_to_projects in (
        (FileUpload, Q(project__in=projects)),
        (Export, Q(project__in=projects)),
        # the conversion endpoint links its files to the export only, without a project
        (ConvertedFormat, Q(project__in=projects) | Q(export__project__in=projects)),
        (DocumentConversionArtifact, Q(file_upload__project__in=projects)),
    ):
        file_fields = [field for field in model._meta.fields if field.get_internal_type() == 'FileField']
        names = model.objects.filter(belongs_to_projects).values_list(*[field.name for field in file_fields])
        for row in iterate_queryset(names):
            for field, name in zip(file_fields, row):
                delete_stored_file_on_commit(field.storage, name)
