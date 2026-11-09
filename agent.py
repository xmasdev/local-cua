#!/usr/bin/env python3
"""Local computer-use agent for Hyprland (Wayland), driven by Holo3.1 on llama-server.

  agent.py "open firefox and search for arch wiki hyprland"
  agent.py --confirm "..."     ask before every action (default for risky actions anyway)
  agent.py --dry-run "..."     plan + print actions, never touch mouse/keyboard

Kill switch: SUPER+SHIFT+Escape (Hyprland bind -> pkill -f cua/agent.py), or Ctrl+C here.
Loop format follows H Company's docs (function calling, <observation> screenshots, last 3 images,
'answer' tool ends the run, thinking on / temp 0.8 for steps, coords in [0,1000]).
"""
import argparse, base64, io, json, os, re, shutil, subprocess, sys, time, urllib.request
from PIL import Image

BASE = os.environ.get("CUA_BASE_URL", "http://127.0.0.1:8080/v1")
MODEL = os.environ.get("CUA_MODEL", "Holo-3.1-4B")

SYSTEM_PROMPT = """You are a computer-use agent operating the user's Linux desktop (Arch Linux, Hyprland tiling window manager, Caelestia shell, single 1920x1080 screen).
Each turn you receive a screenshot inside <observation> tags. Decide the single next action and call exactly one tool.
Think briefly: in one to three short sentences, note what changed on screen and what to do next, then act.
Outside of thinking, write at most one short sentence before the tool call.
Useful facts about this desktop:
- App launcher: press the Super key alone (key "super"), type the app name, press Enter.
- Shortcuts: Super+T terminal (foot), Super+W browser (Firefox), Super+E files (Thunar), Super+C editor (VSCodium), Super+Q close window, Super+F fullscreen.
- Workspaces ("desktops"): use the switch_workspace tool (or Super+1..9). Switching to a workspace that has no windows gives a fresh, empty desktop; apps opened there stay there. After each screenshot you get a text line listing which workspaces have windows.
- Windows tile automatically; new windows split the current workspace.
- Installed apps (this is the complete list): {apps}.
- If none of the installed apps fits the task, use the relevant website in Firefox instead of searching for an app.
Rules:
- Work step by step, verify the result of each action on the next screenshot, and recover if something went wrong.
- To fill a text field, use type_into (click + type in one step) instead of click followed by write.
- To read or extract information from a page or document (lists, names, numbers, articles), call read_text once
  and answer from the text instead of scrolling back and forth. Scroll only to reach something you need to click.
- After every action the screen is already given time to settle before you see it. Only call wait when a page or app is visibly still loading.
- Fields often reformat what you type: e.g. an email address becomes a chip showing the contact's saved name, a URL gets shortened. That means it worked; do not type it again. Once a field is filled, move on to the next one.
- Be autonomous: choose the obvious app, website or method yourself (the user expects you to work it out, like a capable human assistant would). Do not ask the user about choices you can reasonably make.
- Use ask_user only for information you cannot find or infer, or to confirm an irreversible step (sending messages/emails, deleting, purchasing, submitting forms, changing system settings).
- If the user tells you not to do a step (e.g. "don't send it"), do everything up to that step, leave it ready, and finish with answer.
- Never enter passwords or payment details; call ask_user instead.
- When the task is complete (or impossible), call answer with a short report."""


def T(name, desc, props, req):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": req}}}

EL = {"type": "string", "description": "Detailed description of the target UI element"}
X = {"type": "integer", "description": "X coordinate as integer in [0, 1000]"}
Y = {"type": "integer", "description": "Y coordinate as integer in [0, 1000]"}
TOOLS = [
    T("click", "Left-click at (x, y) coordinates", {"element": EL, "x": X, "y": Y}, ["element", "x", "y"]),
    T("double_click", "Double-click at (x, y) coordinates", {"element": EL, "x": X, "y": Y}, ["element", "x", "y"]),
    T("right_click", "Right-click at (x, y) coordinates", {"element": EL, "x": X, "y": Y}, ["element", "x", "y"]),
    T("type_into", "Click a text field at (x, y) and type text into it in one step (use this instead of a click followed by write)",
      {"element": EL, "x": X, "y": Y, "content": {"type": "string", "description": "Text to type"},
       "press_enter": {"type": "boolean", "description": "Press Enter after typing"}}, ["element", "x", "y", "content"]),
    T("write", "Type text into the focused element. Set press_enter to submit afterwards.",
      {"content": {"type": "string", "description": "Text to type"},
       "press_enter": {"type": "boolean", "description": "Press Enter after typing"}}, ["content"]),
    T("key", "Press a key or key combination, e.g. 'enter', 'escape', 'ctrl+l', 'ctrl+shift+t', 'super', 'super+t', 'alt+tab'",
      {"keys": {"type": "string", "description": "Keys joined with '+'"}}, ["keys"]),
    T("scroll", "Scroll with the mouse wheel over (x, y)",
      {"element": EL, "x": X, "y": Y, "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
       "amount": {"type": "integer", "description": "Number of wheel notches (1-15)"}}, ["x", "y", "direction"]),
    T("drag", "Drag from (x1, y1) to (x2, y2) holding the left button",
      {"x1": X, "y1": Y, "x2": X, "y2": Y}, ["x1", "y1", "x2", "y2"]),
    T("switch_workspace", "Switch to Hyprland workspace (desktop) number N. Use an empty one for a fresh desktop.",
      {"number": {"type": "integer", "description": "Workspace number 1-9"}}, ["number"]),
    T("read_text", "Read all the text of the focused window (web page, document, editor) in one step and get the "
      "answer to your question from it. Use it to read or search pages instead of scrolling through them. "
      "Click an empty part of the page first if a text field has focus.",
      {"question": {"type": "string", "description": "What you need from the text, as a complete question "
                    "(e.g. 'What are the names of all the pathways?')"}}, ["question"]),
    T("wait", "Wait for the screen to update (page loads, app start)",
      {"seconds": {"type": "number", "description": "Seconds to wait (max 10)"}}, ["seconds"]),
    T("ask_user", "Ask the user a question or for confirmation; returns their reply",
      {"question": {"type": "string"}}, ["question"]),
    T("answer", "Provide a final answer / report and end the task",
      {"content": {"type": "string", "description": "The answer content"}}, ["content"]),
]
RISKY_KEYS = re.compile(r"^(ctrl|control|cmd|super|meta)\+(enter|return)$", re.I)
RISKY = re.compile(r"\b(send|delete|remove|purchase|buy|pay|submit|confirm|format|erase|uninstall|shut ?down|reboot|log ?out|post|publish)\b", re.I)

# ---------------------------------------------------------------- screen + input (Hyprland / Wayland)
KEYCODES = {  # linux/input-event-codes.h
    "esc": 1, "escape": 1, "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9, "9": 10, "0": 11,
    "minus": 12, "-": 12, "equal": 13, "=": 13, "backspace": 14, "tab": 15, "q": 16, "w": 17, "e": 18, "r": 19,
    "t": 20, "y": 21, "u": 22, "i": 23, "o": 24, "p": 25, "[": 26, "]": 27, "enter": 28, "return": 28,
    "ctrl": 29, "control": 29, "a": 30, "s": 31, "d": 32, "f": 33, "g": 34, "h": 35, "j": 36, "k": 37, "l": 38,
    ";": 39, "'": 40, "`": 41, "shift": 42, "\\": 43, "z": 44, "x": 45, "c": 46, "v": 47, "b": 48, "n": 49,
    "m": 50, ",": 51, ".": 52, "/": 53, "alt": 56, "space": 57, "capslock": 58,
    "f1": 59, "f2": 60, "f3": 61, "f4": 62, "f5": 63, "f6": 64, "f7": 65, "f8": 66, "f9": 67, "f10": 68,
    "f11": 87, "f12": 88, "home": 102, "up": 103, "pageup": 104, "page_up": 104, "left": 105, "right": 106,
    "end": 107, "down": 108, "pagedown": 109, "page_down": 109, "insert": 110, "delete": 111, "del": 111,
    "super": 125, "meta": 125, "win": 125, "cmd": 125, "altgr": 100, "menu": 127, "printscreen": 99, "print": 99,
}


class Desktop:
    def __init__(self, dry=False):
        self.dry = dry
        mon = next(m for m in json.loads(subprocess.check_output(["hyprctl", "monitors", "-j"])) if m["focused"])
        self.mon = mon["name"]; self.ox, self.oy = mon["x"], mon["y"]
        self.scale = mon["scale"]
        self.own_pids = set()
        p = os.getpid()
        while p > 1:  # our ancestors: python <- shell <- terminal emulator
            self.own_pids.add(p)
            try: p = int(open(f"/proc/{p}/stat").read().rsplit(")", 1)[1].split()[1])
            except Exception: break

    PROTECTED_TITLE = re.compile(r"^CUA\b")  # the agent's own chat windows ("CUA chat", web UI "CUA")

    def protected_windows(self):
        """Windows the agent must never send input to: the terminal running it, and its chat windows."""
        return [c for c in json.loads(subprocess.check_output(["hyprctl", "clients", "-j"]))
                if c["pid"] in self.own_pids or self.PROTECTED_TITLE.search(c.get("title", ""))]

    def own_window(self):
        p = self.protected_windows(); return p[0] if p else None

    def guard_keyboard(self):
        act = json.loads(subprocess.check_output(["hyprctl", "activewindow", "-j"]) or b"{}")
        if any(act.get("address") == w["address"] for w in self.protected_windows()):
            raise PermissionError("blocked: keyboard focus is on your own control window (the terminal/chat that runs "
                                  "you). Never type there. Switch to another (empty) workspace or click another window first.")

    def guard_click(self, x, y):
        ws = json.loads(subprocess.check_output(["hyprctl", "activeworkspace", "-j"]))["id"]
        px, py = self.to_px(x, y)
        for w in self.protected_windows():
            (wx, wy), (ww, wh) = w["at"], w["size"]
            if w["workspace"]["id"] == ws and wx <= px <= wx + ww and wy <= py <= wy + wh:
                raise PermissionError("blocked: that point is inside your own control window (the terminal/chat that "
                                      "runs you). Do not interact with it. Use another window or an empty workspace.")

    def switch_workspace(self, n):
        self._run("hyprctl", "dispatch", f"hl.dsp.focus({{ workspace = {int(n)} }})")

    def screenshot(self):
        raw = subprocess.check_output(["grim", "-o", self.mon, "-t", "ppm", "-"])   # uncompressed: ~6x faster than PNG
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        # grim captures device pixels; hyprctl movecursor wants logical pixels
        self.w, self.h = img.size
        return img

    def to_px(self, x, y):  # [0,1000] -> global logical coords
        lx = x / 1000 * self.w / self.scale + self.ox
        ly = y / 1000 * self.h / self.scale + self.oy
        return round(lx), round(ly)

    def _run(self, *cmd):
        if self.dry: return
        subprocess.run(cmd, check=True, capture_output=True)

    def _warp(self, px, py):
        # Hyprland >=0.57 Lua config: dispatch takes a Lua expression
        self._run("hyprctl", "dispatch", f"hl.dsp.cursor.move({{ x = {px}, y = {py} }})")

    def glide(self, px, py, duration=0.12, steps=8):
        """Ease the cursor to (px, py) instead of teleporting, so people can follow what the agent does."""
        if self.dry: return
        try:
            cx, cy = (int(v) for v in subprocess.check_output(["hyprctl", "cursorpos"]).decode().split(","))
        except Exception:
            return
        for i in range(1, steps):
            t = i / steps; e = 1 - (1 - t) ** 3                         # ease-out cubic
            self._warp(round(cx + (px - cx) * e), round(cy + (py - cy) * e)); time.sleep(duration / steps)

    def move(self, x, y):
        px, py = self.to_px(x, y)
        if GLIDE: self.glide(px, py)
        self._warp(px, py)
        # movecursor warps without a motion event, so Hyprland keeps the old pointer focus;
        # a real 1px wiggle through the virtual device makes the window under the cursor take it.
        self._run("ydotool", "mousemove", "-x", "1", "-y", "0"); self._run("ydotool", "mousemove", "-x", "-1", "-y", "0")
        self._warp(px, py); time.sleep(0.08)

    def click(self, x, y, button="left", n=1):
        self.move(x, y)
        code = {"left": "0xC0", "right": "0xC1", "middle": "0xC2"}[button]
        self._run("ydotool", "click", "--repeat", str(n), "--next-delay", "60", code)

    def write(self, text):
        self._run("ydotool", "type", "--key-delay", "8", "--", text)

    def key(self, combo):
        names = [k.strip().lower() for k in combo.replace(" ", "").split("+") if k.strip()]
        codes = []
        for k in names:
            if k not in KEYCODES: raise ValueError(f"unknown key {k!r}")
            codes.append(KEYCODES[k])
        seq = [f"{c}:1" for c in codes] + [f"{c}:0" for c in reversed(codes)]
        self._run("ydotool", "key", *seq)

    def read_text(self, limit=80000):
        """Ctrl+A, Ctrl+C, read the clipboard, then put the user's clipboard back and drop the copied
        page text from cliphist. (Firefox does not publish Ctrl+A as the primary selection.)"""
        if self.dry: return "(dry run)"
        def run(*cmd, inp=None):
            return subprocess.run(cmd, input=inp, capture_output=True, timeout=3)
        saved = None
        try:   # keep whatever the user had on the clipboard (text or image)
            types = run("wl-paste", "--list-types").stdout.decode().split()
            t = next((x for x in types if x.startswith("text/plain")), types[0] if types else None)
            if t: saved = (t, run("wl-paste", "--no-newline", "--type", t).stdout)
        except Exception:
            pass
        self.key("ctrl+a"); time.sleep(0.15); self.key("ctrl+c"); time.sleep(0.35)
        txt = run("wl-paste", "--no-newline", "--type", "text/plain").stdout.decode("utf-8", "replace")
        if saved and txt and txt.encode() == saved[1]: txt = ""        # nothing new was copied
        try:   # remove the page text from clipboard history, then restore the clipboard
            if txt and shutil.which("cliphist"):
                time.sleep(0.2)
                head = re.sub(r"\s+", " ", txt[:40]).strip()
                for line in run("cliphist", "list").stdout.decode("utf-8", "replace").splitlines()[:5]:
                    if line.split("\t", 1)[-1].startswith(head[:30]):
                        run("cliphist", "delete", inp=(line + "\n").encode()); break
            if saved: run("wl-copy", "--type", saved[0], inp=saved[1])
            else: run("wl-copy", "--clear")
        except Exception:
            pass
        txt = re.sub(r"[ \t]+", " ", txt); txt = re.sub(r"\n\s*\n+", "\n", txt).strip()
        if len(txt) < 40:
            return ("Got almost no text. The window may not support select-all, or a text field has focus: "
                    "click once on plain text in the page (not a link or field), then call read_text again. "
                    "If it fails twice, read the screen instead.")
        more = len(txt) > limit
        return f"Text of the focused window ({len(txt)} chars{', truncated' if more else ''}):\n" + txt[:limit]

    def scroll(self, x, y, direction, amount):
        self.move(x, y)
        dx, dy = {"up": (0, 1), "down": (0, -1), "left": (-1, 0), "right": (1, 0)}[direction]
        for _ in range(max(1, min(int(amount), 15))):
            self._run("ydotool", "mousemove", "--wheel", "-x", str(dx), "-y", str(dy)); time.sleep(0.03)

    def drag(self, x1, y1, x2, y2):
        self.move(x1, y1); self._run("ydotool", "click", "0x40"); time.sleep(0.1)
        self.move((x1 + x2) / 2, (y1 + y2) / 2); time.sleep(0.05)
        self.move(x2, y2); time.sleep(0.1); self._run("ydotool", "click", "0x80")


# ---------------------------------------------------------------- model
SLOTS = os.environ.get("CUA_SLOTS", "1") == "1"   # pin agent loop / zoom pass to separate server slots
TIMING = os.environ.get("CUA_TIMING") == "1"
THINK_BUDGET = int(os.environ.get("CUA_THINK_BUDGET", "256"))
THINK_MODE = os.environ.get("CUA_THINK_MODE", "adaptive")
GLIDE = os.environ.get("CUA_GLIDE", "1") == "1"   # animate cursor moves (~0.12 s)
READER = os.environ.get("CUA_READER", "Qwen3.5-9B-reader")   # text model for long read_text results ("" = off)
READ_DIRECT_MAX = 4000      # shorter texts go straight to the agent model
on_status = None            # set by the service: on_status("text") shows progress in the UIs
current_request = ""        # the user's current request, so the reader answers what the user actually asked   # adaptive | always | never
TROUBLE = re.compile(r"error|NOTE:|declined|blocked|Not executed|not executed", re.I)


def thumb_of(img):
    return img.resize((240, 135)).convert("L").tobytes()


def changed(a, b):
    return a is None or b is None or sum(abs(x - y) > 12 for x, y in zip(a, b)) >= 30


def should_think(step, last_result, screen_changed, user_spoke):
    """Thinking costs ~1.5-2 s per step. Use it where it matters: planning the task, after the user
    said something, after errors/refusals/loop warnings, or when the last action changed nothing."""
    if THINK_MODE == "always": return True
    if THINK_MODE == "never": return False
    return step == 1 or user_spoke or not screen_changed or bool(TROUBLE.search(last_result or ""))


def chat(messages, tools=None, temperature=0.8, thinking=True, max_tokens=2048, schema=None, slot=None):
    body = {"model": MODEL, "messages": messages, "temperature": temperature, "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": thinking}}
    if thinking: body["reasoning_budget_tokens"] = THINK_BUDGET   # normal steps think 70-260 tokens; stop rambling
    if tools: body.update(tools=tools, tool_choice="required")
    if schema: body["response_format"] = {"type": "json_schema", "json_schema": {"schema": schema}}
    if SLOTS and slot is not None: body["id_slot"] = slot
    req = urllib.request.Request(BASE + "/chat/completions", json.dumps(body).encode(), {"Content-Type": "application/json"})
    t0 = time.time()
    r = json.load(urllib.request.urlopen(req, timeout=900))
    if TIMING:
        t = r.get("timings", {})
        print(f"    ⏱ {'zoom ' if schema else 'step '} total {time.time()-t0:5.2f}s | prompt {t.get('prompt_n')} tok "
              f"{t.get('prompt_ms', 0)/1000:.2f}s (cached {t.get('cache_n')}) | gen {t.get('predicted_n')} tok "
              f"{t.get('predicted_ms', 0)/1000:.2f}s", file=sys.stderr)
    return r["choices"][0]["message"]


def chat_stream(messages, tools=None, temperature=0.8, thinking=True, max_tokens=1200, slot=None, on_text=None):
    """Like chat(), but streams: on_text(text_so_far) is called as reasoning/content tokens arrive.
    Returns the same message shape as chat()."""
    body = {"model": MODEL, "messages": messages, "temperature": temperature, "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": thinking}, "stream": True}
    if thinking: body["reasoning_budget_tokens"] = THINK_BUDGET
    if tools: body.update(tools=tools, tool_choice="required")
    if SLOTS and slot is not None: body["id_slot"] = slot
    req = urllib.request.Request(BASE + "/chat/completions", json.dumps(body).encode(), {"Content-Type": "application/json"})
    text, reasoning, calls, last_cb = "", "", {}, 0.0
    with urllib.request.urlopen(req, timeout=900) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data: ") or line == "data: [DONE]": continue
            ch = json.loads(line[6:])
            if not ch.get("choices"): continue
            d = ch["choices"][0].get("delta") or {}
            reasoning += d.get("reasoning_content") or ""
            text += d.get("content") or ""
            for tc in d.get("tool_calls") or []:
                c = calls.setdefault(tc.get("index", 0), {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if tc.get("id"): c["id"] = tc["id"]
                f = tc.get("function") or {}
                c["function"]["name"] += f.get("name") or ""
                c["function"]["arguments"] += f.get("arguments") or ""
            if on_text and time.time() - last_cb > 0.12:
                last_cb = time.time(); on_text((reasoning + "\n" + text).strip())
    if on_text: on_text((reasoning + "\n" + text).strip())
    out = {"content": text, "reasoning_content": reasoning}
    if calls: out["tool_calls"] = [calls[k] for k in sorted(calls)]
    return out


def png_b64(img):
    buf = io.BytesIO(); img.save(buf, "PNG", compress_level=1); return base64.b64encode(buf.getvalue()).decode()


def installed_apps():
    import configparser, glob
    names = set()
    for d in ["/usr/share/applications", os.path.expanduser("~/.local/share/applications"),
              "/var/lib/flatpak/exports/share/applications"]:
        for f in glob.glob(d + "/*.desktop"):
            c = configparser.ConfigParser(interpolation=None, strict=False)
            try: c.read(f, encoding="utf-8")
            except Exception: continue
            e = c["Desktop Entry"] if "Desktop Entry" in c else None
            if not e or e.get("Type") != "Application" or e.get("NoDisplay", "").lower() == "true" \
                    or e.get("Hidden", "").lower() == "true":
                continue
            names.add(e.get("Name"))
    return ", ".join(sorted(n for n in names if n))


def signature(name, a):
    """Coarse identity of an action, for loop detection."""
    if name in ("click", "double_click", "right_click", "type_into"):
        return (name, round(a.get("x", 0) / 25), round(a.get("y", 0) / 25))
    if name in ("write", "scroll", "wait"): return (name,)
    return (name, json.dumps(a, sort_keys=True))


def read_with_reader(text, question, max_chars=60000):
    """Long page text: let a stronger text model answer the agent's question from it. The GPU holds one model
    at a time, so this swaps the reader in (the agent model reloads on its next step). Only used here."""
    body = text.split("\n", 1)[1] if "\n" in text else text
    if on_status: on_status(f"Reading {len(body):,} characters with {READER}…")
    req = {"model": READER, "temperature": 0.0, "max_tokens": 1000, "chat_template_kwargs": {"enable_thinking": False},
           "messages": [{"role": "system", "content": "You extract information from page text for a computer-use agent. "
                         "Answer the question using only the text. Be complete and accurate but brief: give the direct answer, "
                         "list names only (no descriptions), at most about 120 words. List items exactly as named in the text. "
                         "If the text does not contain the answer, say so and say what the page is about."},
                        {"role": "user", "content": (
                            f"Question (from the user): {current_request}\n(The agent is looking for: {question})\n"
                            if current_request else f"Question: {question}\n") + f"\nPage text:\n{body[:max_chars]}"}]}
    t0 = time.time()
    try:
        r = json.load(urllib.request.urlopen(urllib.request.Request(
            BASE + "/chat/completions", json.dumps(req).encode(), {"Content-Type": "application/json"}), timeout=600))
        ans = r["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"(the reading model failed: {e})\n" + text[:20000]
    if on_status: on_status(f"Read the page in {time.time() - t0:.0f} s.")
    return (f"Answer from the page text ({len(body):,} chars, read by {READER}):\n{ans}\n"
            "This answer is shown to the user automatically together with yours. If it answers the request, call answer "
            "with only one short sentence (do not repeat the list or details). You do not need to read the page again.")


def loop_advice(names):
    """What to tell the model when it repeats itself, based on what it keeps doing."""
    if names.count("scroll") >= 4:
        return ("NOTE: you keep scrolling back and forth. If you are trying to read or find information, call "
                "read_text now to get the whole text at once, then answer from it.")
    return ("NOTE: you have repeated similar actions several times without making progress. Step back, think about "
            "a completely different way to accomplish the task, and do that instead.")


def desktop_state():
    """One line of Hyprland state the screenshot can't show reliably (which workspaces are occupied)."""
    try:
        clients = json.loads(subprocess.check_output(["hyprctl", "clients", "-j"]))
        active = json.loads(subprocess.check_output(["hyprctl", "activeworkspace", "-j"]))["id"]
    except Exception:
        return ""
    occ = {}
    for c in clients:
        w = c["workspace"]["id"]
        if w > 0: occ.setdefault(w, []).append(c["class"])
    used = "; ".join(f"{w}: {', '.join(v)}" for w, v in sorted(occ.items()))
    empty = [str(i) for i in range(1, 10) if i not in occ]
    return f"Desktop: you are on workspace {active}. Workspaces with windows -> {used or 'none'}. Empty workspaces: {', '.join(empty)}."


def observation(img):
    return {"role": "user", "content": [
        {"type": "text", "text": "<observation>\n"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + png_b64(img)}},
        {"type": "text", "text": "\n</observation>\n" + desktop_state()}]}


def trim_to_last_n_images(messages, n=3, keep=None):
    """Keep at most n screenshots (H's guidance). With keep=k, once the budget is exceeded drop down to
    the newest k at once: the history prefix then changes only every few steps and everything after the
    change point is cheap text, so llama.cpp's prompt cache stays useful (~2k new tokens per step)."""
    total = sum(1 for m in messages if m["role"] == "user" and isinstance(m["content"], list)
                for c in m["content"] if c.get("type") == "image_url")
    if total <= n: return
    if keep is not None: n = keep
    seen = 0
    for msg in reversed(messages):
        if msg["role"] != "user" or not isinstance(msg["content"], list): continue
        for chunk in msg["content"]:
            if chunk.get("type") != "image_url": continue
            seen += 1
            if seen > n:
                chunk["type"] = "text"; chunk["text"] = "[screenshot evicted]"; chunk.pop("image_url", None)


LOC_SCHEMA = {"type": "object", "properties": {"x": {"type": "integer", "minimum": 0, "maximum": 1000},
                                                "y": {"type": "integer", "minimum": 0, "maximum": 1000}}, "required": ["x", "y"]}

def refine(img, element, x, y):
    """Zoom pass: crop half the screen around the guess at native res and re-localize the element."""
    W, H = img.size; px, py = x / 1000 * W, y / 1000 * H
    cw, ch = W // 2, H // 2
    x0 = int(min(max(px - cw / 2, 0), W - cw)); y0 = int(min(max(py - ch / 2, 0), H - ch))
    crop = img.crop((x0, y0, x0 + cw, y0 + ch))
    prompt = ("Localize an element on the GUI image according to the provided target and output a click position.\n"
              f" * You must output a valid JSON following the format: {json.dumps(LOC_SCHEMA)}\n Your target is:\n{element}")
    msg = chat([{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + png_b64(crop)}},
                                             {"type": "text", "text": prompt}]}],
               temperature=0.0, thinking=False, max_tokens=64, schema=LOC_SCHEMA, slot=1)
    p = json.loads(msg["content"])
    nx = (x0 + p["x"] / 1000 * cw) / W * 1000; ny = (y0 + p["y"] / 1000 * ch) / H * 1000
    return round(nx), round(ny)


# ---------------------------------------------------------------- loop
def describe(name, a):
    if name in ("click", "double_click", "right_click"): return f"{name} '{a.get('element','')}' @({a['x']},{a['y']})"
    if name == "type_into": return (f"type_into '{a.get('element','')}' @({a['x']},{a['y']}): {a.get('content','')!r}"
                                    + (" + Enter" if a.get("press_enter") else ""))
    if name == "write": return f"write {a['content']!r}" + (" + Enter" if a.get("press_enter") else "")
    if name == "key": return f"key {a['keys']}"
    if name == "scroll": return f"scroll {a['direction']} x{a.get('amount',3)} @({a['x']},{a['y']})"
    return f"{name} {json.dumps(a)}"


def ask(prompt, unattended_reply):
    if not sys.stdin.isatty():
        print(prompt + f"[unattended -> {unattended_reply!r}]"); return unattended_reply
    try: return input(prompt)
    except EOFError: return unattended_reply


def run(task, max_steps, confirm, dry, use_refine, settle):
    logdir = os.path.expanduser(time.strftime("~/cua/runs/%Y%m%d-%H%M%S")); os.makedirs(logdir, exist_ok=True)
    log = open(os.path.join(logdir, "log.txt"), "w"); log.write(f"TASK: {task}\n")
    print(f"(run log: {logdir})", file=sys.stderr)
    d = Desktop(dry)
    messages = [{"role": "system", "content": SYSTEM_PROMPT.replace("{apps}", installed_apps())},
                {"role": "user", "content": task}]
    history, last_nudge, prev_thumb, last_result = [], -99, None, ""
    for step in range(1, max_steps + 1):
        img = d.screenshot(); img.save(os.path.join(logdir, f"{step:02d}.png"))
        th = thumb_of(img); think = should_think(step, last_result, changed(prev_thumb, th), False); prev_thumb = th
        messages.append(observation(img)); trim_to_last_n_images(messages, 3, keep=1)
        t0 = time.time()
        msg = chat(messages, tools=TOOLS, slot=0, thinking=think, max_tokens=1200)
        calls = msg.get("tool_calls") or []
        # history keeps only the parsed output, never the reasoning
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls or None})
        if msg.get("reasoning_content"):
            print(f"  · thought: {msg['reasoning_content'].strip()[-300:]}", file=sys.stderr)
        if not calls:
            print(f"[{step}] (no tool call, {time.time()-t0:.1f}s)"); continue
        for call in calls:
            name = call["function"]["name"]
            try: a = json.loads(call["function"]["arguments"] or "{}")
            except json.JSONDecodeError: a = {}
            if name in ("click", "double_click", "right_click") and use_refine and a.get("element"):
                a["x"], a["y"] = refine(img, a["element"], a["x"], a["y"])
            print(f"[{step}] {describe(name, a)}  ({time.time()-t0:.1f}s)")
            log.write(f"[{step}] {describe(name, a)}\n    thought: {(msg.get('reasoning_content') or '').strip()}\n"); log.flush()
            if name == "answer":
                print("\nDONE:", a.get("content", "")); return a.get("content", "")
            if name == "ask_user":
                result = ask(f"  ? {a.get('question')}\n  > ",
                             "The user is not available to answer. Make the most reasonable choice yourself and continue "
                             "the task. Do not perform irreversible actions (like sending); stop just before them.")
            else:
                # typing text is reversible; what matters is what gets clicked / which shortcut is pressed
                risky = (name in ("click", "double_click", "right_click") and RISKY.search(a.get("element", ""))) \
                    or (name == "key" and RISKY_KEYS.match(a.get("keys", "").replace(" ", "").lower()))
                if (confirm or risky) and not dry:
                    r = ask(f"  run this{' (looks risky)' if risky and not confirm else ''}? [Y/n/q/or type feedback] ",
                            "no - the user must do this step themselves").strip()
                    if r.lower() == "q": print("aborted"); return None
                    if r and r.lower() not in ("y", "yes"):
                        messages.append({"role": "tool", "tool_call_id": call["id"], "content": f"User declined: {r}"}); continue
                try:
                    result = execute(d, name, a)
                except Exception as e:
                    result = f"error: {e}"
            history.append(signature(name, a))
            recent = history[-8:]
            if step - last_nudge >= 4 and max(recent.count(x) for x in recent) >= 3:
                result = (f"{result}\nNOTE: you have repeated similar actions several times without making progress. "
                          "What you are looking for is probably not there. Step back, think about a completely "
                          "different way to accomplish the task, and do that instead.")
                last_nudge = step; print("  ↺ loop detected, nudging", file=sys.stderr); log.write("  LOOP NUDGE\n")
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": str(result)})
            last_result = str(result)
        time.sleep(settle)
        if not dry:
            wait_until_stable(d)
    print("Step budget exhausted"); return None


def wait_for_change(d, timeout=1.5, interval=0.1):
    """After launching something: return as soon as the screen starts changing (or after timeout)."""
    def thumb():
        raw = subprocess.check_output(["grim", "-o", d.mon, "-s", str(240 / d.w), "-t", "ppm", "-"])
        return Image.open(io.BytesIO(raw)).resize((240, 135)).convert("L").tobytes()
    first, t0 = thumb(), time.time()
    while time.time() - t0 < timeout:
        time.sleep(interval)
        if sum(abs(a - b) > 12 for a, b in zip(first, thumb())) >= 30: return


def wait_until_stable(d, timeout=4.0, interval=0.2):
    """Wait until two consecutive screenshots match (apps finished opening / pages loaded)."""
    def thumb():   # grim scales on capture: a 240x135 grab takes ~20 ms
        raw = subprocess.check_output(["grim", "-o", d.mon, "-s", str(240 / d.w), "-t", "ppm", "-"])
        return Image.open(io.BytesIO(raw)).resize((240, 135)).convert("L").tobytes()
    prev, t0 = thumb(), time.time()
    while time.time() - t0 < timeout:
        time.sleep(interval)
        cur = thumb()
        diff = sum(abs(a - b) > 12 for a, b in zip(prev, cur))
        if diff < 30:  # ignore blinking cursors / clocks
            return
        prev = cur


def execute(d, name, a):
    if not d.dry:
        if name in ("click", "double_click", "right_click", "scroll", "type_into"): d.guard_click(a["x"], a["y"])
        if name in ("write", "key", "read_text"): d.guard_keyboard()
    if name == "switch_workspace":
        d.switch_workspace(a["number"]); time.sleep(0.4)
        wins = [c["class"] for c in json.loads(subprocess.check_output(["hyprctl", "clients", "-j"]))
                if c["workspace"]["id"] == int(a["number"])]
        return (f"ok, now on workspace {a['number']}. "
                + (f"It is NOT empty, it already has: {', '.join(wins)}." if wins else "It is empty."))
    if name == "type_into":
        d.click(a["x"], a["y"]); time.sleep(0.12)
        d.write(a["content"])
        if a.get("press_enter"): d.key("enter")
        return "ok"
    if name == "click": d.click(a["x"], a["y"])
    elif name == "double_click": d.click(a["x"], a["y"], n=2)
    elif name == "right_click": d.click(a["x"], a["y"], "right")
    elif name == "write":
        d.write(a["content"])
        if a.get("press_enter"): d.key("enter")
    elif name == "key":
        d.key(a["keys"])
        if "super" in a["keys"].lower() and not d.dry: wait_for_change(d, 1.5)  # app launch / launcher
    elif name == "scroll": d.scroll(a["x"], a["y"], a["direction"], a.get("amount", 3))
    elif name == "drag": d.drag(a["x1"], a["y1"], a["x2"], a["y2"])
    elif name == "wait": time.sleep(min(float(a.get("seconds", 1)), 10))
    elif name == "read_text":
        text = d.read_text()
        if not text.startswith("Text of the focused window") or len(text) <= READ_DIRECT_MAX or not READER:
            return text
        return read_with_reader(text, a.get("question") or "Summarize the important information in this text.")
    else: return f"unknown tool {name}"
    return "ok"


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("task")
    ap.add_argument("--max-steps", type=int, default=30)
    ap.add_argument("--confirm", action="store_true", help="confirm every action")
    ap.add_argument("--dry-run", action="store_true", help="never send input")
    ap.add_argument("--refine", action=argparse.BooleanOptionalAction, default=False, help="zoom pass on clicks")
    ap.add_argument("--settle", type=float, default=0.15, help="seconds to wait after each action")
    a = ap.parse_args()
    try:
        run(a.task, a.max_steps, a.confirm, a.dry_run, a.refine, a.settle)
    except KeyboardInterrupt:
        print("\ninterrupted")
