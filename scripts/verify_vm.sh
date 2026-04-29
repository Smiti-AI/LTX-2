#!/usr/bin/env bash
# End-to-end VM readiness check. Exits non-zero on any failure.
# Run AFTER setup_vm.sh and after staging model weights. See docs/vm_setup.md.
set -uo pipefail

BUCKET="${LTX_BUCKET:-video_gen_dataset}"
PING_OBJECT="_vm_ping_$(date +%s)_$$.txt"
PING_URI="gs://${BUCKET}/${PING_OBJECT}"

fail=0
say_ok()   { printf "  \033[32mOK\033[0m   %s\n" "$1"; }
say_fail() { printf "  \033[31mFAIL\033[0m %s\n" "$1"; fail=1; }

echo "=== verify_vm.sh ==="
echo

# ---- GPU --------------------------------------------------------------------
echo "[gpu]"
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
  nvidia-smi -L | head -1 | sed 's/^/  /'
  say_ok "nvidia-smi reports a GPU"
else
  say_fail "no GPU / nvidia-smi missing"
fi

# ---- ffmpeg drawtext --------------------------------------------------------
echo
echo "[ffmpeg]"
if ffmpeg -hide_banner -filters 2>&1 | grep -qE "^[A-Z\.]+ drawtext"; then
  say_ok "ffmpeg with drawtext filter"
else
  say_fail "ffmpeg drawtext filter missing — install ffmpeg with libfreetype"
fi

# ---- uv ---------------------------------------------------------------------
echo
echo "[uv]"
if [ -x "$HOME/.local/bin/uv" ] || command -v uv >/dev/null 2>&1; then
  ("$HOME/.local/bin/uv" --version 2>/dev/null || uv --version) | sed 's/^/  /'
  say_ok "uv installed"
else
  say_fail "uv missing — run setup_vm.sh"
fi

# ---- system pip deps --------------------------------------------------------
echo
echo "[python deps]"
if python3 -c "import pyarrow, yaml, tqdm" 2>/dev/null; then
  say_ok "pyarrow + pyyaml + tqdm importable"
else
  say_fail "system Python missing pyarrow/pyyaml/tqdm — run setup_vm.sh"
fi

# ---- repo state -------------------------------------------------------------
echo
echo "[repo]"
if [ -d "$HOME/LTX-2/.git" ]; then
  branch=$(git -C "$HOME/LTX-2" rev-parse --abbrev-ref HEAD)
  commit=$(git -C "$HOME/LTX-2" log -1 --format='%h %s')
  echo "  branch=$branch  $commit"
  say_ok "LTX-2 repo present at ~/LTX-2"
else
  say_fail "~/LTX-2 not cloned"
fi

# ---- gcloud auth ------------------------------------------------------------
echo
echo "[gcloud auth]"
if gcloud auth print-access-token >/dev/null 2>&1; then
  acct=$(gcloud config list account --format='value(core.account)' 2>/dev/null)
  echo "  active account: $acct"
  say_ok "gcloud token works"
else
  say_fail "gcloud auth broken — re-login or fix metadata SA"
fi

# ---- OAuth scopes (the scope-vs-IAM trap) ------------------------------------
echo
echo "[gcloud scopes]"
scopes=$(curl -s -m 3 -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/scopes 2>/dev/null || echo "")
if echo "$scopes" | grep -q "auth/cloud-platform"; then
  say_ok "VM scope includes cloud-platform"
elif echo "$scopes" | grep -q "auth/devstorage.read_write"; then
  say_ok "VM scope includes devstorage.read_write (writes will work)"
elif [ -n "$scopes" ]; then
  echo "  current scopes:"
  echo "$scopes" | sed 's/^/    /'
  say_fail "VM scope is read-only for storage. Stop VM and set --scopes=cloud-platform (see docs/vm_setup.md §1)."
else
  echo "  (not a GCE VM — skipping scope check)"
fi

# ---- bucket round-trip (the real proof) -------------------------------------
echo
echo "[bucket round-trip: $PING_URI]"
echo "ping_$(date +%s)" > "/tmp/$PING_OBJECT"
if gcloud storage cp "/tmp/$PING_OBJECT" "$PING_URI" --verbosity=warning 2>/tmp/_cp_err.log; then
  if gcloud storage cat "$PING_URI" >/dev/null 2>&1; then
    gcloud storage rm "$PING_URI" --verbosity=warning >/dev/null 2>&1
    say_ok "wrote, read, deleted ${PING_URI}"
  else
    say_fail "wrote but cannot read back $PING_URI"
  fi
else
  err=$(cat /tmp/_cp_err.log 2>/dev/null | head -2 | tr '\n' ' ')
  say_fail "write to $PING_URI failed: $err"
fi
rm -f /tmp/_cp_err.log "/tmp/$PING_OBJECT"

# ---- model weights ----------------------------------------------------------
echo
echo "[model weights]"
ltx_safetensors=$(ls "$HOME/models/LTX-2.3"/*.safetensors 2>/dev/null | head -1)
gemma_config="$HOME/models/gemma-3-12b-it-qat-q4_0-unquantized/config.json"
if [ -n "$ltx_safetensors" ]; then
  say_ok "LTX-2 weights at $ltx_safetensors"
else
  say_fail "no LTX-2 .safetensors under ~/models/LTX-2.3/ (see docs/vm_setup.md §4)"
fi
if [ -f "$gemma_config" ]; then
  say_ok "Gemma weights at $(dirname "$gemma_config")/"
else
  say_fail "Gemma config.json missing at $gemma_config"
fi

# ---- summary ----------------------------------------------------------------
echo
if [ "$fail" -eq 0 ]; then
  echo "=== ALL CHECKS PASSED ==="
  exit 0
else
  echo "=== $fail CHECK(S) FAILED — see above; refer to docs/vm_setup.md ==="
  exit 1
fi
