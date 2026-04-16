#!/bin/bash
# =============================================================================
# setup_and_serve.sh
# =============================================================================
# Complete setup script for Gemma 4 26B on GCP vGPU instance.
# Run as the vLLM user (not root). Uses sudo internally where needed.
#
# What this does:
#   1. Installs system dependencies (tmux, build tools, etc.)
#   2. Downloads & installs GCP vGPU driver (580.126.09)
#   3. Installs uv + creates vLLM Python venv
#   4. Installs vLLM nightly cu130 + transformers 5.5+
#   5. Fixes CUDA 13 ABI symlinks
#   6. Creates swap (needed for mmap weight loading)
#   7. Copies model weights from GCS (skips if already present)
#   8. Creates HF cache pointer files
#   9. Starts vLLM in a tmux session named 'vllmserve'
#
# Usage:
#   chmod +x setup_and_serve.sh
#   bash setup_and_serve.sh
#
# Re-run safely — all steps are idempotent.
# =============================================================================

set -eo pipefail

# ── Colours ───────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC}   $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
die()     { echo -e "${RED}[FAIL]${NC} $*"; exit 1; }
step()    { echo -e "\n${BOLD}▶ $*${NC}"; }

# ── Config ────────────────────────────────────────────────────
VLLM_USER="${USER}"
HOME_DIR="${HOME}"
VLLM_DIR="${HOME_DIR}/vllm"
VENV_DIR="${VLLM_DIR}/.venv"
PYTHON_VERSION="3.12"

# Model
MODEL_ID="google/gemma-4-26B-A4B-it"
SNAP_HASH="1db3cff1840c2ae59759d8e842ff37831cf8cb63"
HF_CACHE_DIR="${HOME_DIR}/.cache/huggingface/hub/models--google--gemma-4-26B-A4B-it/snapshots/${SNAP_HASH}"
HF_MODEL_ROOT="${HOME_DIR}/.cache/huggingface/hub/models--google--gemma-4-26B-A4B-it"
GCS_MODEL_PATH="gs://gemini-1xn-us-central1-hf-model-cache/model-cache/google/gemma-4-26B-A4B-it"

# Driver
DRIVER_FILE="NVIDIA-Linux-x86_64-580.126.09-grid-gcp.run"
DRIVER_BUCKET="gs://gce-nvidia-vgpu-drivers/G4_VGPU/${DRIVER_FILE}"
DRIVER_PATH="/tmp/${DRIVER_FILE}"

# Chat template
JINJA_PATH="${VLLM_DIR}/gemma4_26b_injected.jinja"

# vLLM serve params
GPU_MEM_UTIL="0.9"
MAX_MODEL_LEN="19456"
MAX_NUM_SEQS="20"
MAX_NUM_BATCHED_TOKENS="19456"
TMUX_SESSION="vllmserve"

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}  Gemma 4 26B — Full Setup & Serve Script               ${NC}"
echo -e "${BOLD}  User    : ${VLLM_USER}                                ${NC}"
echo -e "${BOLD}  vLLM dir: ${VLLM_DIR}                                 ${NC}"
echo -e "${BOLD}  Model   : ${MODEL_ID}                                 ${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════╝${NC}"
echo ""

# =============================================================================
# STEP 1 — System packages
# =============================================================================
step "1/9  System packages"

sudo apt-get update -qq
sudo apt-get install -y \
    tmux \
    curl \
    make \
    gcc \
    build-essential \
    dkms \
    linux-headers-$(uname -r) \
    google-cloud-cli 2>/dev/null || \
sudo apt-get install -y \
    tmux curl make gcc build-essential dkms \
    linux-headers-$(uname -r)

# Fix IPv6 issue — force IPv4 preference to avoid HF download hangs
if ! grep -q "precedence ::ffff:0:0/96" /etc/gai.conf 2>/dev/null; then
    echo 'precedence ::ffff:0:0/96 100' | sudo tee -a /etc/gai.conf > /dev/null
    info "IPv4 preference set in /etc/gai.conf"
fi

success "System packages installed (tmux, build tools, gcloud)"

# =============================================================================
# STEP 2 — vGPU driver
# =============================================================================
step "2/9  NVIDIA vGPU driver"

if nvidia-smi &>/dev/null; then
    DRIVER_VER=$(nvidia-smi | grep "Driver Version" | awk '{print $3}')
    warn "Driver already installed (${DRIVER_VER}) — skipping"
else
    info "Downloading GCP vGPU driver..."
    if [ ! -f "${DRIVER_PATH}" ]; then
        gsutil cp "${DRIVER_BUCKET}" "${DRIVER_PATH}" || \
            die "Driver download failed. Check gcloud auth."
    fi
    chmod +x "${DRIVER_PATH}"

    # Remove any conflicting packages
    sudo apt-get remove -y --purge 'nvidia-*' 'libnvidia-*' 'cuda-*' 2>/dev/null || true
    sudo modprobe -r nvidia_drm nvidia_modeset nvidia_uvm nvidia 2>/dev/null || true

    info "Installing driver (~60 seconds)..."
    sudo "${DRIVER_PATH}" -s --no-drm --no-nouveau-check --no-opengl-files

    sudo mkdir -p /lib/modules/$(uname -r)/updates/dkms
    sudo depmod -a

    sudo modprobe nvidia
    sudo modprobe nvidia_uvm
    sudo modprobe nvidia_modeset
    printf 'nvidia\nnvidia_uvm\nnvidia_modeset\n' | sudo tee /etc/modules-load.d/nvidia.conf > /dev/null

    nvidia-smi || die "nvidia-smi failed after install"
    success "Driver installed"
fi

# =============================================================================
# STEP 3 — uv + Python venv
# =============================================================================
step "3/9  Python environment"

# Install uv if missing
if ! command -v uv &>/dev/null; then
    info "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    source "${HOME_DIR}/.local/bin/env" 2>/dev/null || \
        export PATH="${HOME_DIR}/.local/bin:${PATH}"
fi
# Ensure uv is in PATH even if already installed
export PATH="${HOME_DIR}/.local/bin:${PATH}"
success "uv $(uv --version)"

# Create vLLM dir and venv
mkdir -p "${VLLM_DIR}"
if [ ! -d "${VENV_DIR}" ]; then
    info "Creating venv at ${VENV_DIR}..."
    cd "${VLLM_DIR}"
    uv venv --python "${PYTHON_VERSION}" --seed
    success "venv created"
else
    warn "venv already exists — skipping"
fi

# =============================================================================
# STEP 4 — Install vLLM
# =============================================================================
step "4/9  vLLM installation"

source "${VENV_DIR}/bin/activate"

VLLM_INSTALLED=$(python -c "import vllm; print(vllm.__version__)" 2>/dev/null || echo "")
if [ -n "${VLLM_INSTALLED}" ]; then
    warn "vLLM already installed (${VLLM_INSTALLED}) — skipping"
else
    info "Installing vLLM nightly cu130..."
    pip install vllm \
        --pre \
        --index-url https://wheels.vllm.ai/nightly/cu130 \
        --extra-index-url https://download.pytorch.org/whl/cu130 \
        --extra-index-url https://pypi.org/simple \
        --quiet
    success "vLLM installed: $(python -c 'import vllm; print(vllm.__version__)')"
fi

# Install transformers 5.5+ (required for Gemma 4)
# Note: vLLM may show a dependency conflict warning — this is expected and safe to ignore.
# transformers 5.5+ is required for Gemma 4 model support.
TRANSFORMERS_INSTALLED=$(python -c "import transformers; print(transformers.__version__)" 2>/dev/null || echo "0")
NEEDS_UPGRADE=$(python -c "
from packaging.version import Version
v = '${TRANSFORMERS_INSTALLED}'
print('no' if v != '0' and Version(v) >= Version('5.5.0') else 'yes')
" 2>/dev/null || echo "yes")

if [ "${NEEDS_UPGRADE}" = "no" ]; then
    warn "transformers ${TRANSFORMERS_INSTALLED} already >= 5.5.0 — skipping"
else
    info "Installing transformers>=5.5.0 (dependency conflict warning is expected)..."
    pip install "transformers>=5.5.0" --quiet 2>&1 | grep -v "^WARNING\|dependency resolver" || true
    success "transformers $(python -c 'import transformers; print(transformers.__version__)') installed"
fi

# =============================================================================
# STEP 5 — CUDA 13 ABI symlinks
# =============================================================================
step "5/9  CUDA 13 ABI fix"

CUDA13_LIB="${VENV_DIR}/lib/python${PYTHON_VERSION}/site-packages/nvidia/cu13/lib"
TORCH_LIB="${VENV_DIR}/lib/python${PYTHON_VERSION}/site-packages/torch/lib"

if [ -f "${CUDA13_LIB}/libcudart.so.13" ]; then
    ln -sf "${CUDA13_LIB}/libcudart.so.13" "${CUDA13_LIB}/libcudart.so.12" 2>/dev/null || true
    ln -sf "${CUDA13_LIB}/libcudart.so.13" "${TORCH_LIB}/libcudart.so.12"  2>/dev/null || true
    success "libcudart.so.12 symlinks created"
else
    warn "libcudart.so.13 not found at expected path — check vLLM install"
fi

# Persist LD_LIBRARY_PATH to ~/.bashrc
if ! grep -q "nvidia/cu13/lib" "${HOME_DIR}/.bashrc" 2>/dev/null; then
    echo "" >> "${HOME_DIR}/.bashrc"
    echo "# vLLM CUDA 13 environment" >> "${HOME_DIR}/.bashrc"
    echo "export LD_LIBRARY_PATH=${CUDA13_LIB}:\${LD_LIBRARY_PATH:-}" >> "${HOME_DIR}/.bashrc"
    echo "[ ! -f '${CUDA13_LIB}/libcudart.so.12' ] && ln -sf '${CUDA13_LIB}/libcudart.so.13' '${CUDA13_LIB}/libcudart.so.12' 2>/dev/null" >> "${HOME_DIR}/.bashrc"
    echo "[ ! -f '${TORCH_LIB}/libcudart.so.12' ]  && ln -sf '${CUDA13_LIB}/libcudart.so.13' '${TORCH_LIB}/libcudart.so.12'  2>/dev/null" >> "${HOME_DIR}/.bashrc"
    success "LD_LIBRARY_PATH persisted to ~/.bashrc"
fi

export LD_LIBRARY_PATH="${CUDA13_LIB}:${LD_LIBRARY_PATH:-}"

# Verify vLLM loads
python -c "import vllm._C" 2>/dev/null && success "vllm._C import OK" || \
    warn "vllm._C import failed — CUDA symlink may still be wrong"

# =============================================================================
# STEP 6 — Swap space (required for mmap weight loading)
# =============================================================================
step "6/9  Swap space"

SWAP_ACTIVE=$(swapon --show --noheadings 2>/dev/null | wc -l)
if [ "${SWAP_ACTIVE}" -gt 0 ]; then
    SWAP_SIZE=$(swapon --show --noheadings 2>/dev/null | awk '{print $3}' | head -1)
    warn "Swap already active (${SWAP_SIZE}) — skipping"
else
    info "Creating 32GB swap file (required for safetensors mmap loading)..."
    sudo fallocate -l 32G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile
    sudo swapon /swapfile

    # Persist across reboots
    if ! grep -q "/swapfile" /etc/fstab; then
        echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab > /dev/null
    fi

    SWAP_TOTAL=$(free -h | grep Swap | awk '{print $2}')
    success "Swap created: ${SWAP_TOTAL} total"
fi

# =============================================================================
# STEP 7 — Model weights from GCS
# =============================================================================
step "7/9  Model weights"

mkdir -p "${HF_CACHE_DIR}"

SHARD_COUNT=$(ls "${HF_CACHE_DIR}"/*.safetensors 2>/dev/null | wc -l || echo 0)
if [ "${SHARD_COUNT}" -ge 2 ]; then
    TOTAL_SIZE=$(du -sh "${HF_CACHE_DIR}" 2>/dev/null | cut -f1)
    warn "Weights already present (${SHARD_COUNT} shards, ${TOTAL_SIZE}) — skipping download"
else
    info "Copying weights from GCS (2-5 min on GCP internal network)..."
    info "  Source: ${GCS_MODEL_PATH}"
    info "  Dest  : ${HF_CACHE_DIR}"

    # Use rsync — avoids wildcard expansion issues with gcloud storage cp
    gcloud storage rsync -r \
        "${GCS_MODEL_PATH}" \
        "${HF_CACHE_DIR}" || die "GCS weight copy failed"

    SHARD_COUNT=$(ls "${HF_CACHE_DIR}"/*.safetensors 2>/dev/null | wc -l || echo 0)
    TOTAL_SIZE=$(du -sh "${HF_CACHE_DIR}" 2>/dev/null | cut -f1)
    [ "${SHARD_COUNT}" -ge 2 ] || die "Weight copy failed — no safetensors files found in ${HF_CACHE_DIR}"
    success "Weights copied: ${SHARD_COUNT} shards, ${TOTAL_SIZE}"
fi

# =============================================================================
# STEP 8 — HF cache pointer
# =============================================================================
step "8/9  HF cache pointer"

mkdir -p "${HF_MODEL_ROOT}/refs"
echo "${SNAP_HASH}" > "${HF_MODEL_ROOT}/refs/main"
success "refs/main → ${SNAP_HASH}"

# =============================================================================
# STEP 9 — Start vLLM in tmux
# =============================================================================
step "9/9  Starting vLLM in tmux session '${TMUX_SESSION}'"

# Check if jinja template exists
if [ ! -f "${JINJA_PATH}" ]; then
    warn "Chat template not found at ${JINJA_PATH}"
    warn "Copy gemma4_26b_injected.jinja to ${JINJA_PATH} and restart"
    TEMPLATE_FLAG=""
else
    TEMPLATE_FLAG="--chat-template ${JINJA_PATH} --chat-template-content-format string"
    success "Chat template found: ${JINJA_PATH}"
fi

# Kill existing session if running
tmux kill-session -t "${TMUX_SESSION}" 2>/dev/null && \
    info "Killed existing tmux session '${TMUX_SESSION}'" || true

# Build serve command
SERVE_CMD="cd ${VLLM_DIR} \
&& source ${VENV_DIR}/bin/activate \
&& export LD_LIBRARY_PATH=${CUDA13_LIB}:\${LD_LIBRARY_PATH:-} \
&& HF_HUB_OFFLINE=1 vllm serve ${HF_CACHE_DIR} \
    --gpu-memory-utilization ${GPU_MEM_UTIL} \
    --max-model-len ${MAX_MODEL_LEN} \
    --max-num-seqs ${MAX_NUM_SEQS} \
    --max-num-batched-tokens ${MAX_NUM_BATCHED_TOKENS} \
    --dtype auto \
    --quantization fp8 \
    --enable-auto-tool-choice \
    --tool-call-parser gemma4 \
    --reasoning-parser gemma4 \
    ${TEMPLATE_FLAG} \
    --served-model-name ${MODEL_ID} \
    --disable-log-requests"

# Start in tmux
tmux new-session -d -s "${TMUX_SESSION}" -x 220 -y 50
tmux send-keys -t "${TMUX_SESSION}" "${SERVE_CMD}" Enter

success "vLLM started in tmux session '${TMUX_SESSION}'"

# =============================================================================
# Summary
# =============================================================================
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}  Setup Complete!                                       ${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${CYAN}Model weights :${NC} ${HF_CACHE_DIR}"
echo -e "  ${CYAN}vLLM env      :${NC} ${VENV_DIR}"
echo -e "  ${CYAN}tmux session  :${NC} ${TMUX_SESSION}"
echo -e "  ${CYAN}Serve params  :${NC} max_model_len=${MAX_MODEL_LEN} max_num_seqs=${MAX_NUM_SEQS} gpu_mem=${GPU_MEM_UTIL}"
echo ""
echo -e "${YELLOW}Useful commands:${NC}"
echo ""
echo -e "  # Watch vLLM logs"
echo -e "  tmux attach -t ${TMUX_SESSION}"
echo -e "  # Detach: Ctrl+B D"
echo ""
echo -e "  # Monitor GPU memory"
echo -e "  watch -n5 nvidia-smi"
echo ""
echo -e "  # Health check (after ~15 min startup)"
echo -e "  curl http://localhost:8000/health"
echo ""
echo -e "${YELLOW}Note:${NC} Weight loading takes 8-15 minutes. Watch tmux logs."
echo -e "      You'll see 'Uvicorn running on http://0.0.0.0:8000' when ready."
echo ""