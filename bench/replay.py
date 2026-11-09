#!/usr/bin/env python3
"""Speed benchmark for the agent loop: replay a recorded run (screenshots + actions) against a server.

  replay.py NAME RUN_DIR [--port 8091] [--reps 2]

For every step the model gets exactly the prompt the live agent built (system prompt, task, history of the
recorded actions, last 1-3 screenshots) and generates its own next action with the live settings
(thinking on, temp 0.8, tool_choice required). Its output is timed and then discarded; the recorded action
goes into the history, so every configuration sees identical prompts.
"""
import argparse, json, os, re, sys, time, urllib.request
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import agent  # noqa: E402

LINE = re.compile(r"^\[(\d+)\] (\w+) ?(.*)$")


def parse_action(name, rest):
    if name in ("click", "double_click", "right_click"):
        m = re.match(r"'(.*)' @\((\d+),(\d+)\)", rest)
        return {"element": m.group(1), "x": int(m.group(2)), "y": int(m.group(3))}
    if name == "write":
        m = re.match(r"(['\"])(.*)\1( \+ Enter)?$", rest, re.S)
        return {"content": m.group(2).encode().decode("unicode_escape"), "press_enter": bool(m.group(3))}
    if name == "key": return {"keys": rest}
    if name == "scroll":
        m = re.match(r"(\w+) x(\d+) @\((\d+),(\d+)\)", rest)
        return {"direction": m.group(1), "amount": int(m.group(2)), "x": int(m.group(3)), "y": int(m.group(4))}
    try: return json.loads(rest)
    except Exception: return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name"); ap.add_argument("run"); ap.add_argument("--port", type=int, default=8091)
    ap.add_argument("--reps", type=int, default=2)
    a = ap.parse_args()
    agent.BASE = f"http://127.0.0.1:{a.port}/v1"
    lines = open(os.path.join(a.run, "log.txt")).read().splitlines()
    task = lines[0][len("TASK: "):]
    steps = []
    for l in lines[1:]:
        m = LINE.match(l)
        if m: steps.append((int(m.group(1)), m.group(2), parse_action(m.group(2), m.group(3))))
    system = agent.SYSTEM_PROMPT.replace("{apps}", agent.installed_apps())
    rows = []
    for rep in range(a.reps):
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": task}]
        for i, (n, name, args) in enumerate(steps):
            img = Image.open(os.path.join(a.run, f"{n:02d}.png")).convert("RGB")
            msgs.append(agent.observation(img)); agent.trim_to_last_n_images(msgs, 3, keep=1)
            body = {"model": "x", "messages": msgs, "temperature": 0.8, "max_tokens": 1200, "tools": agent.TOOLS,
                    "tool_choice": "required", "id_slot": 0, "reasoning_budget_tokens": 512,
                    "chat_template_kwargs": {"enable_thinking": True}}
            t0 = time.time()
            r = json.load(urllib.request.urlopen(urllib.request.Request(
                agent.BASE + "/chat/completions", json.dumps(body).encode(), {"Content-Type": "application/json"}), timeout=600))
            dt = time.time() - t0; t = r.get("timings", {})
            got = (r["choices"][0]["message"].get("tool_calls") or [{}])[0].get("function", {}).get("name")
            rows.append(dict(rep=rep, step=n, total=dt, prompt_n=t.get("prompt_n"), prompt_ms=t.get("prompt_ms"),
                             gen_n=t.get("predicted_n"), gen_ms=t.get("predicted_ms"), draft_n=t.get("draft_n"),
                             draft_acc=t.get("draft_n_accepted"), same_tool=(got == name)))
            print(f"rep{rep} step{n:2d} {dt:5.2f}s prompt {t.get('prompt_n')}/{t.get('prompt_ms',0)/1000:.2f}s "
                  f"gen {t.get('predicted_n')} tok {t.get('predicted_per_second',0):5.1f} tok/s "
                  f"draft {t.get('draft_n_accepted')}/{t.get('draft_n')}  tool {got} (rec {name})", file=sys.stderr)
            cid = f"call_{n}"
            msgs.append({"role": "assistant", "content": "",
                         "tool_calls": [{"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]})
            msgs.append({"role": "tool", "tool_call_id": cid, "content": "ok"})
    def avg(k, rs): v = [r[k] for r in rs if r[k] is not None]; return sum(v) / max(len(v), 1)
    rs = [r for r in rows if r["rep"] > 0] or rows          # first rep warms caches
    gen_tps = sum(r["gen_n"] for r in rs) / max(sum(r["gen_ms"] for r in rs) / 1000, 1e-9)
    acc = sum(r["draft_acc"] or 0 for r in rs), sum(r["draft_n"] or 0 for r in rs)
    line = (f"{a.name}: step {avg('total', rs):.2f}s | prompt {avg('prompt_ms', rs)/1000:.2f}s | gen {avg('gen_n', rs):.0f} tok "
            f"@ {gen_tps:.1f} tok/s ({avg('gen_ms', rs)/1000:.2f}s) | draft accepted {acc[0]}/{acc[1]} | "
            f"same tool as recorded {sum(r['same_tool'] for r in rs)}/{len(rs)}")
    print(line)
    open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "speed.txt"), "a").write(line + "\n")


if __name__ == "__main__":
    main()
