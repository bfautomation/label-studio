# Docling Converter Service

Lightweight Django REST service that converts `.docx` and `.pdf` uploads into Markdown using Docling.

Endpoints:

- `GET /healthz`
- `POST /api/convert`

Optional auth:

- Set `DOCLING_CONVERTER_API_TOKEN`
- Clients should send `Authorization: Token <value>`
- Or set `DOCLING_CONVERTER_AUTH_HEADER_NAME` and `DOCLING_CONVERTER_AUTH_HEADER_VALUE` to use a custom shared header

Start the optional Compose service by setting these values in your local `.env`:

```dotenv
COMPOSE_PROFILES=docling
LABEL_STUDIO_FILE_PREPROCESSOR=data_import.document_preprocessing.external_document_service_preprocessor
LABEL_STUDIO_DOCUMENT_UPLOAD_ALLOWED_EXTENSIONS=.txt,.md,.markdown,.docx,.pdf
LABEL_STUDIO_DOCUMENT_CONVERTER_URL=http://docling-converter:8090/api/convert
# Use the same shared secret for the app, worker and converter.
LABEL_STUDIO_DOCUMENT_CONVERTER_AUTH_HEADER_NAME=Authorization
LABEL_STUDIO_DOCUMENT_CONVERTER_AUTH_HEADER_VALUE=Token replace-with-a-secret
```

Then run `docker compose up --build`. The converter is available only inside the Compose network. Enable this profile and its configuration together; the text-only default setup does not start or depend on Docling. Wait for the converter health check before submitting documents after startup. The first conversion can take longer while model weights download; adjust `LABEL_STUDIO_DOCUMENT_CONVERTER_TIMEOUT` if needed.

Run locally:

```bash
python manage.py runserver 0.0.0.0:8090
```

Operational notes:

- The converter should reuse a single in-process Docling converter instance, so model weights are initialized once per service process instead of once per request.
- The Docker image runs Gunicorn as an unprivileged user.
- Uploads are limited to 250 MiB by default; set `DOCLING_CONVERTER_MAX_FILE_SIZE` in bytes to change this.
- In Docker Compose, mount a persistent cache volume for `/home/converter/.cache` to avoid re-downloading Hugging Face artifacts after container recreation.
