#!/usr/bin/env python3
"""CUA service: a persistent, interruptible computer-use agent with a web/phone UI.

  python3 ~/cua/service.py            # or: systemctl --user start cua
  open http://127.0.0.1:8765/?token=<~/cua/token>   (phone: via `tailscale serve`, see README)

- One long-running conversation, saved to ~/cua/session.json (survives restarts).
- Send messages any time: while it works they are fed in before its next step;
  when it asks a question or wants confirmation, your next message is the answer.
- Stop halts it after the current action. /new starts a fresh session.
- Live screen as MJPEG; chat/events via Server-Sent Events. Stdlib only.
"""
import base64, hmac, http.server, json, os, queue, re, secrets, socketserver, subprocess, sys, threading, time, traceback
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("CUA_MODEL", "Holo-3.1-4B")
import agent  # noqa: E402  (Desktop, chat, tools, prompt, helpers)
agent.MODEL = os.environ["CUA_MODEL"]

HOME = os.path.expanduser("~/cua")
STATE_FILE = os.path.join(HOME, "session.json")
TOKEN_FILE = os.path.join(HOME, "token")
WEB = os.path.join(HOME, "web")
HOST, PORT = os.environ.get("CUA_HOST", "127.0.0.1"), int(os.environ.get("CUA_PORT", "8765"))
MAX_STEPS = int(os.environ.get("CUA_MAX_STEPS", "40"))
CTX_BUDGET = 60000          # est. prompt tokens before compacting history (80k unified context)
IMG_TOKENS = 2100           # one 1080p screenshot
UNANSWERED = object()


def load_token():
    if not os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "w") as f: f.write(secrets.token_urlsafe(24))
        os.chmod(TOKEN_FILE, 0o600)
    return open(TOKEN_FILE).read().strip()


def salvage_args(raw):
    """Arguments cut off by the token limit: recover the string fields that are there (mainly answer content)."""
    out = {}
    for k, v in re.findall(r'"(\w+)"\s*:\s*"((?:[^"\\]|\\.)*)', raw or ""):
        try: out[k] = json.loads('"' + v + '"')
        except Exception: out[k] = v
    if "content" in out: out["content"] += " …(cut off)"
    return out


def dedupe(text, max_paras=4):
    """Drop repeated paragraphs/sentences from a runaway plain-text reply."""
    out, seen = [], set()
    for p in re.split(r"\n\s*\n", text.strip()):
        key = re.sub(r"\W+", " ", p.lower()).strip()[:80]
        if key and key not in seen:
            seen.add(key); out.append(p.strip())
        if len(out) >= max_paras: break
    return "\n\n".join(out)


class Stopped(Exception):
    pass


# ======================================================================== session / agent loop
class Session:
    def __init__(self):
        self.lock = threading.RLock()
        self.subs = []                    # SSE subscriber queues
        self.inbox = queue.Queue()        # user messages
        self.stop = threading.Event()
        self.state = "idle"               # idle | working | waiting
        self.waiting_for = None           # question/confirm text while waiting
        self.desktop = agent.Desktop()
        self.system = agent.SYSTEM_PROMPT.replace("{apps}", agent.installed_apps()) + """
Session mode: this is one long, ongoing conversation with the user. Earlier tasks and your actions stay in
the history, so you can refer back to them. The user may send new messages while you work: treat them as
corrections or extra instructions for the current task (or a new task once you have answered).
A message right after your ask_user call is the user's answer.
To reply to the user (e.g. answering a question about what you did), call the answer tool once with a short reply;
you don't need to touch the screen for that."""
        self.load()

    # ---------------- persistence
    def load(self):
        try:
            d = json.load(open(STATE_FILE))
            self.messages, self.journal, self.events = d["messages"], d["journal"], d["events"]
            self.messages[0]["content"] = self.system      # refresh prompt (installed apps may change)
        except Exception:
            self.reset(save=False)
        self.next_id = (self.events[-1]["id"] + 1) if self.events else 1

    def reset(self, save=True):
        with self.lock:
            self.messages = [{"role": "system", "content": self.system}]
            self.journal, self.events, self.next_id = [], [], 1
        if save: self.save()

    def save(self):
        with self.lock:
            msgs = json.loads(json.dumps(self.messages))
            for m in msgs:   # never store screenshots on disk
                if isinstance(m.get("content"), list):
                    for c in m["content"]:
                        if c.get("type") == "image_url":
                            c.pop("image_url", None); c["type"] = "text"; c["text"] = "[screenshot evicted]"
            tmp = STATE_FILE + ".tmp"
            json.dump({"messages": msgs, "journal": self.journal[-400:], "events": self.events[-300:]}, open(tmp, "w"))
            os.replace(tmp, STATE_FILE)

    # ---------------- events to UIs
    def emit(self, kind, text="", **extra):
        with self.lock:
            ev = {"id": self.next_id, "t": time.time(), "kind": kind, "text": text, **extra}
            self.next_id += 1
            if kind not in ("state", "thinking", "live"): self.events.append(ev)
            for q in list(self.subs): q.put(ev)
        return ev

    def set_state(self, state, waiting_for=None):
        self.state, self.waiting_for = state, waiting_for
        self.emit("state", state, waiting_for=waiting_for)

    # ---------------- inputs from UIs
    def user_message(self, text):
        text = text.strip()
        if not text: return
        self.emit("user", text)
        self.inbox.put(text)

    def request_stop(self):
        if self.state != "idle":
            self.stop.set(); self.emit("status", "Stopping…")
            self.inbox.put(UNANSWERED)  # unblock a pending question

    # ---------------- context management
    def est_tokens(self):
        n = 0
        for m in self.messages:
            c = m.get("content")
            if isinstance(c, list):
                for x in c: n += IMG_TOKENS if x.get("type") == "image_url" else len(x.get("text", "")) // 3
            else: n += len(c or "") // 3
            n += len(json.dumps(m.get("tool_calls") or "")) // 3
        return n

    def compact(self):
        """Replace old turns with a text summary built from the journal, keeping the last few steps verbatim."""
        if self.est_tokens() < CTX_BUDGET: return
        obs = [i for i, m in enumerate(self.messages) if m["role"] == "user" and isinstance(m.get("content"), list)]
        if len(obs) < 6: return
        cut = obs[-4]       # keep the last 4 steps (each starts with an observation)
        lines = self.journal[-120:]
        summary = ("Summary of this session so far (older steps compressed; screenshots not available):\n"
                   + "\n".join(l[:220] for l in lines))
        current = next((m for m in reversed(self.messages[:cut]) if m["role"] == "user" and isinstance(m.get("content"), str)
                        and not m["content"].startswith("Summary of this session")), None)
        keep_head = [self.messages[0], {"role": "user", "content": summary}]
        if current: keep_head.append({"role": "user", "content": "(The user's latest request, repeated for reference:) " + current["content"]})
        self.messages = keep_head + self.messages[cut:]
        self.emit("status", "Compacted older history to stay within the model's memory.")

    # ---------------- the agent
    def wait_reply(self, kind, text):
        """Block until the user answers a question / confirmation (or stops)."""
        self.emit(kind, text)
        self.set_state("waiting", text)
        r = self.inbox.get()
        self.set_state("working")
        if r is UNANSWERED or self.stop.is_set(): raise Stopped()
        return r

    def close_dangling_calls(self, why):
        """Every assistant tool call needs a tool result before the next user message, or the template breaks."""
        for i in range(len(self.messages) - 1, -1, -1):
            m = self.messages[i]
            if m["role"] == "assistant":
                done = {x.get("tool_call_id") for x in self.messages[i + 1:] if x["role"] == "tool"}
                for c in m.get("tool_calls") or []:
                    if c.get("id") not in done:
                        self.messages.append({"role": "tool", "tool_call_id": c.get("id"), "content": why})
                return

    def drain_inbox(self):
        while True:
            try: m = self.inbox.get_nowait()
            except queue.Empty: return
            if m is UNANSWERED: continue
            self.user_spoke = True
            self.messages.append({"role": "user", "content": f"New message from the user while you were working: {m}"})
            self.journal.append(f"User (mid-task): {m}")

    def worker(self):
        while True:
            msg = self.inbox.get()
            if msg is UNANSWERED: continue
            self.stop.clear()
            with self.lock:
                self.messages.append({"role": "user", "content": msg}); self.journal.append(f"User: {msg}")
            self.set_state("working")
            agent.current_request = msg; self.reader_answer = None
            try:
                self.run_task()
            except Stopped:
                self.close_dangling_calls("Not executed: the user pressed Stop.")
                self.messages.append({"role": "user", "content": "[The user pressed Stop. Wait for further instructions.]"})
                self.journal.append("(stopped by user)")
                self.emit("status", "Stopped.")
            except Exception as e:
                traceback.print_exc()
                self.close_dangling_calls(f"error: {e}")
                self.emit("error", f"{type(e).__name__}: {e}")
                self.journal.append(f"(error: {e})")
            finally:
                self.stop.clear(); self.set_state("idle"); self.save()

    def run_task(self):
        d = self.desktop
        history, last_nudge, empty_steps, prev_thumb, last_result, nudges = [], -99, 0, None, "", 0
        for step in range(1, MAX_STEPS + 1):
            if self.stop.is_set(): raise Stopped()
            self.user_spoke = False
            self.drain_inbox()
            self.compact()
            img = d.screenshot()
            th = agent.thumb_of(img)
            think = agent.should_think(step, last_result, agent.changed(prev_thumb, th), self.user_spoke)
            prev_thumb = th
            self.messages.append(agent.observation(img)); agent.trim_to_last_n_images(self.messages, 3, keep=1)
            while True:   # runs once, or twice when a quick step tried to finish (see "answer" below)
                redo = False
                self.emit("thinking", f"step {step}" + ("" if think else ", quick"))
                msg = agent.chat_stream(self.messages, tools=agent.TOOLS, slot=0, max_tokens=3000, thinking=think,
                                        on_text=lambda t: self.emit("live", t[-600:]))
                calls = msg.get("tool_calls") or []
                thought = ((msg.get("reasoning_content") or "") + "\n" + (msg.get("content") or "")).strip()
                if not calls:
                    # Holo sometimes replies to conversational messages in plain text (and then repeats itself).
                    # Treat that as its answer to the user.
                    text = dedupe(msg.get("content") or "")
                    if text and not think and agent.THINK_MODE == "adaptive":
                        think = True; continue          # finishing: re-decide with thinking on
                    self.messages.append({"role": "assistant", "content": text})
                    if text:
                        self.journal.append(f"Agent answered: {text}")
                        self.emit("answer", text, thought=thought[-600:]); return
                    empty_steps += 1
                    if empty_steps >= 3: raise RuntimeError("the model produced no action three times in a row")
                    continue
                self.messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
                for call in calls:
                    name = call["function"]["name"]
                    try: a = json.loads(call["function"]["arguments"] or "{}")
                    except json.JSONDecodeError: a = salvage_args(call["function"]["arguments"])
                    if self.stop.is_set():
                        self.messages.append({"role": "tool", "tool_call_id": call["id"], "content": "Not executed: the user pressed Stop."})
                        raise Stopped()
                    if name in ("click", "double_click", "right_click", "type_into") and a.get("element"):
                        a["x"], a["y"] = agent.refine(img, a["element"], a["x"], a["y"])
                    desc = agent.describe(name, a)
                    self.journal.append(f"Action: {desc}")
                    if name == "answer" and not think and agent.THINK_MODE == "adaptive":
                        # finishing is a decision worth thinking about: redo this step with thinking on
                        self.messages.pop(); self.journal.pop()
                        think, redo = True, True
                        break
                    if name == "answer":
                        self.messages.append({"role": "tool", "tool_call_id": call["id"], "content": "Delivered to the user."})
                        self.journal.append(f"Agent answered: {a.get('content', '')}")
                        text = a.get("content", "")
                        if self.reader_answer:   # show the reader's answer instead of a retyped (error-prone) copy
                            first = re.split(r"(?<=[.!?])\s|\n", text.strip(), maxsplit=1)[0] if text.strip() else ""
                            text = (first + "\n\n" + self.reader_answer).strip()
                        self.emit("answer", text, thought=thought[-600:])
                        return
                    point = {"x": a["x"], "y": a["y"]} if "x" in a and "y" in a else None
                    if name == "ask_user":
                        result = self.wait_reply("question", a.get("question", ""))
                        self.journal.append(f"User replied: {result}")
                    else:
                        self.emit("action", desc, thought=thought[-600:], point=point, tool=name)
                        risky = (name in ("click", "double_click", "right_click") and agent.RISKY.search(a.get("element", ""))) \
                            or (name == "key" and agent.RISKY_KEYS.match(a.get("keys", "").replace(" ", "").lower()))
                        if risky:
                            r = self.wait_reply("confirm", f"Allow this? {desc}").strip()
                            if r.lower() not in ("y", "yes", "ok", "allow", "go", "do it"):
                                self.journal.append(f"User declined: {r}")
                                self.messages.append({"role": "tool", "tool_call_id": call["id"],
                                                      "content": f"User declined this action. Their message: {r}"})
                                continue
                        try:
                            result = agent.execute(d, name, a)
                        except Exception as e:
                            result = f"error: {e}"; self.emit("status", str(result))
                    history.append(agent.signature(name, a)); recent = history[-8:]
                    if step - last_nudge >= 3 and max(recent.count(x) for x in recent) >= 3:
                        nudges += 1; last_nudge = step
                        if nudges >= 4:   # still stuck after 3 nudges: stop and hand back to the user
                            self.messages.append({"role": "tool", "tool_call_id": call["id"], "content": str(result)})
                            self.emit("answer", "I seem to be stuck (" + agent.describe(name, a)[:80] + ", repeatedly). "
                                      "I stopped here. Tell me how to continue or what to do differently.")
                            self.journal.append("(stopped: stuck in a loop)"); return
                        result = f"{result}\n" + agent.loop_advice([h[0] for h in recent])
                        self.emit("status", "Loop detected, nudging the agent to change approach.")
                    self.messages.append({"role": "tool", "tool_call_id": call["id"], "content": str(result)})
                    last_result = str(result)
                    if name == "read_text" and last_result.startswith("Answer from the page text"):
                        self.reader_answer = last_result.split("\n", 1)[1].rsplit("\nThis answer is shown", 1)[0].strip()
                if redo: continue      # same screenshot, now with thinking
                break
            time.sleep(0.15)
            agent.wait_until_stable(d)
            if step % 5 == 0: self.save()
        self.emit("answer", f"I used my step budget ({MAX_STEPS}) without finishing. Tell me how to continue.")


# ======================================================================== live screen (shared grabber)
class Screen:
    def __init__(self, mon):
        self.mon, self.frame, self.viewers, self.cv = mon, None, 0, threading.Condition()
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            if self.viewers <= 0:
                time.sleep(0.3); continue
            try:
                jpg = subprocess.check_output(["grim", "-o", self.mon, "-t", "jpeg", "-q", "42", "-s", "0.5", "-"])
                with self.cv: self.frame = jpg; self.cv.notify_all()
            except Exception:
                time.sleep(1)
            time.sleep(0.6)   # ~1.4 fps: enough to follow the agent, light on mobile data


# ======================================================================== HTTP
SESSION: Session = None
SCREEN: Screen = None
TOKEN = ""
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
        ".svg": "image/svg+xml", ".webmanifest": "application/manifest+json", ".png": "image/png"}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "cua"

    def log_message(self, fmt, *args):
        pass

    def authed(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        cands = q.get("token", [])
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "cua_token": cands.append(v)
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer "): cands.append(auth[7:])
        return any(hmac.compare_digest(c, TOKEN) for c in cands)

    def send(self, code, body=b"", ctype="text/plain; charset=utf-8", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items(): self.send_header(k, v)
        self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/manifest.webmanifest", "/icon.svg", "/sw.js"):   # public, needed for install
            return self.static(path)
        if not self.authed():
            return self.send(401, b"<h3>Not signed in.</h3><p>Open the link that includes <code>?token=...</code> once "
                                  b"(run <code>cua-link</code> on the PC).</p>", "text/html; charset=utf-8")
        if path == "/":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "token" in q:   # set cookie, then drop the token from the URL
                return self.send(302, b"", headers={
                    "Set-Cookie": f"cua_token={TOKEN}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Strict",
                    "Location": "/"})
            return self.static("/index.html")
        if path == "/events": return self.events()
        if path == "/screen.mjpg": return self.mjpeg()
        if path == "/state":
            return self.send(200, json.dumps({"state": SESSION.state, "waiting_for": SESSION.waiting_for}).encode(), "application/json")
        return self.static(path)

    def do_POST(self):
        if not self.authed(): return self.send(401, b"unauthorized")
        path = urllib.parse.urlparse(self.path).path
        n = int(self.headers.get("Content-Length") or 0)
        try: body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError: body = {}
        if path == "/send":
            SESSION.user_message(str(body.get("text", ""))[:4000])
        elif path == "/stop":
            SESSION.request_stop()
        elif path == "/new":
            if SESSION.state != "idle": return self.send(409, b"busy: stop it first")
            SESSION.reset(); SESSION.emit("reset", "New session started.")
        else:
            return self.send(404, b"not found")
        self.send(200, b"{}", "application/json")

    def static(self, path):
        f = os.path.normpath(os.path.join(WEB, path.lstrip("/")))
        if not f.startswith(WEB + os.sep) or not os.path.isfile(f): return self.send(404, b"not found")
        self.send(200, open(f, "rb").read(), MIME.get(os.path.splitext(f)[1], "application/octet-stream"))

    def events(self):
        q = queue.Queue()
        with SESSION.lock:
            backlog = list(SESSION.events[-200:])
            SESSION.subs.append(q)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream"); self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no"); self.end_headers()
        try:
            for ev in backlog + [{"id": 0, "kind": "state", "text": SESSION.state, "waiting_for": SESSION.waiting_for}]:
                self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    ev = q.get(timeout=15)
                    self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with SESSION.lock:
                if q in SESSION.subs: SESSION.subs.remove(q)
            self.close_connection = True

    def mjpeg(self):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store"); self.end_headers()
        SCREEN.viewers += 1
        last = None
        try:
            while True:
                with SCREEN.cv:
                    SCREEN.cv.wait_for(lambda: SCREEN.frame is not None and SCREEN.frame is not last, timeout=10)
                    frame = SCREEN.frame
                if frame is None or frame is last: continue
                last = frame
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(frame)).encode()
                                 + b"\r\n\r\n" + frame + b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            SCREEN.viewers -= 1; self.close_connection = True


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    global SESSION, SCREEN, TOKEN
    TOKEN = load_token()
    SESSION = Session()
    SCREEN = Screen(SESSION.desktop.mon)
    agent.on_status = lambda t: SESSION.emit("status", t)
    threading.Thread(target=SESSION.worker, daemon=True).start()
    def warmup():   # load the model into the GPU now, so the first task doesn't wait for it
        for _ in range(30):
            try:
                agent.chat([{"role": "user", "content": "hi"}], thinking=False, max_tokens=1); return
            except Exception:
                time.sleep(2)
    threading.Thread(target=warmup, daemon=True).start()
    srv = Server((HOST, PORT), Handler)
    print(f"CUA service on http://{HOST}:{PORT}/  (model {agent.MODEL}, {len(SESSION.events)} events restored)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        SESSION.save()


if __name__ == "__main__":
    main()
