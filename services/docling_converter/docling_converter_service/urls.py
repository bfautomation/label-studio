from django.http import JsonResponse
from django.urls import include, path


def healthcheck(_request):
    return JsonResponse({'status': 'ok'})


urlpatterns = [
    path('healthz', healthcheck),
    path('api/', include('converter_api.urls')),
]
