"""Create the harness's Blob containers in Azurite, retrying until it accepts connections."""

import os
import sys
import time

from azure.core.exceptions import ResourceExistsError, ServiceRequestError
from azure.storage.blob import BlobServiceClient

CONTAINERS = ('source', 'exports')

account_name = os.environ['AZURE_BLOB_ACCOUNT_NAME']
client = BlobServiceClient(
    account_url=f'https://{account_name}.blob.core.windows.net',
    credential={'account_name': account_name, 'account_key': os.environ['AZURE_BLOB_ACCOUNT_KEY']},
    # this loop does the waiting; the SDK's own backoff would hide a TLS failure for minutes
    retry_total=0,
)

for attempt in range(30):
    try:
        for name in CONTAINERS:
            try:
                client.create_container(name)
            except ResourceExistsError:
                pass
        break
    except ServiceRequestError as error:
        last_error = error
        time.sleep(1)
else:
    sys.exit(f'Azurite did not accept connections: {last_error}')

print('Containers ready:', ', '.join(c.name for c in client.list_containers()))
