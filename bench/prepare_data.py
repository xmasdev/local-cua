#!/usr/bin/env python3
"""Download the benchmark sample used in the README (fixed seed, so results are comparable).

148 ScreenSpot-Pro samples (Linux, VS Code, PyCharm, Word, Excel, Windows) and
100 ScreenSpot-v2 desktop samples, written to bench/data/bench.json plus the images.
"""
import json, os, random, urllib.parse, urllib.request, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
D = os.path.join(HERE, "data")
PRO = "https://huggingface.co/datasets/likaixin/ScreenSpot-Pro/resolve/main/"
V2 = "https://huggingface.co/datasets/OS-Copilot/ScreenSpot-v2/resolve/main/"
TAKE = {"linux_common_linux": 50, "vscode_macos": 20, "pycharm_macos": 20, "word_macos": 20,
        "excel_macos": 20, "windows_common_windows": 20}


def get(url, path):
    if os.path.exists(path): return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    urllib.request.urlretrieve(url, path)


def main():
    random.seed(0)
    out = []
    for k, n in TAKE.items():
        ann = os.path.join(D, "sspro", "annotations", k + ".json")
        get(PRO + f"annotations/{k}.json", ann)
        for s in random.sample(json.load(open(ann)), n):
            out.append(dict(id=s["id"], img="sspro/images/" + s["img_filename"], bbox=s["bbox"],
                            instr=s["instruction"], kind=s["ui_type"], set="pro"))
    v2 = os.path.join(D, "ssv2", "screenspot_desktop_v2.json")
    get(V2 + "screenspot_desktop_v2.json", v2)
    for i, s in enumerate(random.sample(json.load(open(v2)), 100)):
        x, y, w, h = s["bbox"]
        out.append(dict(id=f"v2_{i}", img="ssv2/img/" + s["img_filename"], bbox=[x, y, x + w, y + h],
                        instr=s["instruction"], kind=s["data_type"], set="v2"))
    json.dump(out, open(os.path.join(D, "bench.json"), "w"), indent=0)

    for img in sorted({o["img"] for o in out if o["set"] == "pro"}):
        rel = img[len("sspro/images/"):]
        try: get(PRO + "images/" + urllib.parse.quote(rel), os.path.join(D, img))
        except Exception as e: print("skip", rel, e)   # bench.py ignores samples whose image is missing
    need = {os.path.basename(o["img"]) for o in out if o["set"] == "v2"}
    if not all(os.path.exists(os.path.join(D, "ssv2", "img", n)) for n in need):
        z = os.path.join(D, "ssv2", "imgs.zip")
        get(V2 + "screenspotv2_image.zip", z)
        os.makedirs(os.path.join(D, "ssv2", "img"), exist_ok=True)
        with zipfile.ZipFile(z) as zf:
            for info in zf.infolist():
                b = os.path.basename(info.filename)
                if b in need: open(os.path.join(D, "ssv2", "img", b), "wb").write(zf.read(info))
        os.remove(z)
    print(f"{len(out)} samples ready in {D}")


if __name__ == "__main__":
    main()
