"""Benchmark STT models on FLEURS (es/en) + your own recordings. Usage: python run_bench.py [model ...]"""
import csv
import gc
import os
import re
import sys
import threading
import time
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))

import engines  # noqa: E402
import jiwer  # noqa: E402
import numpy as np  # noqa: E402
import pynvml  # noqa: E402
import soundfile as sf  # noqa: E402

DATA = os.path.join(HERE, "data")
SETS = {"fleurs-es": ("fleurs/es_419", "es"), "fleurs-en": ("fleurs/en_us", "en"), "fleurs-de": ("fleurs/de_de", "de"),
        "mine": ("mine", None)}


def norm(t):
    t = unicodedata.normalize("NFC", t.lower())
    t = re.sub(r"[^\w\s']", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def load_set(sub):
    d = os.path.join(DATA, sub)
    p = os.path.join(d, "refs.tsv")
    if not os.path.exists(p):
        return []
    items = []
    with open(p, encoding="utf-8") as f:
        for row in csv.reader(f, delimiter="\t"):
            audio, sr = sf.read(os.path.join(d, row[0]), dtype="float32")
            if audio.ndim > 1:
                audio = audio.mean(axis=1)
            assert sr == 16000, sr
            lang = row[2] if len(row) > 2 else None
            items.append((audio, row[1], lang))
    return items


class VramPeak:
    def __init__(self, h):
        self.h, self.peak, self.stop = h, 0, False

    def run(self):
        while not self.stop:
            self.peak = max(self.peak, pynvml.nvmlDeviceGetMemoryInfo(self.h).used)
            time.sleep(0.05)


def bench(name, sets, h):
    gc.collect()
    base = pynvml.nvmlDeviceGetMemoryInfo(h).used
    mon = VramPeak(h)
    t = threading.Thread(target=mon.run, daemon=True)
    t.start()
    t0 = time.perf_counter()
    eng = engines.load(name)
    load_s = time.perf_counter() - t0
    rows = []
    for set_name, (items, set_lang) in sets.items():
        refs, hyps, audio_s, proc_s, lat = [], [], 0.0, 0.0, []
        for audio, ref, lang in items:
            t1 = time.perf_counter()
            hyp = eng.transcribe(audio, language=lang or set_lang if engines.MODELS[name][2].get("needs_language") else None)
            dt = time.perf_counter() - t1
            refs.append(norm(ref)); hyps.append(norm(hyp) or "<empty>")
            audio_s += len(audio) / 16000; proc_s += dt; lat.append(dt)
        wer = jiwer.wer(refs, hyps) * 100
        rows.append({"model": name, "set": set_name, "clips": len(items), "wer": round(wer, 2),
                     "rtfx": round(audio_s / proc_s, 1), "avg_latency_s": round(float(np.mean(lat)), 3),
                     "p90_latency_s": round(float(np.percentile(lat, 90)), 3)})
        print(f"  {set_name:10s} WER {wer:6.2f}%  RTFx {audio_s/proc_s:7.1f}  avg {np.mean(lat):.3f}s", flush=True)
    mon.stop = True; t.join()
    vram = (mon.peak - base) / 2**20
    for r in rows:
        r["load_s"] = round(load_s, 1); r["peak_vram_mb"] = round(vram)
    del eng
    gc.collect()
    return rows


def main():
    pynvml.nvmlInit()
    h = pynvml.nvmlDeviceGetHandleByIndex(0)
    sets = {k: (load_set(sub), lang) for k, (sub, lang) in SETS.items()}
    sets = {k: v for k, v in sets.items() if v[0]}
    names = sys.argv[1:] or list(engines.MODELS)
    out = os.path.join(HERE, "results.csv")
    done = []
    if os.path.exists(out):
        with open(out, encoding="utf-8") as f:
            done = [r for r in csv.DictReader(f) if r["model"] not in names]
    rows = list(done)
    for n in names:
        print(f"== {n}", flush=True)
        try:
            rows += bench(n, sets, h)
        except Exception as e:  # keep going if one model fails
            print(f"  FAILED: {e!r}", flush=True)
        with open(out, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["model", "set", "clips", "wer", "rtfx", "avg_latency_s",
                                              "p90_latency_s", "load_s", "peak_vram_mb"])
            w.writeheader(); w.writerows(rows)
    with open(os.path.join(HERE, "results.md"), "w", encoding="utf-8") as f:
        f.write("| model | set | WER % | RTFx | avg latency s | p90 s | load s | VRAM MB |\n|---|---|---|---|---|---|---|---|\n")
        for r in rows:
            f.write(f"| {r['model']} | {r['set']} | {r['wer']} | {r['rtfx']} | {r['avg_latency_s']} | "
                    f"{r['p90_latency_s']} | {r['load_s']} | {r['peak_vram_mb']} |\n")
    print("wrote results.csv / results.md")


if __name__ == "__main__":
    main()
