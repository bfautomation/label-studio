"""Harness only: record every name the app's Python processes look up, so smoke.sh can spot outbound traffic.

Python loads this file automatically because the harness puts its folder on PYTHONPATH; the image is unchanged.
"""

import sys

LOG = '/tmp/outbound-lookups.log'


def _record_lookup(event, args):
    if event == 'socket.getaddrinfo':
        with open(LOG, 'a') as log:
            log.write(f'{args[0]}\n')


sys.addaudithook(_record_lookup)
