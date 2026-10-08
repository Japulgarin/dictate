"""Stream the first N clips of Google FLEURS dev (es_419, en_us) without downloading the full archives."""
import csv
import io
import os
import sys
import tarfile

import requests
from huggingface_hub import hf_hub_download, hf_hub_url

N = 40
OUT = os.path.join(os.path.dirname(__file__), "data", "fleurs")


def fetch(lang):
    out = os.path.join(OUT, lang)
    os.makedirs(out, exist_ok=True)
    tsv = hf_hub_download("google/fleurs", f"data/{lang}/dev.tsv", repo_type="dataset")
    refs = {}
    with open(tsv, encoding="utf-8") as f:
        for row in csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
            refs[row[1]] = row[2]  # file_name -> raw transcription
    url = hf_hub_url("google/fleurs", f"data/{lang}/audio/dev.tar.gz", repo_type="dataset")
    got = []
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        r.raw.decode_content = True
        with tarfile.open(fileobj=r.raw, mode="r|gz") as tar:
            for m in tar:
                name = os.path.basename(m.name)
                if not m.isfile() or name not in refs:
                    continue
                with open(os.path.join(out, name), "wb") as w:
                    w.write(tar.extractfile(m).read())
                got.append((name, refs[name]))
                if len(got) >= N:
                    break
    with open(os.path.join(out, "refs.tsv"), "w", encoding="utf-8", newline="") as f:
        csv.writer(f, delimiter="\t").writerows(got)
    print(lang, len(got), "clips")


if __name__ == "__main__":
    for lang in (sys.argv[1:] or ["es_419", "en_us", "de_de"]):
        fetch(lang)
