"""Dump real constructor signatures of the classes the tutor needs."""
import inspect

def sig(mod, cls):
    try:
        m = __import__(mod, fromlist=[cls])
        c = getattr(m, cls)
        print("\n%s.%s%s" % (mod, cls, inspect.signature(c.__init__)))
        print("   module file:", c.__module__)
        return c
    except Exception as exc:
        print("\nFAIL %s.%s -> %s" % (mod, cls, exc))

sig("pipecat.transports.local", "LocalTransport")
sig("pipecat.transports.local", "LocalTransportKinds")
sig("pipecat.transports.local", "LocalTransportFactory")
sig("pipecat.services.deepseek.llm", "DeepSeekLLMService")
sig("pipecat.services.whisper.stt", "WhisperSTTService")
sig("pipecat.services.piper.tts", "PiperTTSService")
sig("pipecat.audio.vad.silero", "SileroVADAnalyzer")
sig("pipecat.audio.vad.vad_analyzer", "VADParams")
sig("pipecat.pipeline.pipeline", "Pipeline")
sig("pipecat.pipeline.runner", "PipelineRunner")
sig("pipecat.pipeline.task", "PipelineTask")
sig("pipecat.processors.aggregators.llm_context", "LLMContext")
sig("pipecat.processors.frameworks.rtvi", "RTVIObserver")

# what transports.local exports
import pipecat.transports.local as L
print("\npipecat.transports.local exports:", [n for n in dir(L) if not n.startswith("_")])