# GPU VM Setup — Bootstrap Guide

> **Goal:** spin up a fresh GPU VM and have it fully operational for the
> training pipeline in under 15 minutes. Two scripts handle the parts that
> can be automated; this doc covers the parts that must be done by a human.

Last verified: **2026-04-29** on `ltx-2-development` (`35.238.2.51`).

---

## TL;DR — five steps, in order

1. **Create the VM with the right OAuth scopes** (do NOT use defaults — they're read-only for storage). See §1.
2. **Bind the SA to the GCS bucket** with `roles/storage.objectAdmin`. See §2.
3. **SSH in and run `bash scripts/setup_vm.sh`** — installs ffmpeg, uv, clones the repo, installs Python deps. See §3.
4. **Stage the LTX-2 + Gemma model weights** at `~/models/`. See §4.
5. **Run `bash scripts/verify_vm.sh`** — proves auth, GPU, ffmpeg, model paths, and bucket write all work end-to-end. See §5.

After §5 passes, the VM is ready for `bash scripts/dataset_pipeline/build_full.sh chase|skye`.

---

## §1 — VM creation: OAuth scopes are NOT IAM

> **The most important lesson learned from this project.** GCE VMs carry
> two separate authorization layers: IAM (what the SA is *allowed* to do)
> and OAuth scopes (what the metadata-server-issued token *can ask for*).
> Even with full IAM, a token issued under `devstorage.read_only` will
> refuse writes with `403 "Provided scope(s) are not authorized"`.

**Always launch GPU training VMs with `--scopes=cloud-platform`.**

### Console UI
**Compute Engine → Create Instance → Identity and API access → Cloud API access scopes → "Allow full access to all Cloud APIs"** (NOT the default "Allow default access").

### gcloud equivalent
```bash
gcloud compute instances create NEW_VM_NAME \
  --zone=us-central1-c   # ltx-2-development lives here; verify with: gcloud compute instances list \
  --machine-type=a2-highgpu-1g \
  --accelerator=type=nvidia-a100-80gb,count=1 \
  --boot-disk-size=1000GB \
  --image-family=pytorch-latest-gpu \
  --image-project=deeplearning-platform-release \
  --service-account=942580369887-compute@developer.gserviceaccount.com \
  --scopes=cloud-platform
```

### Already-running VM with wrong scopes? Fix without rebuilding:
```bash
# If the VM has a local SSD attached, gcloud requires --discard-local-ssd=true|false.
# Local SSD is zonal scratch — its contents ARE lost on stop. Confirm nothing
# important is mounted from it via:
#   ssh USER@VM 'df -h ~ ~/LTX-2 ~/models; lsblk -o NAME,SIZE,TYPE,MOUNTPOINT'
# `~/` typically lives on `/dev/sda1` (persistent boot disk) and survives stop.
gcloud compute instances stop VM_NAME --zone=ZONE --discard-local-ssd=true
gcloud compute instances set-service-account VM_NAME \
  --service-account=SA_EMAIL \
  --scopes=cloud-platform \
  --zone=ZONE
gcloud compute instances start VM_NAME --zone=ZONE
```
~2 min downtime. The boot disk persists across stop/start, so files under `~/` survive.

**Verify the zone first** — `ltx-2-development` lives in `us-central1-c`, NOT
`-a` despite the project default. Always:
```bash
gcloud compute instances list --filter="name=VM_NAME" --format="value(zone.basename())"
```

### Verify scopes after boot
```bash
ssh USER@VM 'curl -s -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/scopes'
```
Should include `https://www.googleapis.com/auth/cloud-platform`.

---

## §2 — IAM: bind the SA to the bucket

Even with `cloud-platform` scope, the SA needs an IAM role granting actual
permissions on the bucket. Bind once per bucket, per SA.

```bash
# Replace the SA email and bucket as needed.
gcloud storage buckets add-iam-policy-binding gs://video_gen_dataset \
  --member="serviceAccount:942580369887-compute@developer.gserviceaccount.com" \
  --role="roles/storage.objectAdmin"
```

**Role choice — `roles/storage.objectAdmin`:** grants `objects.create`, `get`, `list`, `delete`, `update`. Bucket-scoped only — does NOT grant access to other buckets in the project.

If you want to scope tighter (e.g., the SA can only write under `TinyStories/training_data/`), add an IAM Condition:
```bash
gcloud storage buckets add-iam-policy-binding gs://video_gen_dataset \
  --member="serviceAccount:SA_EMAIL" \
  --role="roles/storage.objectAdmin" \
  --condition='expression=resource.name.startsWith("projects/_/buckets/video_gen_dataset/objects/TinyStories/training_data/"),title=training_data_only'
```

### Verify IAM after bind
```bash
gcloud storage buckets get-iam-policy gs://BUCKET --format=json | \
  jq '.bindings[] | select(.role=="roles/storage.objectAdmin")'
```
The SA should appear in `members`.

---

## §3 — On-VM bootstrap: `scripts/setup_vm.sh`

Idempotent. Re-runnable. Installs:

- `ffmpeg` + build essentials (apt)
- `uv` (Python package manager) into `~/.local/bin/`
- `pyarrow`, `pyyaml`, `tqdm` into the system Python (used by dataset_pipeline scripts that aren't part of the trainer venv)
- Clones `https://github.com/Smiti-AI/LTX-2.git` into `~/LTX-2/` if not present, otherwise `git pull`
- Sets up the trainer venv via `uv sync` inside `~/LTX-2/`
- Creates working directories (`~/training_data_full/`, `~/models/`)

### Run it
```bash
ssh USER@VM 'bash <(curl -fsSL https://raw.githubusercontent.com/Smiti-AI/LTX-2/main/scripts/setup_vm.sh)'
```
Or, if you've already cloned the repo:
```bash
cd ~/LTX-2 && bash scripts/setup_vm.sh
```

Expected runtime: ~5 minutes on a fresh deep-learning VM image (most components already pre-installed).

---

## §4 — Model weights

The trainer needs LTX-2 + Gemma weights on the VM's local disk. URLs are
not supported. The two we use today:

```
~/models/
├── LTX-2.3/
│   ├── ltx-2.3-22b-dev.safetensors          (the training target)
│   ├── ltx-2.3-22b-distilled-lora-384.safetensors  (optional inference variant)
│   └── ltx-2.3-spatial-upscaler-x2-1.0.safetensors (optional)
└── gemma-3-12b-it-qat-q4_0-unquantized/
    ├── config.json
    ├── tokenizer.model
    ├── tokenizer_config.json
    └── model-00001-of-00005.safetensors … model-00005-of-00005.safetensors
```

### Stage from another VM (if mirrored to GCS)
```bash
gsutil -m cp -r gs://shapeshifter-459611-models/LTX-2.3/ ~/models/
gsutil -m cp -r gs://shapeshifter-459611-models/gemma-3-12b-it-qat-q4_0-unquantized/ ~/models/
```

### Or download from Hugging Face (gated; HF_TOKEN required for Gemma)
```bash
huggingface-cli login   # paste your HF_TOKEN with read scope
huggingface-cli download Lightricks/LTX-Video --local-dir ~/models/LTX-2.3/
huggingface-cli download google/gemma-3-12b-it-qat-q4_0-unquantized --local-dir ~/models/gemma-3-12b-it-qat-q4_0-unquantized/
```

(Exact HF model IDs may shift — the canonical names are in the active configs under `packages/ltx-trainer/configs/`.)

---

## §5 — Verify: `scripts/verify_vm.sh`

Runs a checklist proving the VM can train:

| Check | Pass criterion |
|---|---|
| `nvidia-smi` returns at least one GPU | required |
| `ffmpeg -filters \| grep drawtext` finds the filter | required (gallery render) |
| `uv --version` works | required (trainer invocation) |
| `gcloud auth print-access-token` succeeds | required |
| OAuth scope includes `cloud-platform` | required (writes to GCS) |
| `gcloud storage cp /tmp/ping → gs://BUCKET/_vm_ping → cat → rm` | required (end-to-end IAM + scope) |
| Model paths exist (`~/models/LTX-2.3/...safetensors`, `~/models/gemma.../config.json`) | required |
| `pyarrow`, `pyyaml`, `tqdm` importable in system Python | required |
| `~/LTX-2` repo cloned, on `main`, up-to-date | required |

```bash
bash ~/LTX-2/scripts/verify_vm.sh
```
Exits 0 if all checks pass; non-zero with a clear summary otherwise.

---

## §6 — Common failure modes & fixes

| Symptom | Likely cause | Fix |
|---|---|---|
| `ERROR: ... 403 "Provided scope(s) are not authorized"` on a `gcloud storage cp` write | OAuth scope is `devstorage.read_only` (default) | Stop VM, set `--scopes=cloud-platform`, start (§1) |
| Same 403 error AFTER the scope was already changed (curl with the metadata token works, but gcloud/gsutil keep saying 403) | gcloud cached an old token from before the scope change | `rm -f ~/.config/gcloud/access_tokens.db ~/.config/gcloud/application_default_credentials.json` then retry. gcloud will re-fetch from the metadata server. |
| `403 "does not have storage.objects.list access"` | Missing IAM binding | `gcloud storage buckets add-iam-policy-binding ...` (§2) |
| `gcloud auth print-access-token` fails on the developer's local machine | Refresh token expired | `gcloud auth login` (interactive once) |
| `uv: command not found` under nohup | uv installed at `~/.local/bin/uv` not on nohup's PATH | Use full path `/home/USER/.local/bin/uv` or wrap in `bash -lc` |
| `pgrep -fa PATTERN \| wc -l` from local-side `ssh` returns 1 even when no match | The bash command line itself contains PATTERN → pgrep matches itself | Quote pattern differently or use `pgrep -f PATTERN > /dev/null 2>&1; echo $?` instead |
| `nohup: failed to run command 'uv'` | Same PATH issue | Same fix |
| ffmpeg `drawtext` filter missing | Default Homebrew ffmpeg lacks freetype | On macOS: `brew install ffmpeg --with-freetype`. On Ubuntu/Debian: stock `apt install ffmpeg` works (built `--enable-libfreetype`) |

---

## §7 — Reference: SAs and buckets in this project

| Name | Purpose | Bound to |
|---|---|---|
| `942580369887-compute@developer.gserviceaccount.com` | Default Compute Engine SA — used by training VMs | `roles/storage.objectAdmin` on `gs://video_gen_dataset` |
| `efrat.t@smiti.ai` | Developer user account, full project owner | All buckets, all roles |
| `gs://video_gen_dataset` | Datasets, raw episodes, processed datasets, training_runs | Read by VMs, written by VMs (under the IAM scope above) |
| `gs://shapeshifter-459611-models` | Mirrored model weights for fast VM-side staging | Read-only via SA |

If you create a new SA per-VM (instead of reusing the default compute SA), you'll need to repeat §2 for that SA's email.

---

## §8 — Forward-compatibility notes

- This guide assumes a single-GPU training VM. Multi-GPU adds: `accelerate launch` instead of `python scripts/train.py`, plus an `accelerate config` step.
- If you migrate to a GKE Autopilot or Vertex AI custom job, the IAM model is the same but the scope mechanism is different (Workload Identity Federation, or Vertex's managed credentials). The `cloud-platform` scope advice applies only to GCE VMs.
- Always test §5 (`verify_vm.sh`) immediately after VM creation — catches scope/IAM/PATH issues before they sink an overnight training run.
