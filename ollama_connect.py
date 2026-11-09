"""
Connects to Ollama whether or not the OLLAMA_HOST environment variable is set.

Handles:
  - not set                -> http://127.0.0.1:11434
  - 0.0.0.0 (or 0.0.0.0:11434, http://0.0.0.0:11434) -> http://127.0.0.1:11434
  - a custom port          -> kept, e.g. 127.0.0.1:11500
  - a remote machine       -> kept, e.g. 192.168.1.50:11434

Test it by itself:   python ollama_connect.py
Test with a model:   python ollama_connect.py qwen2.5:7b
"""
import os
import sys
from urllib.parse import urlparse

DEFAULT_PORT = 11434
LOCAL = "127.0.0.1"


def resolve_url(url=None):
    """Return a clean http(s)://host:port URL that Python can actually connect to."""
    raw = (url or os.environ.get("OLLAMA_HOST", "")).strip()
    if not raw:
        return f"http://{LOCAL}:{DEFAULT_PORT}"

    if "://" not in raw:
        raw = "http://" + raw
    parsed = urlparse(raw)

    scheme = parsed.scheme or "http"
    host = parsed.hostname or LOCAL
    if host in ("0.0.0.0", "::", "[::]", "localhost", ""):
        host = LOCAL   # 0.0.0.0 is a "listen" address, not somewhere you can connect to

    port = parsed.port
    if port is None and scheme == "http":
        port = DEFAULT_PORT

    return f"{scheme}://{host}:{port}" if port else f"{scheme}://{host}"


def get_client(url=None):
    """Return an ollama.Client that ignores broken OLLAMA_HOST values and proxy settings."""
    import ollama
    return ollama.Client(host=resolve_url(url), trust_env=False)


def model_names(client):
    """List installed model names (works with old and new ollama python versions)."""
    data = client.list()
    models = data["models"] if isinstance(data, dict) else data.models
    names = []
    for m in models:
        if isinstance(m, dict):
            names.append(m.get("model") or m.get("name"))
        else:
            names.append(getattr(m, "model", None) or getattr(m, "name", None))
    return [n for n in names if n]


if __name__ == "__main__":
    print("OLLAMA_HOST in this computer :", os.environ.get("OLLAMA_HOST", "(not set)"))
    url = resolve_url()
    print("Address the app will use     :", url)
    try:
        client = get_client()
        names = model_names(client)
        print("Connection                   : OK")
        print("Installed models             :", ", ".join(names) if names else "(none - run: ollama pull <model>)")
        if len(sys.argv) > 1:
            model = sys.argv[1]
            if model not in names:
                print(f"\n!! Model '{model}' is not installed. Run: ollama pull {model}")
            else:
                print(f"\nTesting '{model}' (first call can take a while to load)...")
                r = client.chat(model=model, messages=[
                    {"role": "user", "content": "Translate to French, output only the translation: Good morning, everyone."}])
                print("Answer:", r["message"]["content"].strip())
    except Exception as e:
        print("Connection                   : FAILED")
        print("Reason:", e)
