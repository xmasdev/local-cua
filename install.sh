#!/bin/bash
# install.sh: set up local-cua on an Arch + Hyprland machine (safe to re-run).
#   ./install.sh            link commands + services, set up input access
#   ./install.sh --models   also download the models (~4 GB for the agent, ~6 GB for the chat model)
set -euo pipefail
REPO=$(cd "$(dirname "$0")" && pwd)
say()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32mok\033[0m  %s\n' "$*"; }
warn() { printf '    \033[33m!!\033[0m  %s\n' "$*"; }

[ "$REPO" = "$HOME/cua" ] || warn "the scripts expect the repo at ~/cua (it is at $REPO). Move it or symlink it: ln -s $REPO ~/cua"

say "Dependencies"
for c in hyprctl grim ydotool python3 curl; do command -v $c >/dev/null && ok "$c" || warn "$c missing (pacman -S ${c/hyprctl/hyprland})"; done
python3 -c "import PIL" 2>/dev/null && ok "python-pillow" || warn "python-pillow missing (pacman -S python-pillow)"
if command -v llama-server >/dev/null || [ -x ~/llama.cpp/build/bin/llama-server ]; then ok "llama-server"
else warn "llama-server not found: build llama.cpp with CUDA (or set LLAMA_SERVER=/path/to/llama-server)"; fi

say "Commands and services"
mkdir -p ~/.local/bin ~/.config/systemd/user
for f in "$REPO"/bin/*; do ln -sfn "$f" ~/.local/bin/"$(basename "$f")"; done
ok "linked cua, cua-link, cua-once into ~/.local/bin"
for u in "$REPO"/systemd/*.service; do ln -sfn "$u" ~/.config/systemd/user/"$(basename "$u")"; done
systemctl --user daemon-reload
ok "linked cua.service and llama-router.service (nothing starts at login; use 'cua start')"

say "Virtual mouse/keyboard (ydotool needs /dev/uinput)"
if [ -w /dev/uinput ]; then ok "/dev/uinput is writable"
else
  echo "    Granting the logged-in user access to /dev/uinput (asks for sudo):"
  echo 'KERNEL=="uinput", TAG+="uaccess"' | sudo tee /etc/udev/rules.d/60-uinput-uaccess.rules >/dev/null
  echo uinput | sudo tee /etc/modules-load.d/uinput.conf >/dev/null
  sudo modprobe uinput
  sudo udevadm control --reload
  sudo udevadm trigger --action=add /sys/devices/virtual/misc/uinput
  [ -w /dev/uinput ] && ok "/dev/uinput is writable" || warn "still not writable; log out and back in"
fi

if [ "${1:-}" = "--models" ]; then
  say "Models (into ~/models/llama.cpp)"
  command -v hf >/dev/null || { warn "install the Hugging Face CLI first: pipx install huggingface_hub"; exit 1; }
  M=~/models/llama.cpp; mkdir -p "$M"/Holo-3.1-4B "$M"/Qwen3.5-9B
  hf download mradermacher/Holo-3.1-4B-GGUF Holo-3.1-4B.Q5_K_M.gguf Holo-3.1-4B.mmproj-Q8_0.gguf --local-dir "$M"/Holo-3.1-4B
  [ -f "$M"/Holo-3.1-4B/mmproj-Q8_0.gguf ] || mv "$M"/Holo-3.1-4B/Holo-3.1-4B.mmproj-Q8_0.gguf "$M"/Holo-3.1-4B/mmproj-Q8_0.gguf
  hf download unsloth/Qwen3.5-9B-GGUF Qwen3.5-9B-IQ4_XS.gguf mmproj-F16.gguf --local-dir "$M"/Qwen3.5-9B
  ok "models ready"
fi

say "Optional: kill switch for 'cua-once' (add to your Hyprland config)"
echo '    hl.bind("SUPER + SHIFT + Escape", hl.dsp.exec_cmd("pkill -INT -f cua/agent.py"))'

say "Done"
echo "    cua start   then   cua \"open firefox and search for the weather\""
echo "    Phone access: scripts/phone-setup.sh"
