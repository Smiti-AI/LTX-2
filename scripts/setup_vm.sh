#!/usr/bin/env bash
# Bootstrap a fresh GPU VM for the LTX-2 training pipeline.
# Idempotent — re-run any time. See docs/vm_setup.md for the full guide
# (including the prerequisite VM-creation OAuth scope and bucket IAM bindings
# that this script CANNOT do from inside the VM).
set -euo pipefail

REPO_URL="https://github.com/Smiti-AI/LTX-2.git"
REPO_DIR="$HOME/LTX-2"
LOG=/tmp/setup_vm_$$.log
exec > >(tee -a "$LOG") 2>&1

echo "=== setup_vm.sh ==="
date

# ---- 0. Sanity checks --------------------------------------------------------
if ! curl -s -m 3 -H "Metadata-Flavor: Google" \
     http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email \
     >/dev/null 2>&1; then
  echo "WARN: not running on a GCE VM (or metadata server unreachable). Continuing anyway."
fi

# ---- 1. apt: ffmpeg + build essentials ---------------------------------------
echo
echo "[1/6] apt packages"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  ffmpeg \
  build-essential \
  python3-pip \
  curl \
  jq \
  git
ffmpeg -hide_banner -filters 2>&1 | grep -E "^[A-Z\.]+ drawtext" >/dev/null \
  && echo "  ffmpeg drawtext filter: OK" \
  || { echo "  ffmpeg drawtext filter MISSING"; exit 1; }

# ---- 2. uv (Python package manager) ------------------------------------------
echo
echo "[2/6] uv"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"
uv --version

# ---- 3. system Python deps used by dataset_pipeline scripts -----------------
echo
echo "[3/6] system pip deps for dataset_pipeline (pyarrow, pyyaml, tqdm)"
pip3 install --user --quiet --upgrade pyarrow pyyaml tqdm
python3 -c "import pyarrow, yaml, tqdm; print(f'  pyarrow {pyarrow.__version__}, yaml {yaml.__version__}, tqdm {tqdm.__version__}')"

# ---- 4. clone or update the repo --------------------------------------------
echo
echo "[4/6] LTX-2 repo at $REPO_DIR"
if [ -d "$REPO_DIR/.git" ]; then
  git -C "$REPO_DIR" fetch --quiet
  git -C "$REPO_DIR" pull --ff-only --quiet
  echo "  pulled latest main"
else
  git clone --quiet "$REPO_URL" "$REPO_DIR"
  echo "  cloned $REPO_URL"
fi
git -C "$REPO_DIR" log -1 --oneline

# ---- 5. trainer venv via uv -------------------------------------------------
echo
echo "[5/6] trainer venv (uv sync inside repo)"
cd "$REPO_DIR"
uv sync --quiet
echo "  uv sync complete; .venv ready at $REPO_DIR/.venv"

# ---- 6. working directories --------------------------------------------------
echo
echo "[6/6] working dirs"
mkdir -p "$HOME/training_data_full"
mkdir -p "$HOME/models"
mkdir -p "/tmp/manifests"   # build_poc.py looks here for parquets/JSONs
echo "  ~/training_data_full ($HOME/training_data_full)"
echo "  ~/models             ($HOME/models)         <- stage LTX-2 + Gemma weights here"
echo "  /tmp/manifests       (parquet cache for build_poc.py)"

echo
echo "=== setup_vm.sh DONE ==="
echo "Next: stage model weights into ~/models/ (see docs/vm_setup.md §4),"
echo "      then run: bash $REPO_DIR/scripts/verify_vm.sh"
