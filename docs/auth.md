# GCP Authentication

## Goal

Per `active_plan.md §1`: never re-prompt for `gcloud auth login` mid-task. Auth setup happens once, deterministically, at session start via `scripts/preflight_gcp.sh`.

## Current setup (no service account created)

Per session decision (the user has all needed permissions and existing keys), we did not create a project-scoped service account for this work. We use existing credentials:

| Environment | Auth path | Status |
|---|---|---|
| Local workstation (`/Users/efrattaig/projects/sm/LTX-2`) | gcloud user account `efrat.t@smiti.ai` | Working — all 6 audit buckets listable |
| Remote VM `35.238.2.51` (efrattaig) | GCE instance SA `942580369887-compute@developer.gserviceaccount.com` (default) | Active, but **scoped down** — cannot list the 6 audit buckets |
| Remote VM `35.238.2.51` user-account fallback | `efrat.t@smiti.ai` (configured but inactive, refresh token expired) | Re-login required to use |
| Remote VM `34.56.137.11` | Unreachable at time of preflight | Status pending |

### Implication for Phase 0

- All GCS-touching scripts in Phase 0 (`audit_data.py`, etc.) **run locally**, not on the VM. The local user-account works for `gcloud storage` subprocess calls.
- The VM is unaffected — Phase 0 doesn't need it. Phase 1 / training does, and we'll revisit at that point (probably the simplest path is widening the instance SA's `storage.objectViewer`, not key-shipping).

## Why we don't refresh ADC interactively

The local `~/.config/gcloud/application_default_credentials.json` exists but its refresh token is expired (Apr 26 last good). `gcloud auth application-default login` would fix it but requires opening a browser — and the session decision was no auth prompts. The audit scripts use `gcloud storage` CLI subprocess, which uses the active user account (still valid) instead of ADC. This is a pragmatic choice for Phase 0; Phase 1 may need ADC for SDK calls, at which point we'll re-login once.

## Verifier

`bash scripts/preflight_gcp.sh` checks: project matches `pre_flight.yaml`, at least one of (ADC | metadata server | user-account token) is valid, every audit bucket is listable. Exits non-zero on any failure. **Run at the start of every session.**

## Future work

- If/when ADC refresh is needed for Phase 1 SDK calls, run `gcloud auth application-default login` once locally. (Single user-prompt, not mid-task.)
- If the audit is moved to run on the VM (perf), either:
  - Add `roles/storage.objectViewer` on the audit buckets to `942580369887-compute@developer.gserviceaccount.com`, or
  - Re-login `efrat.t@smiti.ai` on the VM.
- VM `34.56.137.11` access — currently unreachable. Confirm if that VM is still active or has been deprovisioned.
