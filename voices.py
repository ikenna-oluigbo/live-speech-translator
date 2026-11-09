"""
Voice manager for Live Speech Translator.

Run it BEFORE starting the translation:     python voices.py

You can:
  - listen to a sample of any installed voice (plays on the laptop speakers)
  - download more English / French voices (internet needed, ~60-110 MB each)
  - choose the English voice and the French voice the app will use

Your choice is saved in voice_settings.json, so you never need to edit the code.
"""
import json
import urllib.request
from pathlib import Path

import numpy as np

BASE = Path(__file__).parent
VOICE_DIR = BASE / "voices"
SETTINGS_FILE = BASE / "voice_settings.json"

CATALOG_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/voices.json"
FILE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/{path}"

LANGS = {"en": "English", "fr": "French"}
DEFAULTS = {"en": "en_US-lessac-medium", "fr": "fr_FR-siwis-medium"}
SAMPLES = {
    "en": "Good morning everyone, and welcome to our event. We are glad to have you with us today.",
    "fr": "Bonjour à tous, et bienvenue à notre événement. Nous sommes heureux de vous accueillir aujourd'hui.",
}
# Used only if the online catalog can't be reached
FALLBACK_IDS = {
    "en": ["en_US-lessac-medium", "en_US-amy-medium", "en_US-ryan-medium",
           "en_GB-alan-medium", "en_GB-alba-medium"],
    "fr": ["fr_FR-siwis-medium", "fr_FR-tom-medium", "fr_FR-upmc-medium", "fr_FR-gilles-low"],
}

_catalog = None


# ---------------------------------------------------------------------
# Saved choice
# ---------------------------------------------------------------------
def load_choice():
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_choice(lang, voice_id):
    data = load_choice()
    data[lang] = f"voices/{voice_id}.onnx"
    SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def current_id(lang):
    return Path(load_choice().get(lang, DEFAULTS[lang])).stem


# ---------------------------------------------------------------------
# Installed voices
# ---------------------------------------------------------------------
def is_installed(voice_id):
    onnx = VOICE_DIR / f"{voice_id}.onnx"
    return onnx.exists() and Path(str(onnx) + ".json").exists()


def installed_voices(lang):
    names = []
    if VOICE_DIR.exists():
        for f in sorted(VOICE_DIR.glob("*.onnx")):
            if f.stem.split("_")[0] == lang and is_installed(f.stem):
                names.append(f.stem)
    return names


# ---------------------------------------------------------------------
# Online catalog + downloads
# ---------------------------------------------------------------------
def _open(url, timeout=60):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "live-translator"}),
                                  timeout=timeout)


def fetch_catalog():
    global _catalog
    if _catalog is not None:
        return _catalog
    try:
        print("Contacting the voice catalog (internet needed)...")
        with _open(CATALOG_URL, timeout=30) as r:
            _catalog = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"Could not reach the voice catalog: {e}")
        _catalog = {}
    return _catalog


def catalog_voices(lang):
    """Single-speaker voices for a language: list of (id, quality, region, size_in_bytes)."""
    cat = fetch_catalog()
    rows = []
    if cat:
        for vid, info in cat.items():
            language = info.get("language", {})
            if language.get("family") != lang or info.get("num_speakers", 1) != 1:
                continue
            size = sum(f.get("size_bytes", 0) for p, f in info.get("files", {}).items() if p.endswith(".onnx"))
            rows.append((vid, info.get("quality", vid.split("-")[-1]), language.get("region", ""), size))
    else:
        print("Showing a short built-in list instead (some names may not exist).")
        for vid in FALLBACK_IDS[lang]:
            rows.append((vid, vid.split("-")[-1], vid.split("-")[0].split("_")[-1], 0))
    rows.sort(key=lambda r: (r[2], r[0]))
    return rows


def download_voice(voice_id):
    VOICE_DIR.mkdir(exist_ok=True)
    info = fetch_catalog().get(voice_id)
    paths = [p for p in info["files"] if p.endswith(".onnx") or p.endswith(".onnx.json")] if info else []
    if not paths:
        region, name, quality = voice_id.split("-", 2)
        base = f"{region.split('_')[0]}/{region}/{name}/{quality}/{voice_id}"
        paths = [base + ".onnx", base + ".onnx.json"]

    for p in paths:
        dest = VOICE_DIR / Path(p).name
        if dest.exists():
            continue
        tmp = dest.with_name(dest.name + ".part")
        try:
            with _open(FILE_URL.format(path=p)) as r, open(tmp, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        print(f"\r   {dest.name}: {done * 100 // total}%", end="", flush=True)
            tmp.replace(dest)
            print()
        except Exception as e:
            print(f"\n   Download failed for {dest.name}: {e}")
            if tmp.exists():
                tmp.unlink()
            return False
    return True


# ---------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------
def synth_pcm(voice, text):
    pcm, rate = bytearray(), None
    if hasattr(voice, "synthesize_stream_raw"):        # older piper-tts
        rate = voice.config.sample_rate
        for chunk in voice.synthesize_stream_raw(text):
            pcm += chunk
    else:                                              # newer piper-tts
        for chunk in voice.synthesize(text):
            rate = chunk.sample_rate
            pcm += chunk.audio_int16_bytes
    return np.frombuffer(bytes(pcm), dtype=np.int16), rate


def preview(voice_id, lang):
    try:
        import sounddevice as sd
        from piper import PiperVoice
        voice = PiperVoice.load(str(VOICE_DIR / f"{voice_id}.onnx"))
        pcm, rate = synth_pcm(voice, SAMPLES[lang])
        print("   Playing on the laptop's default speakers...")
        sd.play(pcm, rate)
        sd.wait()
        return True
    except Exception as e:
        print(f"   Could not play this voice: {e}")
        return False


# ---------------------------------------------------------------------
# Menus
# ---------------------------------------------------------------------
def ask(prompt):
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit


def choose_voice(lang):
    names = installed_voices(lang)
    if not names:
        print(f"\nNo {LANGS[lang]} voices are installed yet. Use option 3 to download some.")
        return
    while True:
        print(f"\n{LANGS[lang]} voices on this laptop:")
        for i, n in enumerate(names, 1):
            print(f"  {i}) {n}{'   <- currently selected' if n == current_id(lang) else ''}")
        answer = ask("Type a number to LISTEN to it (or press Enter to go back): ")
        if not answer:
            return
        if not answer.isdigit() or not (1 <= int(answer) <= len(names)):
            print("   Please type one of the numbers above.")
            continue
        voice_id = names[int(answer) - 1]
        while True:
            print(f"\n   Voice: {voice_id}")
            preview(voice_id, lang)
            r = ask("   Use this voice?  y = yes, r = replay, n = no : ").lower()
            if r == "r":
                continue
            if r == "y":
                save_choice(lang, voice_id)
                print(f"   Saved. The app will speak {LANGS[lang]} with '{voice_id}'.")
                return
            break


def download_menu():
    lang_answer = ask("\nDownload voices for: 1 = English, 2 = French : ")
    lang = {"1": "en", "2": "fr"}.get(lang_answer)
    if not lang:
        return
    rows = catalog_voices(lang)
    if not rows:
        print("No voices found.")
        return
    print(f"\n{LANGS[lang]} voices available (single-speaker voices only):")
    print("  Quality: x_low/low = smaller and simpler, medium = good balance, high = best but larger\n")
    for i, (vid, quality, region, size) in enumerate(rows, 1):
        size_txt = f"{size / 1_000_000:.0f} MB" if size else "?"
        status = "installed" if is_installed(vid) else size_txt
        print(f"  {i:>3}) {vid:<42} {quality:<7} {region:<3} [{status}]")
    answer = ask("\nType the numbers to download (example: 2 5 9), or press Enter to go back: ")
    for token in answer.replace(",", " ").split():
        if token.isdigit() and 1 <= int(token) <= len(rows):
            vid = rows[int(token) - 1][0]
            if is_installed(vid):
                print(f"\n{vid}: already installed.")
                continue
            print(f"\nDownloading {vid} ...")
            if download_voice(vid):
                print(f"   Done. Choose it with option {1 if lang == 'en' else 2} to listen and select.")


def main():
    print("=" * 60)
    print(" Live Translator - voice manager")
    print("=" * 60)
    while True:
        print(f"\nCurrent voices:  English = {current_id('en')}   |   French = {current_id('fr')}")
        print("\n  1) Choose the ENGLISH voice (listen first)")
        print("  2) Choose the FRENCH voice (listen first)")
        print("  3) Download more voices")
        print("  4) Quit")
        c = ask("\nYour choice (1-4): ")
        if c == "1":
            choose_voice("en")
        elif c == "2":
            choose_voice("fr")
        elif c == "3":
            download_menu()
        elif c == "4":
            print("\nDone. Start the translator with:  python app.py")
            return


if __name__ == "__main__":
    main()
