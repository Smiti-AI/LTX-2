#!/usr/bin/env bash
# Verify GCP auth + project + bucket access. Run at session start.
#
# Strategy: do not prompt. If `gcloud auth application-default print-access-token`
# fails (refresh token expired), fall back to the user-account access token via
# `gcloud auth print-access-token`. That covers gcloud-CLI-based workflows
# (which is what scripts/phase0/audit_data.py uses).
#
# On a GCE VM, the metadata server provides credentials automatically — no
# action needed.
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PRE_FLIGHT="$REPO_ROOT/docs/pre_flight.yaml"

if [ ! -f "$PRE_FLIGHT" ]; then
  echo "ERROR: $PRE_FLIGHT not found." >&2
  exit 1
fi

PROJECT=$(python3 -c "import yaml,sys; print(yaml.safe_load(open('$PRE_FLIGHT'))['gcp']['project'])")
BUCKETS=$(python3 -c "import yaml; [print(b) for b in yaml.safe_load(open('$PRE_FLIGHT'))['gcp']['buckets_to_audit']]")

echo "=== GCP preflight ==="
echo "project (pre_flight): $PROJECT"

# Detect runtime: GCE VM (metadata server) vs developer workstation.
if curl -s -m 2 -H 'Metadata-Flavor: Google' \
     http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email \
     >/dev/null 2>&1; then
  RUNTIME="gce_vm"
  SA=$(curl -s -H 'Metadata-Flavor: Google' \
         http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email)
  echo "runtime: GCE VM (instance SA: $SA)"
else
  RUNTIME="workstation"
  ACTIVE_ACCT=$(gcloud config list account --format='value(core.account)' 2>/dev/null || echo "")
  ACTIVE_PROJ=$(gcloud config list project --format='value(core.project)' 2>/dev/null || echo "")
  echo "runtime: workstation (gcloud account: $ACTIVE_ACCT, project: $ACTIVE_PROJ)"
  if [ "$ACTIVE_PROJ" != "$PROJECT" ]; then
    echo "WARN: gcloud project ($ACTIVE_PROJ) differs from pre_flight ($PROJECT)." >&2
  fi
fi

# Token check — try ADC first, then user-account.
if gcloud auth application-default print-access-token >/dev/null 2>&1; then
  echo "auth: ADC OK"
elif [ "$RUNTIME" = "gce_vm" ]; then
  echo "auth: GCE metadata server (no ADC needed)"
elif gcloud auth print-access-token >/dev/null 2>&1; then
  echo "auth: user-account OK (ADC missing/expired — fine for gcloud CLI subprocess flows)"
else
  echo "ERROR: no working auth. Run 'gcloud auth login' interactively." >&2
  exit 2
fi

# Bucket reachability — list one entry per bucket. Tolerates empty buckets.
echo
echo "=== Bucket access ==="
fail=0
while IFS= read -r b; do
  if [ -z "$b" ]; then continue; fi
  if gcloud storage ls "gs://$b/" >/dev/null 2>&1; then
    echo "  OK   $b"
  else
    echo "  FAIL $b"
    fail=1
  fi
done <<< "$BUCKETS"

if [ "$fail" -ne 0 ]; then
  echo "ERROR: at least one bucket is not accessible." >&2
  exit 3
fi

echo
echo "OK: GCP preflight passed."
