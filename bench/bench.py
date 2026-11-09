#!/usr/bin/env python3
"""GUI-grounding benchmark against a running llama-server.

Usage: bench.py NAME [--port 8091] [--zoom] [--limit N] [--maxside PX]
Scores "click lands inside target bbox" on bench.json (ScreenSpot-Pro + v2 sample).
Prompt follows H Company's element-localization recipe (coords in [0,1000]).
"""
import argparse, base64, io, json, time, urllib.request, os, sys
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
SCHEMA = {"type": "object",
          "properties": {"x": {"type": "integer", "minimum": 0, "maximum": 1000, "description": "X coordinate as integer in [0, 1000]"},
                         "y": {"type": "integer", "minimum": 0, "maximum": 1000, "description": "Y coordinate as integer in [0, 1000]"}},
          "required": ["x", "y"]}
PROMPT = ("Localize an element on the GUI image according to the provided target and output a click position.\n"
          f" * You must output a valid JSON following the format: {json.dumps(SCHEMA)}\n Your target is:\n")


def b64(img):
    buf = io.BytesIO(); img.save(buf, "PNG"); return base64.b64encode(buf.getvalue()).decode()


def locate(port, img, target):
    body = {"messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64(img)}},
                {"type": "text", "text": PROMPT + target}]}],
            "temperature": 0.0, "max_tokens": 64,
            "response_format": {"type": "json_schema", "json_schema": {"schema": SCHEMA}},
            "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    r = json.load(urllib.request.urlopen(req, timeout=600))
    txt = r["choices"][0]["message"]["content"]
    p = json.loads(txt)
    return p["x"] / 1000 * img.width, p["y"] / 1000 * img.height, r.get("timings", {})


def ground(port, img, target, zoom=False, maxside=None):
    """Return (x, y) in original image pixels."""
    view, s = img, 1.0
    if maxside and max(img.size) > maxside:
        s = maxside / max(img.size)
        view = img.resize((round(img.width * s), round(img.height * s)), Image.LANCZOS)
    x, y, t = locate(port, view, target)
    if not zoom:
        return x / s, y / s
    # zoom from the downscaled view (what a real 1080p screen would give us), then map back
    img = view
    # second pass: crop a window (half width/height, at least 640x360) around the guess, at native res
    cw, ch = max(img.width // 2, min(640, img.width)), max(img.height // 2, min(360, img.height))
    x0 = int(min(max(x - cw / 2, 0), img.width - cw)); y0 = int(min(max(y - ch / 2, 0), img.height - ch))
    crop = img.crop((x0, y0, x0 + cw, y0 + ch))
    cx, cy, _ = locate(port, crop, target)
    return (x0 + cx) / s, (y0 + cy) / s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name"); ap.add_argument("--port", type=int, default=8091)
    ap.add_argument("--zoom", action="store_true"); ap.add_argument("--limit", type=int)
    ap.add_argument("--maxside", type=int); ap.add_argument("--only", choices=["pro", "v2"])
    a = ap.parse_args()
    samples = json.load(open(os.path.join(DATA, "bench.json")))
    samples = [s for s in samples if os.path.exists(os.path.join(DATA, s["img"])) and (not a.only or s["set"] == a.only)]
    if a.limit: samples = samples[:a.limit]
    out = os.path.join(HERE, "results", a.name + ".jsonl"); os.makedirs(os.path.dirname(out), exist_ok=True)
    res = []
    with open(out, "w") as f:
        for i, s in enumerate(samples):
            img = Image.open(os.path.join(DATA, s["img"])).convert("RGB")
            t0 = time.time()
            try:
                x, y = ground(a.port, img, s["instr"], a.zoom, a.maxside); err = None
            except Exception as e:
                x = y = -1; err = repr(e)[:200]
            dt = time.time() - t0
            x1, y1, x2, y2 = s["bbox"]
            hit = x1 <= x <= x2 and y1 <= y <= y2
            r = dict(id=s["id"], set=s["set"], kind=s["kind"], hit=hit, x=x, y=y, sec=round(dt, 2), err=err)
            res.append(r); f.write(json.dumps(r) + "\n"); f.flush()
            print(f"[{i+1}/{len(samples)}] {'HIT ' if hit else 'miss'} {dt:5.1f}s {s['id']} {err or ''}", file=sys.stderr)
    summarize(a.name, res)


def summarize(name, res):
    def acc(rs): return f"{100*sum(r['hit'] for r in rs)/max(len(rs),1):5.1f}% (n={len(rs)})"
    pro = [r for r in res if r["set"] == "pro"]; v2 = [r for r in res if r["set"] == "v2"]
    lat = sorted(r["sec"] for r in res)
    line = (f"{name}: ALL {acc(res)} | Pro {acc(pro)} | v2 {acc(v2)} | median {lat[len(lat)//2]:.1f}s "
            f"| errors {sum(1 for r in res if r['err'])}")
    print(line)
    open(os.path.join(HERE, "results", "summary.txt"), "a").write(line + "\n")


if __name__ == "__main__":
    main()
