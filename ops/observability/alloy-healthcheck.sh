#!/usr/bin/bash
set -euo pipefail

# Query the running collector; config validation remains a separate CI gate.
exec 3<>/dev/tcp/127.0.0.1/12345
printf 'GET /-/ready HTTP/1.0\r\nHost: localhost\r\nConnection: close\r\n\r\n' >&3
IFS= read -r status <&3
case "$status" in
  HTTP/1.[01]' 200 '*) exit 0 ;;
  *) exit 1 ;;
esac
