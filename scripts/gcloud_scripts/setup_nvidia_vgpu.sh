#!/bin/bash
# =============================================================================
# GCP vGPU Driver Setup — Gemma 4 26B Instance
# =============================================================================
# Learned from instance-20260409-182425 setup session.
#
# KEY FACTS:
#   - GCP exposes GPUs as vGPU, NOT bare metal
#   - Standard NVIDIA drivers (570, 580 from apt) FAIL with:
#       "NVRM: The NVIDIA vGPU 10de:XXXX is not supported by open nvidia.ko"
#   - You MUST use GCP's specific vGPU driver from their Cloud Storage bucket
#   - Driver: NVIDIA-Linux-x86_64-580.126.09-grid-gcp.run
#
# USAGE:
#   gcloud compute scp setup_nvidia_vgpu.sh USER@INSTANCE:~ --zone us-central1-b
#   ssh into instance, then: chmod +x setup_nvidia_vgpu.sh && sudo bash setup_nvidia_vgpu.sh
#
# Or run inline over SSH:
#   gcloud compute ssh USER@instance-20260409-20260410-041212 \
#       --zone us-central1-b -- 'bash -s' < setup_nvidia_vgpu.sh
# =============================================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC}   $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
die()     { echo -e "${RED}[FAIL]${NC} $*"; exit 1; }

# =============================================================================
# STEP 1 — Detect GPU PCI ID
# =============================================================================
info "Detecting GPU..."
GPU_PCI=$(lspci | grep -i nvidia | head -1 || true)
if [ -z "$GPU_PCI" ]; then
    die "No NVIDIA GPU detected. Wrong instance?"
fi
echo "  Found: $GPU_PCI"

# =============================================================================
# STEP 2 — Remove any existing NVIDIA packages (they won't work on vGPU)
# =============================================================================
info "Removing any existing NVIDIA packages..."
sudo apt-get remove -y --purge 'nvidia-*' 'libnvidia-*' 'cuda-*' 2>/dev/null || true
sudo apt-get autoremove -y 2>/dev/null || true

# Remove any kernel modules that might be loaded
sudo modprobe -r nvidia_drm nvidia_modeset nvidia_uvm nvidia 2>/dev/null || true
success "Existing NVIDIA packages removed"

# =============================================================================
# STEP 3 — Install build dependencies
# =============================================================================
info "Installing kernel headers and build tools..."
sudo apt-get update -qq
sudo apt-get install -y \
    linux-headers-$(uname -r) \
    build-essential \
    dkms \
    gcc \
    make \
    curl \
    google-cloud-cli 2>/dev/null || \
sudo apt-get install -y \
    linux-headers-$(uname -r) \
    build-essential \
    gcc \
    make \
    curl
success "Build dependencies installed"

# =============================================================================
# STEP 4 — Download GCP vGPU driver
# =============================================================================
DRIVER_FILE="NVIDIA-Linux-x86_64-580.126.09-grid-gcp.run"
DRIVER_BUCKET="gs://gce-nvidia-vgpu-drivers/G4_VGPU/${DRIVER_FILE}"
DRIVER_PATH="/tmp/${DRIVER_FILE}"

info "Downloading GCP vGPU driver from ${DRIVER_BUCKET}..."
if [ ! -f "$DRIVER_PATH" ]; then
    gsutil cp "$DRIVER_BUCKET" "$DRIVER_PATH" || \
        die "Failed to download driver. Make sure gsutil/gcloud is authenticated."
else
    warn "Driver already present at $DRIVER_PATH, skipping download"
fi
chmod +x "$DRIVER_PATH"
success "Driver downloaded: $(du -h $DRIVER_PATH | cut -f1)"

# =============================================================================
# STEP 5 — Install the vGPU driver
# =============================================================================
info "Installing vGPU driver (this takes ~60 seconds)..."
sudo "$DRIVER_PATH" \
    -s \
    --no-drm \
    --no-nouveau-check \
    --no-opengl-files
success "Driver installed"

# =============================================================================
# STEP 6 — Fix module priority (vGPU driver must take precedence)
# =============================================================================
# Without this, the kernel may load a stale/wrong nvidia.ko from
# kernel/nvidia-XXXsrv/ instead of the freshly installed one in updates/dkms/
info "Fixing module priority with depmod..."
sudo mkdir -p /lib/modules/$(uname -r)/updates/dkms
sudo depmod -a
success "depmod -a completed"

# =============================================================================
# STEP 7 — Load modules
# =============================================================================
info "Loading NVIDIA kernel modules..."
sudo modprobe nvidia
sudo modprobe nvidia_uvm
sudo modprobe nvidia_modeset
success "Modules loaded"

# =============================================================================
# STEP 8 — Make modules persistent across reboots
# =============================================================================
info "Making modules persistent..."
cat << 'EOF' | sudo tee /etc/modules-load.d/nvidia.conf
nvidia
nvidia_uvm
nvidia_modeset
EOF
success "Persistence configured"

# =============================================================================
# STEP 9 — Verify
# =============================================================================
info "Verifying installation..."
echo ""
if nvidia-smi; then
    echo ""
    success "nvidia-smi works! Driver installation successful."
    echo ""
    # Show CUDA version
    CUDA_VER=$(nvidia-smi | grep "CUDA Version" | awk '{print $NF}' || echo "unknown")
    DRIVER_VER=$(nvidia-smi | grep "Driver Version" | awk '{print $3}' || echo "unknown")
    GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader || echo "unknown")
    echo -e "  ${CYAN}GPU     :${NC} $GPU_NAME"
    echo -e "  ${CYAN}Driver  :${NC} $DRIVER_VER"
    echo -e "  ${CYAN}CUDA    :${NC} $CUDA_VER"
else
    echo ""
    die "nvidia-smi failed. Check dmesg for errors: sudo dmesg | grep -i nvidia"
fi

# =============================================================================
# STEP 10 — Setup vLLM environment (optional, run separately if preferred)
# =============================================================================
echo ""
echo -e "${YELLOW}========================================${NC}"
echo -e "${YELLOW}  Next steps to install vLLM for 26B:   ${NC}"
echo -e "${YELLOW}========================================${NC}"
cat << 'NEXT'

# Switch to your user (not root):
sudo su - $SUDO_USER   # or: sudo su - shiladitya

# Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env

# Create vLLM environment
mkdir ~/vllm && cd ~/vllm
uv venv --python 3.12 --seed
source .venv/bin/activate

# Install vLLM + transformers (transformers 5.5+ required for Gemma 4)
uv pip install vllm --torch-backend=auto
uv pip install --upgrade transformers

# Serve Gemma 4 26B-A4B with thinking + tool calling (both work on 26B!)
vllm serve google/gemma-4-26B-A4B-it \
    --gpu-memory-utilization 0.90 \
    --max-model-len 8192 \
    --dtype auto \
    --enable-auto-tool-choice \
    --tool-call-parser gemma4 \
    --reasoning-parser gemma4

# Unlike E4B, on 26B-A4B:
#   - reasoning_content will be populated (thinking + tools work simultaneously)
#   - No need for the --chat-template injection workaround
#   - The model genuinely thinks before each tool call

NEXT
