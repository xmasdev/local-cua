#!/bin/bash
# phone-setup.sh: make the CUA web app reachable from your phone through Tailscale (safe to re-run).
#   - installs + starts Tailscale (asks for your sudo password)
#   - logs this PC into your tailnet and lets you run `tailscale serve` without sudo
#   - publishes the app privately at https://<pc>.<tailnet>.ts.net (tailnet only)
#   - starts the agent once (cua start) so the link can be tested
#   - prints the phone sign-in link + QR code
set -euo pipefail
say() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()  { printf '    \033[32m✓\033[0m %s\n' "$*"; }

# ---------------------------------------------------------------- packages
say "Packages"
need=()
for p in tailscale qrencode; do pacman -Qq "$p" &>/dev/null && ok "$p installed" || need+=("$p"); done
if ((${#need[@]})); then sudo pacman -S --needed --noconfirm "${need[@]}"; ok "installed ${need[*]}"; fi

# ---------------------------------------------------------------- tailscale daemon + login
say "Tailscale"
if ! systemctl is-active --quiet tailscaled; then
  sudo systemctl enable --now tailscaled; ok "tailscaled enabled"
else ok "tailscaled running"; fi
for i in {1..20}; do tailscale status --json &>/dev/null && break; sleep 0.5; done

state=$(tailscale status --json 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin).get('BackendState',''))" || true)
if [ "$state" != "Running" ]; then
  echo "    Log in with the same account you'll use on your phone (a URL will appear):"
  sudo tailscale up --operator="$USER"
else
  ok "logged in"
  sudo tailscale set --operator="$USER"
fi
ok "operator set to $USER"

# ---------------------------------------------------------------- agent
say "Starting the agent"
~/.local/bin/cua start

# ---------------------------------------------------------------- private HTTPS for the phone
say "Publishing the app inside your tailnet"
if tailscale serve status 2>/dev/null | grep -q "127.0.0.1:8765"; then
  ok "already served"
else
  echo "    (if it prints a link to enable HTTPS certificates, open it, click enable, then re-run this script)"
  tailscale serve --bg 8765
fi
tailscale serve status | sed 's/^/    /'

# ---------------------------------------------------------------- done
say "Phone: install the Tailscale app, log in with the same account, then open this link"
~/.local/bin/cua-link
cat <<'EOF'

    Then in the phone browser menu: "Add to Home screen" / "Install app".
    PC chat: run `cua`.  Free the GPU when done: `cua stop`.
EOF
