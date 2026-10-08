"""Record yourself reading sentences so the benchmark also measures YOUR voice + mic. Press Enter to start/stop each one."""
import csv
import os

import numpy as np
import sounddevice as sd
import soundfile as sf

OUT = os.path.join(os.path.dirname(__file__), "data", "mine")
SENTENCES = [
    ("es", "Mañana tengo una reunión a las tres de la tarde con el equipo de ventas."),
    ("es", "Por favor, envíame el informe antes del viernes para poder revisarlo."),
    ("es", "La computadora portátil se calienta mucho cuando uso la tarjeta gráfica."),
    ("es", "Necesito comprar leche, huevos, pan y un poco de café para la semana."),
    ("es", "¿Puedes recordarme llamar a mi mamá cuando salga del trabajo?"),
    ("en", "I need to finish the presentation before the meeting on Thursday morning."),
    ("en", "Please send me the link to the repository so I can review the code."),
    ("en", "The speech recognition model runs locally on my laptop graphics card."),
    ("en", "Let's schedule a quick call next week to talk about the budget."),
    ("en", "Can you remind me to buy groceries on my way home tonight?"),
]


def main():
    os.makedirs(OUT, exist_ok=True)
    rows = []
    for i, (lang, text) in enumerate(SENTENCES, 1):
        print(f"\n[{i}/{len(SENTENCES)}] ({lang}) Read this:\n  {text}")
        input("  Enter = start recording...")
        chunks = []
        with sd.InputStream(samplerate=16000, channels=1, dtype="float32",
                            callback=lambda d, *_: chunks.append(d.copy())):
            input("  Recording... Enter = stop")
        name = f"mine_{i:02d}.wav"
        sf.write(os.path.join(OUT, name), np.concatenate(chunks)[:, 0], 16000)
        rows.append((name, text, lang))
    with open(os.path.join(OUT, "refs.tsv"), "w", encoding="utf-8", newline="") as f:
        csv.writer(f, delimiter="\t").writerows(rows)
    print(f"\nSaved {len(rows)} clips to {OUT}. Now run: python bench\run_bench.py")


if __name__ == "__main__":
    main()
