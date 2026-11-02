"""
Speech to text: the learner's recorded audio -> text, with faster-whisper.

Learners speak Hindi, English, Hinglish, or try German, often in one sentence,
so by default whisper detects the language per utterance (WHISPER_LANG=auto).
The audio can be any format the app records (wav, webm/opus, m4a, ogg):
faster-whisper decodes it with its bundled PyAV.

Runs on the GPU when it can (0.2 s per sentence) and on the CPU otherwise
(about 2 s). WHISPER_DEVICE=auto | cuda | cpu.
"""

import asyncio
import ctypes
import glob
import io
import logging
import site
import threading

from . import config

log = logging.getLogger("tutor")

_model = None
device = None  # "cuda" or "cpu" once loaded
_load_lock = threading.Lock()
# One transcription at a time: whisper already uses every CPU core.
_run_lock = threading.Lock()

# Primes whisper with the kind of speech to expect. Without it, a Hinglish
# question about German comes out as nonsense ("Qtntag Matlab, Kaya Hoda Hai").
BASE_HINT = "Guten Tag matlab kya hota hai? Danke ko German mein kaise bolte hain? Wie geht es Ihnen?"

# Half a second of silence as a WAV, to try the model once after loading.
_SILENCE = (b"RIFF" + (36 + 16000).to_bytes(4, "little") + b"WAVEfmt "
            + (16).to_bytes(4, "little") + (1).to_bytes(2, "little") + (1).to_bytes(2, "little")
            + (16000).to_bytes(4, "little") + (32000).to_bytes(4, "little")
            + (2).to_bytes(2, "little") + (16).to_bytes(2, "little")
            + b"data" + (16000).to_bytes(4, "little") + bytes(16000))

# Whisper invents these from silence and noise.
_PHANTOMS = {"", "thank you.", "thanks for watching!", "you", "."}


class STTError(Exception):
    """The audio could not be turned into text."""


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return True


def _load_cuda_libs():
    """
    The GPU build needs cuBLAS and cuDNN, installed as pip packages
    (nvidia-cublas-cu12, nvidia-cudnn-cu12) rather than system-wide. Load them
    by path so CTranslate2 finds them without LD_LIBRARY_PATH.
    """
    for root in site.getsitepackages():
        for pattern in ("nvidia/cublas/lib/libcublas*.so.*", "nvidia/cudnn/lib/libcudnn*.so.*"):
            for lib in sorted(glob.glob(f"{root}/{pattern}")):
                try:
                    ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
                except OSError:
                    pass


def _load():
    global _model, device
    with _load_lock:
        if _model is not None:
            return _model
        from faster_whisper import WhisperModel

        if config.WHISPER_DEVICE in ("auto", "cuda"):
            _load_cuda_libs()
            try:
                model = WhisperModel(config.WHISPER_MODEL, device="cuda", compute_type="float16")
                # A missing library only shows up on first use, so use it once.
                list(model.transcribe(io.BytesIO(_SILENCE))[0])
                _model, device = model, "cuda"
            except Exception as e:
                if config.WHISPER_DEVICE == "cuda":
                    raise
                log.warning("whisper cannot use the GPU, using the CPU: %s", e)
        if _model is None:
            _model, device = WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type="int8"), "cpu"
        log.info("whisper %s loaded on %s", config.WHISPER_MODEL, device)
    return _model


def _transcribe_sync(audio: bytes, words: list[str] | None = None) -> tuple[str, str]:
    model = _load()
    lang = None if config.WHISPER_LANG == "auto" else config.WHISPER_LANG
    with _run_lock:
        try:
            segments, info = model.transcribe(
                io.BytesIO(audio), language=lang, beam_size=1,
                vad_filter=True, condition_on_previous_text=False,
                initial_prompt=" ".join([BASE_HINT, *(words or [])[:30]]),
            )
            # Segments are decoded lazily, so a too-long clip costs nothing yet.
            if info.duration > config.MAX_AUDIO_SECONDS:
                raise STTError(f"Audio is {info.duration:.0f}s; the limit is {config.MAX_AUDIO_SECONDS}s.")
            # Whisper's own silence rule: likely no speech AND an unsure guess.
            # no_speech_prob alone drops real speech (0.5-0.7 is common here).
            text = " ".join(s.text.strip() for s in segments
                            if not (s.no_speech_prob > 0.6 and s.avg_logprob < -1.0))
        except STTError:
            raise
        except Exception as e:
            raise STTError(f"Could not decode or transcribe the audio: {e}") from e
    text = " ".join(text.split())
    if text.lower() in _PHANTOMS:
        text = ""
    return text, info.language


async def transcribe(audio: bytes, words: list[str] | None = None) -> tuple[str, str]:
    """
    (text, detected language). Empty text means nothing was heard. `words` are
    the lesson's German words, so whisper expects them.
    """
    return await asyncio.to_thread(_transcribe_sync, audio, words)


def warm_up():
    """Load the model now, so the first learner does not wait for it."""
    if available():
        _load()
