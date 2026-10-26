"""Speech-to-text engines with one interface: transcribe(audio_float32_16k, language=None) -> str."""
import cuda_dlls  # noqa: F401  (must run before ctranslate2 / onnxruntime load CUDA)

import numpy as np

SR = 16000
DEVICE = "auto"  # "auto" | "cuda" | "cpu"; set from config.toml by main.py


def cuda_available():
    try:
        import onnxruntime
        return "CUDAExecutionProvider" in onnxruntime.get_available_providers()
    except Exception:
        return False


def use_cuda():
    return DEVICE == "cuda" or (DEVICE == "auto" and cuda_available())

# name -> (engine class name, model id, extra kwargs)
MODELS = {
    "whisper-large-v3-turbo": ("whisper", "large-v3-turbo", {"compute_type": "int8_float16"}),
    "whisper-large-v3-turbo-greedy": ("whisper", "large-v3-turbo", {"compute_type": "int8_float16", "beam_size": 1}),
    "whisper-large-v3": ("whisper", "large-v3", {"compute_type": "int8_float16"}),
    "whisper-medium": ("whisper", "medium", {"compute_type": "int8_float16"}),
    "whisper-small": ("whisper", "small", {"compute_type": "int8_float16"}),
    "parakeet-tdt-0.6b-v3": ("onnx", "nemo-parakeet-tdt-0.6b-v3", {}),
    "canary-1b-v2": ("onnx", "nemo-canary-1b-v2", {"needs_language": True}),
}


class WhisperEngine:
    def __init__(self, model_id, compute_type="int8_float16", beam_size=5):
        from faster_whisper import WhisperModel
        self.beam_size = beam_size
        if use_cuda():
            self.model = WhisperModel(model_id, device="cuda", compute_type=compute_type)
        else:
            self.model = WhisperModel(model_id, device="cpu", compute_type="int8")

    def transcribe(self, audio, language=None):
        segments, _ = self.model.transcribe(
            audio, language=language, beam_size=self.beam_size, vad_filter=True,
            condition_on_previous_text=False,
        )
        return " ".join(s.text.strip() for s in segments).strip()


class OnnxEngine:
    def __init__(self, model_id, needs_language=False, quantization=None):
        import onnx_asr
        import onnxruntime
        onnxruntime.set_default_logger_severity(3)
        self.needs_language = needs_language
        self.model = onnx_asr.load_model(
            model_id, quantization=quantization, providers=(["CUDAExecutionProvider"] if use_cuda() else []) + ["CPUExecutionProvider"]
        )

    def transcribe(self, audio, language=None):
        kw = {}
        if self.needs_language:
            kw["language"] = language or "es"
        return self.model.recognize(audio.astype(np.float32), sample_rate=SR, **kw).strip()


def _is_gpu_error(e):
    msg = str(e).upper()
    return any(k in msg for k in ("CUDA", "CUBLAS", "CUDNN", "OUT OF MEMORY"))


class FallbackEngine:
    """Wraps a GPU engine; if CUDA fails (e.g. VRAM taken by a game / LM Studio), reloads on CPU and retries."""

    def __init__(self, name):
        self.name = name
        self.engine = _build(name)

    def transcribe(self, audio, language=None):
        try:
            return self.engine.transcribe(audio, language=language)
        except Exception as e:
            global DEVICE
            if DEVICE == "cpu" or not _is_gpu_error(e):
                raise
            import logging
            logging.warning("GPU failed (%s); switching %s to CPU", str(e).splitlines()[0][:200], self.name)
            DEVICE = "cpu"
            self.engine = None
            self.engine = _build(self.name)
            return self.engine.transcribe(audio, language=language)


def _build(name):
    kind, model_id, kw = MODELS[name]
    cls = WhisperEngine if kind == "whisper" else OnnxEngine
    engine = cls(model_id, **kw)
    engine.transcribe(np.zeros(SR, dtype=np.float32), language="en")  # warm up
    return engine


def load(name):
    global DEVICE
    try:
        return FallbackEngine(name)
    except Exception as e:
        if DEVICE == "cpu" or not _is_gpu_error(e):
            raise
        DEVICE = "cpu"
        return FallbackEngine(name)
