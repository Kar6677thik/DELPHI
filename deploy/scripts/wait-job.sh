#!/usr/bin/env bash
# Wait for a Job in the current namespace to succeed or fail (docs/23 §3).
# `kubectl wait --for=condition=complete` would sit out its whole timeout on a failed Job.
# Usage: wait-job.sh <job-name> <timeout-seconds>. Exit 0 = succeeded, 1 = failed or timed out.
set -euo pipefail
name="$1"
deadline=$(( $(date +%s) + $2 ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  status="$(kubectl get "job/${name}" -o jsonpath='{.status.succeeded}/{.status.failed}')"
  case "$status" in
    1/*) echo "job/${name} succeeded"; exit 0 ;;
    */[1-9]*) echo "job/${name} failed"; exit 1 ;;
  esac
  sleep 5
done
echo "job/${name} timed out after $2 s"
exit 1
