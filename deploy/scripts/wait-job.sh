#!/usr/bin/env bash
# Wait for a Job in the current namespace to finish (docs/23 §3).
# Decides from the Job's conditions: `Complete` or `Failed` is set only once the Job is really
# done, so a failed first attempt with a retry still pending (backoffLimit) is not mistaken for
# the end. `kubectl wait --for=condition=complete` would sit out its whole timeout on a failure.
# Usage: wait-job.sh <job-name> <timeout-seconds>. Exit 0 = complete, 1 = failed or timed out.
set -euo pipefail
name="$1"
deadline=$(( $(date +%s) + $2 ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  conditions="$(kubectl get "job/${name}" \
    -o jsonpath='{range .status.conditions[*]}{.type}={.status}{" "}{end}')"
  case " ${conditions} " in
    *" Complete=True "*) echo "job/${name} complete"; exit 0 ;;
    *" Failed=True "*) echo "job/${name} failed"; exit 1 ;;
  esac
  sleep 5
done
echo "job/${name} timed out after $2 s"
exit 1
