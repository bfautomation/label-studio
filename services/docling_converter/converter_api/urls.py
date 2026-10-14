from django.urls import path

from .views import ConvertDocumentView

urlpatterns = [
    path('convert', ConvertDocumentView.as_view(), name='convert-document'),
]
