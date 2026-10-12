#!/bin/sh
set -eu

cd /app
/bin/sh ./waitingsecrets.sh
exec /bin/sh ./start.sh
