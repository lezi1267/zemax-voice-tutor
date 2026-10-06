"""Verify pipecat service imports for the voice tutor stack."""
import importlib
import importlib.util

candidates = {
    # deepseek llm
    "deepseek.llm": "pipecat.services.deepseek.llm",
    # local whisper stt
    "whisper.stt": "pipecat.services.whisper.stt",
    "whisper.stt2": "pipecat.services.whisper",
    # piper tts
    "piper.tts": "pipecat.services.piper.tts",
    "piper.tts2": "pipecat.services.piper",
    # silero vad
    "silero.vad": "pipecat.services.silero.vad",
    "silero.vad2": "pipecat.services.silero",
    # local transport
    "local.transport": "pipecat.transports.services.local",
    # pipeline
    "pipeline": "pipecat.pipeline.pipeline",
    "runner": "pipecat.pipeline.runner",
    # frames / processors
    "llm_context": "pipecat.processors.aggregators.llm_context",
}

for name, mod in candidates.items():
    try:
        importlib.import_module(mod)
        print("OK    %-18s %s" % (name, mod))
    except Exception as exc:
        print("FAIL  %-18s %s -> %s" % (name, mod, type(exc).__name__ + ": " + str(exc)[:120]))

# list actual service dirs to find real paths
import os
base = importlib.import_module("pipecat").__path__[0]
for sub in ("services", "transports/services", "transports/network", "processors/aggregators"):
    p = os.path.join(base, sub.replace("/", os.sep))
    if os.path.isdir(p):
        print("\n[%s]" % sub)
        print("  " + ", ".join(sorted(os.listdir(p))[:40]))