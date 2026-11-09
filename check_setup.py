"""
Run this on any laptop BEFORE starting the app:   python check_setup.py
It checks everything the app needs and tells you how to fix what is missing.
"""
import importlib
import sys
from pathlib import Path

BASE = Path(__file__).parent
problems = 0


def check(ok, good, bad, fix=""):
    global problems
    if ok:
        print(f"  [ OK ] {good}")
    else:
        problems += 1
        print(f"  [FAIL] {bad}")
        if fix:
            print(f"         -> {fix}")


def gpu_test(A):
    """Really try to run Whisper on the GPU and print the exact error if it fails."""
    import subprocess

    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,driver_version",
             "--format=csv,noheader"], capture_output=True, text=True, timeout=20)
        check(out.returncode == 0, f"GPU found: {out.stdout.strip()}",
              "nvidia-smi returned an error", "Install or update the NVIDIA driver")
        print("         (name, total memory, memory ALREADY IN USE, driver version)")
        print("         If 'in use' is high, close other programs and run:  ollama stop <model>")
    except FileNotFoundError:
        check(False, "", "nvidia-smi not found: no NVIDIA driver (or no NVIDIA GPU)",
              "Install the NVIDIA driver, or use WHISPER_DEVICE = \"cpu\"")
        return

    A.add_cuda_dlls()
    try:
        import ctranslate2
        print(f"         ctranslate2 version: {ctranslate2.__version__}")
        n = ctranslate2.get_cuda_device_count()
        check(n > 0, f"{n} GPU(s) visible to Whisper's engine", "Whisper's engine sees no GPU",
              "pip install nvidia-cublas-cu12 \"nvidia-cudnn-cu12==9.*\"")
        if n > 0:
            print("         Compute types this GPU supports:", sorted(ctranslate2.get_supported_compute_types("cuda")))
    except Exception as e:
        check(False, "", f"ctranslate2 GPU check failed: {e}")
        return

    # Real test with your configured model (this is where cuBLAS errors show up)
    import numpy as np
    from faster_whisper import WhisperModel
    name = A.WHISPER_MODEL
    if A.SOURCE != "en" and name.endswith(".en"):
        name = name[:-3]
    print(f"         Testing '{name}' on the GPU (first time may download it)...")
    for compute in dict.fromkeys([A.WHISPER_COMPUTE, "int8_float16", "float16", "int8"]):
        try:
            model = WhisperModel(name, device="cuda", compute_type=compute)
            segs, _ = model.transcribe(np.zeros(16000, dtype=np.float32), language=A.SOURCE, beam_size=1)
            list(segs)
            check(True, f"Whisper runs on the GPU with compute_type='{compute}'", "")
            print(f"         -> Use: WHISPER_COMPUTE = \"{compute}\"")
            del model
            return
        except Exception as e:
            check(False, "", f"GPU test failed with compute_type='{compute}'", "")
            print(f"         EXACT ERROR: {e}")
    print("         -> Copy the EXACT ERROR lines above and ask for help, or use the CPU settings.")


print("\n1. Python")
check(sys.version_info >= (3, 10), f"Python {sys.version.split()[0]}",
      f"Python {sys.version.split()[0]} is too old", "Install Python 3.11 or 3.12")
in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
check(in_venv, "Virtual environment is active", "Virtual environment is NOT active",
      r"Run:  venv\Scripts\activate")

print("\n2. Python packages")
for module, pip_name in [("faster_whisper", "faster-whisper"), ("sounddevice", "sounddevice"),
                         ("numpy", "numpy"), ("fastapi", "fastapi"), ("uvicorn", "uvicorn[standard]"),
                         ("piper", "piper-tts"), ("ollama", "ollama"), ("qrcode", "qrcode[pil]")]:
    try:
        importlib.import_module(module)
        check(True, pip_name, "")
    except Exception as e:
        check(False, "", f"{pip_name} is not working ({e})", f"pip install {pip_name}")

print("\n3. Project files")
A = None
try:
    import app as A
    check(True, "app.py loads", "")
except Exception as e:
    check(False, "", f"app.py could not be loaded: {e}", "Make sure the packages above are installed")

check((BASE / "static" / "index.html").exists(), "static/index.html found",
      "static/index.html is missing", "Create a folder 'static' and put index.html inside it")
check((BASE / "ollama_connect.py").exists(), "ollama_connect.py found",
      "ollama_connect.py is missing", "Copy it into the same folder as app.py")

if A:
    print("\n4. French / English voices (Piper)")
    for lang, path in A.chosen_voices().items():
        p = BASE / path
        needed = lang == A.TARGET
        both = p.exists() and Path(str(p) + ".json").exists()
        if needed:
            check(both, f"{path} (needed for your current DIRECTION)",
                  f"{path} is missing (NEEDED for DIRECTION = {A.DIRECTION})",
                  "Run:  python voices.py   (option 3 downloads voices, options 1/2 select them)")
        else:
            print(f"  [ -- ] {path}: {'found' if both else 'missing (only needed for the other direction)'}")

    print("\n5. Ollama (translator)")
    try:
        from ollama_connect import get_client, model_names, resolve_url
        print(f"         address used: {resolve_url(A.OLLAMA_URL)}")
        names = model_names(get_client(A.OLLAMA_URL))
        check(True, "Connected to Ollama", "")
        check(A.OLLAMA_MODEL in names, f"Model '{A.OLLAMA_MODEL}' is installed",
              f"Model '{A.OLLAMA_MODEL}' is NOT installed on this laptop (installed: {', '.join(names) or 'none'})",
              f"Run:  ollama pull {A.OLLAMA_MODEL}")
    except Exception as e:
        check(False, "", f"Cannot reach Ollama: {e}", "Open the Ollama app (or run 'ollama serve')")

    print("\n6. Audio input (mixer)")
    try:
        import sounddevice as sd
        inputs = [(i, d["name"]) for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]
        check(bool(inputs), f"{len(inputs)} input device(s) found", "No audio input device found",
              "Connect the mixer/interface, then check Windows Sound settings")
        for i, name in inputs[:8]:
            print(f"         {i}: {name}")
        print(f"         AUDIO_DEVICE in app.py is currently: {A.AUDIO_DEVICE}")
    except Exception as e:
        check(False, "", f"Audio check failed: {e}")

    print("\n7. Whisper device")
    print(f"         Settings: {A.WHISPER_MODEL}, {A.WHISPER_DEVICE}, {A.WHISPER_COMPUTE}")
    if A.WHISPER_DEVICE == "cuda":
        gpu_test(A)
    else:
        print("         Running on CPU: nothing more to check.")

print("\n" + "=" * 50)
if problems == 0:
    print("Everything looks good. Start the app with:  python app.py")
else:
    print(f"{problems} problem(s) found. Fix the [FAIL] lines above, then run this check again.")
print("=" * 50)
