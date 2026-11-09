#!/usr/bin/env python3
"""Terminal chat with the CUA service (same session as the phone app).

  cua                 open the chat
  cua "do something"  open the chat and send this right away
Commands: /stop  /new  /quit      (while it asks "Allow?", answer y / n)
"""
import json, os, sys, threading, urllib.request

URL = os.environ.get("CUA_URL", "http://127.0.0.1:8765")
TOKEN = open(os.path.expanduser("~/cua/token")).read().strip() if os.path.exists(os.path.expanduser("~/cua/token")) else ""
HDR = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
C = {"dim": "\033[2m", "b": "\033[1m", "blue": "\033[34m", "yellow": "\033[33m", "red": "\033[31m", "green": "\033[32m", "x": "\033[0m"}


def post(path, body=None):
    req = urllib.request.Request(URL + path, json.dumps(body or {}).encode(), HDR)
    try: urllib.request.urlopen(req, timeout=10).read()
    except urllib.error.HTTPError as e: print(f"{C['red']}{e.read().decode()}{C['x']}")


def out(s):
    sys.stdout.write("\r\033[K" + s + "\n> "); sys.stdout.flush()


def listen():
    seen = set()
    while True:
        try:
            r = urllib.request.urlopen(urllib.request.Request(URL + "/events", headers=HDR), timeout=60)
            for raw in r:
                line = raw.decode().strip()
                if not line.startswith("data: "): continue
                ev = json.loads(line[6:]); k, t = ev["kind"], ev.get("text", "")
                if ev.get("id") and ev["id"] in seen: continue
                if ev.get("id"): seen.add(ev["id"])
                if k == "user": out(f"{C['blue']}you ›{C['x']} {t}")
                elif k == "action": out(f"{C['dim']}  · {t}{C['x']}")
                elif k == "answer": out(f"{C['green']}{C['b']}agent ›{C['x']} {t}")
                elif k == "question": out(f"{C['yellow']}{C['b']}? {t}{C['x']}  (type your answer)")
                elif k == "confirm": out(f"{C['yellow']}{C['b']}! {t}{C['x']}  (y / n)")
                elif k in ("status", "reset"): out(f"{C['dim']}  [{t}]{C['x']}")
                elif k == "error": out(f"{C['red']}  error: {t}{C['x']}")
                elif k == "state" and t == "working": out(f"{C['dim']}  [working… /stop to halt]{C['x']}")
        except Exception:
            import time; time.sleep(2)


def main():
    sys.stdout.write("\033]2;CUA chat\007")   # window title: the agent never touches "CUA…" windows
    try:
        urllib.request.urlopen(urllib.request.Request(URL + "/state", headers=HDR), timeout=3).read()
    except Exception:
        print("CUA is not running. Start it with:  cua start"); return
    print(f"{C['b']}CUA chat{C['x']}  (same session as the phone app)  /stop  /new  /quit")
    threading.Thread(target=listen, daemon=True).start()
    if len(sys.argv) > 1: post("/send", {"text": " ".join(sys.argv[1:])})
    while True:
        try: line = input("> ").strip()
        except (EOFError, KeyboardInterrupt): print(); return
        if not line: continue
        if line in ("/quit", "/exit", "/q"): return
        if line == "/stop": post("/stop"); continue
        if line == "/new": post("/new"); continue
        post("/send", {"text": line})


if __name__ == "__main__":
    main()
