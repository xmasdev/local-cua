# local-cua

A computer-use agent that runs entirely on your own PC. You tell it what to do, in a terminal or from
your phone, and it looks at the screen and clicks, types and scrolls until the job is done.

- **Fully local.** The model runs on your GPU through llama.cpp. Screenshots never leave the machine.
- **Fits a laptop GPU.** Built and tuned on an RTX 4050 with 6 GB of VRAM.
- **Live and interruptible.** One ongoing conversation that remembers earlier tasks. Send corrections while
  it works, answer its questions, or press Stop. You see what it is writing as it writes it.
- **Phone app.** Watch the PC screen live and chat with the agent from anywhere, privately over Tailscale.

Built for Arch Linux with the Hyprland compositor (Wayland). Other Wayland compositors need small changes
to the input and workspace code.

```
you:   write a mail to a friend inviting them for dinner today at 8pm at Hellhound cafe.
       don't send it, I'll review it. do it on a new desktop, this one is cluttered.

agent: switch_workspace 2 > Super+W (Firefox) > gmail.com > Compose > fills To, Subject, Body
       "The draft is ready for your review. I did not send it."
```

## How it works

```
 screenshot (grim, 1920x1080)
        |
        v
 Holo3.1-4B  <-- system prompt (desktop facts, installed apps, rules), the conversation,
   thinks, then picks ONE action          the last 1 to 3 screenshots, a workspace status line
   e.g. click(element="Subject field", x=668, y=507)        (coordinates are 0 to 1000)
        |
        v
 zoom pass (clicks only): crop the area around the point, ask the model again for a precise spot
        |
        v
 safety checks: never touch its own chat windows; "Send", "Delete", "Pay", Ctrl+Enter need your OK
        |
        v
 hyprctl moves the cursor, ydotool clicks / types / presses keys
        |
        v
 wait until the screen stops changing, then the next step
```

- **Model:** [Holo3.1-4B](https://huggingface.co/Hcompany/Holo-3.1-4B) by H Company, a Qwen3.5 model
  trained for computer use, as a Q5_K_M GGUF with its vision projector. It plans, picks the action and
  locates the target on screen. There is no separate planner: the model reasons before every step, and only
  the chosen actions are kept in the history.
- **Server:** llama.cpp in router mode, so the same server also hosts a general chat model
  (Qwen3.5-9B) without loading both at once.
- **Input:** `hyprctl` places the cursor on an exact pixel and `ydotool` acts as a virtual mouse and keyboard
  through `/dev/uinput`.
- **Service:** `service.py` holds the conversation, runs the agent loop in a background thread and serves a
  small web app (Server-Sent Events for the chat, MJPEG for the live screen). Python standard library only,
  plus Pillow.

## Benchmarks

Click accuracy on 248 real screenshots: 148 from
[ScreenSpot-Pro](https://huggingface.co/datasets/likaixin/ScreenSpot-Pro) (dense professional apps such as
IDEs, Office and Linux desktops) and 100 from
[ScreenSpot-v2](https://huggingface.co/datasets/OS-Copilot/ScreenSpot-v2) (desktop). Every screenshot is
scaled to 1920x1080 first, to match a real laptop screen. A hit means the predicted click lands inside the
target element. Measured on an RTX 4050 Laptop GPU (6 GB).

| Setup | ScreenSpot-Pro | ScreenSpot-v2 | Time per click |
|---|---|---|---|
| Holo3.1-4B Q5_K_M + zoom pass, full resolution | 69.6% | | 3.8 s |
| **Holo3.1-4B Q5_K_M + zoom pass, 1280 image tokens** (default) | **67.6%** | | **2.5 s** |
| Holo3.1-4B Q4_K_M + zoom pass, 1280 image tokens | 66.9% | | 2.4 s |
| Holo3.1-4B Q4_K_M + zoom pass, full resolution | 65.5% | | 3.5 s |
| Holo3.1-9B IQ4_XS + zoom pass | 68.9% | | 8.9 s |
| Holo3.1-9B IQ4_XS | 61.5% | 96.0% | 5.5 s |
| Holo3.1-4B Q5_K_M | 59.5% | 98.0% | 2.4 s |
| Qwen3.5-9B IQ4_XS (general model, baseline) | 50.0% | | 6.3 s |

What this showed:

- The 9B model does not fit fully in 6 GB, so it is twice as slow and no more accurate once the zoom pass is on.
- The zoom pass adds about 10 points on hard screens for about 1.4 s per click.
- 1280 image tokens per screenshot instead of about 2100 makes grounding 34% faster for 2 points, so that is
  the default. Q4 instead of Q5 is barely faster and less accurate.

### Speed of the agent loop

Measured by replaying a recorded 15-step task (`bench/replay.py`), model time per step:

| Change | Quick step | Thinking step |
|---|---|---|
| Start (full history re-read every step) | | 7 to 8.5 s |
| Zoom pass on its own slot, screenshots evicted in batches | | 4.8 s |
| Adaptive thinking (only when it matters) | 3.4 s | 4.3 s |
| At most one sentence before each action | 3.05 s | |
| 1280 image tokens | **2.33 s** | **3.48 s** |

What else was tried:

- Flash attention and CUDA graphs were already on. Bigger batches (`-ub 2048`) and a native rebuild gave nothing.
- Speculative decoding with Holo3.1-0.8B as the draft model was slower (34 to 38 tok/s vs 46): llama.cpp cannot
  pass image embeddings to the draft, so it guesses blind. `ngram-mod` speculation is on (about 8% faster, no VRAM).
- Outside the model: screenshots as PPM (118 to 23 ms), PNG level 1 (75 to 31 ms), settle detection with tiny
  scaled grabs (790 to 360 ms), and waiting for the screen to change after app shortcuts instead of 1.5 s.

Fewer steps help as much as faster steps: `type_into` clicks a field and types in one action, and a quick step
that tries to finish is re-checked with thinking on so tasks are not declared done too early.

Reproduce: `python3 bench/prepare_data.py`, start a model with `bench/serve.sh`, then
`python3 bench/bench.py NAME --maxside 1920 [--zoom] [--only pro]`. Raw results are in `bench/results/`.

## Requirements

- Arch Linux with Hyprland 0.57 or newer (Lua config; the cursor and workspace calls use `hl.dsp.*`)
- An NVIDIA GPU with 6 GB of VRAM or more, and llama.cpp built with CUDA
- `grim`, `ydotool`, `python`, `python-pillow`, `curl`
- For the phone app: Tailscale on the PC and on the phone

## Install

```bash
git clone https://github.com/xmasdev/local-cua ~/cua
cd ~/cua
./install.sh --models
```

`install.sh` links the `cua` commands into `~/.local/bin`, links the systemd user units, gives your user
access to `/dev/uinput` (it asks for sudo once) and, with `--models`, downloads the models into
`~/models/llama.cpp`. If `llama-server` is not on your PATH, set `LLAMA_SERVER=/path/to/llama-server`.

## Usage

```bash
cua start      # start the model server, the agent service and the virtual input device
cua            # chat with the agent in this terminal (/stop, /new, /quit)
cua status     # what is running, and how much GPU memory is in use
cua stop       # stop everything and free the GPU
```

Nothing starts at login. The model only loads into the GPU on the first task.

Things you can do in the chat:

- Give it a task: `open thunar on an empty workspace and go to Downloads`
- Correct it while it works: `actually, use the Documents folder`
- Answer its questions. When it wants to click something risky, like Send, it asks first and your
  next message (`y` or `n`) is the answer.
- Ask about earlier work: `what did you just do?`

There is also a one-shot mode without the service: `cua-once "task"`.

## Phone app

The agent service serves a small web app that you can install on your phone's home screen. It shows the PC
screen live, with a red marker wherever the agent clicks, the chat, Allow and Don't buttons for
confirmations, and a Stop button.

```bash
scripts/phone-setup.sh
```

This installs Tailscale, logs the PC in, starts the agent and publishes the app at
`https://<pc-name>.<your-tailnet>.ts.net`. That address is only reachable by devices in your own Tailscale
network. The first time, Tailscale may ask you to enable HTTPS for your network; open the link it prints,
enable it and run the script again.

On the phone: install the Tailscale app, log in with the same account, scan the QR code that `cua-link`
prints, then use "Add to Home screen" in the browser menu.

The live screen uses about 150 to 250 MB per hour while visible. It pauses when the app is in the background.

## Safety

- The agent never types into, clicks on or sends keys to the terminal running it or any window whose title
  starts with "CUA" (the chat windows). This also stops it from cancelling itself with Escape.
- Clicks on elements named like Send, Delete, Remove, Pay, Buy, Submit or Publish, and Ctrl+Enter, need your
  approval. If nobody can answer, the action is refused.
- It is told never to type passwords or payment details and to ask you instead.
- The web app needs a random token (`~/cua/token`) on top of Tailscale. To revoke every signed-in device:
  `rm ~/cua/token && systemctl --user restart cua`.
- The service listens on 127.0.0.1 only. Do not expose it to the internet: whoever can use it controls
  your PC.

This is still a 4B model operating a real desktop. Keep a habit of "prepare it, don't send it" for anything
that matters, and watch it the first few times.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `CUA_MODEL` | `Holo-3.1-4B` | Model name in the router |
| `CUA_BASE_URL` | `http://127.0.0.1:8080/v1` | OpenAI-compatible endpoint |
| `CUA_MODELS_DIR` | `~/models/llama.cpp` | Where the router looks for GGUF models |
| `LLAMA_SERVER` | `llama-server` on PATH | Path to the llama.cpp server binary |
| `CUA_THINK_MODE` | `adaptive` | `adaptive`, `always` or `never` |
| `CUA_THINK_BUDGET` | `256` | Max thinking tokens per step |
| `CUA_GLIDE` | `1` | Animate cursor moves instead of teleporting |
| `CUA_MAX_STEPS` | `40` | Step limit per task in the service |
| `CUA_PORT` | `8765` | Web app port (localhost only) |

Model settings (context size, image tokens, slots) are in `config/models.ini`. The desktop facts the
agent is told about (screen size, shortcuts, launcher) are in `SYSTEM_PROMPT` at the top of `agent.py`.
Adjust them to your own setup.

## Limitations

- Simple and medium tasks work well. Long multi-step tasks can wander, and it sometimes takes a roundabout path.
- Tiny icons in dense apps are where it misses most, about one in three even with the zoom pass.
- The PC has to be awake and unlocked.
- Typing uses a US keyboard layout.
- One monitor is assumed (the focused one).

## Project layout

```
agent.py           agent loop, tools, screen capture, input, safety guards, one-shot CLI
service.py         persistent session, interruptible worker, web server (SSE, MJPEG, token auth)
client.py          terminal chat client
web/               phone/web app (installable PWA)
bin/               cua, cua-link, cua-once
systemd/           cua.service, llama-router.service
config/models.ini  llama.cpp router presets
serve.sh           starts the llama.cpp router
install.sh         installer
scripts/           phone-setup.sh (Tailscale)
bench/             benchmark scripts and results
```

## Credits

- [Holo3.1](https://huggingface.co/Hcompany/Holo-3.1-4B) by H Company. The agent loop follows their
  published conventions (function calling, `<observation>` screenshots, coordinates in 0 to 1000).
- [llama.cpp](https://github.com/ggml-org/llama.cpp)
- [ScreenSpot-Pro](https://github.com/likaixin2000/ScreenSpot-Pro-GUI-Grounding) and
  [ScreenSpot-v2](https://huggingface.co/datasets/OS-Copilot/ScreenSpot-v2) for the benchmark data
- GGUF conversions by [mradermacher](https://huggingface.co/mradermacher) and [unsloth](https://huggingface.co/unsloth)

## License

MIT
