# PdfToAudiobook

Convert **PDF, EPUB, TXT, and Markdown** books into **AI-narrated audiobooks** —
entirely locally. No API keys, no credits, no cloud.

A single menu-driven script handles everything: pick a file, choose a voice,
split into parts where you want, generate, and join into one MP3 or FLAC.

---

## ✨ Features

- **Two TTS engines, chosen at runtime**
  - **Piper** — fast neural TTS; runs *several times faster than real-time* on CPU.
    The right choice when you don't have an NVIDIA GPU.
  - **XTTS v2** (Coqui) — highest quality / most natural, but slow on CPU.
- **8 curated Piper voices** (US/GB, male/female) plus any voice key from the
  [Piper catalog](https://huggingface.co/rhasspy/piper-voices). Voices download
  on first use and are cached.
- **User-defined split points** for every format — by page (PDF), chapter (EPUB),
  heading section (Markdown), or paragraph (TXT).
- **Resume** — interrupt any time (Ctrl-C or `q`); rerun to continue where you left off.
- **Live progress** with smoothed ETA and estimated output size for every step.
- **Preferences** (`prefs.json`) editable in-app via the **Settings** menu —
  default voice/engine, output directory, audio format & quality, narration
  speed, pause lengths, and how EPUB/MD are split.
- **Join** parts into a single `audiobook.mp3` (128/192/320 kbps) or `audiobook.flac`.

---

## 🚀 Quick start

```bash
# 1. Install (auto-detects OS/GPU and installs the right PyTorch for XTTS)
python install.py

# 2. Run
python main.py
```

Then in the menu:

1. **Convert Book to Audiobook** — pick your file, choose where to split, pick an
   engine/voice, and let it generate. You'll be offered (or auto-)joined output.
2. **Join Audio Parts → Audiobook** — combine an existing folder's parts into one file.
3. **Settings** — edit preferences (see below).

Put your book file in the folder you run `main.py` from (or type a full path).
Output goes to `output/<book name>/` by default.

---

## ⚙️ Settings (`prefs.json`)

Open **Settings** from the main menu, or edit `prefs.json` directly (each setting
is documented inline in the file's `_help` block). Invalid values are safely
reset to defaults — bad config never crashes the app.

| Setting | Default | Meaning |
|---------|---------|---------|
| `default_engine` | *(blank)* | `piper`/`xtts`, or blank to be asked each run |
| `default_voice` | *(blank)* | a Piper voice key, or blank to be asked each run |
| `output_dir` | `output` | where audiobook folders are created; blank = ask each run |
| `audio_format` | `mp3` | `mp3` or `flac` for the joined file |
| `mp3_bitrate` | `192k` | `128k` / `192k` / `320k` (ignored for FLAC) |
| `narration_speed` | `1.0` | speed multiplier, 0.5–2.0 (0.9 slower, 1.15 faster) |
| `auto_join` | `true` | auto-join parts when generation finishes |
| `pause_header_ms` / `pause_caption_ms` / `pause_body_ms` | `1000`/`500`/`200` | silence after each block type |
| `epub_split` | `chapter` | EPUB split unit: `chapter` or `paragraph` |
| `md_split` | `heading` | Markdown split unit: `heading` or `paragraph` |
| `md_heading_marker` | `#` | section depth for MD heading splits (`#`=top-level only, `###`=also sub-subsections) |

A per-book subfolder (named after the file) is always created inside `output_dir`.

---

## 🎙️ Choosing a voice & engine

On CPU (no NVIDIA GPU), **Piper is dramatically faster** — roughly 5–28× real-time
depending on the voice tier, versus XTTS at slower-than-real-time. Piper tiers:

| Tier | Sample rate | Speed (CPU) | Quality |
|------|------------|-------------|---------|
| `low` | 16 kHz | ~28× real-time | fastest, lower fidelity |
| `medium` | 22 kHz | ~22× real-time | **balanced (recommended)** |
| `high` | 22 kHz | ~5× real-time | best, larger model |

Within a tier, voices run at the same speed — pick by how they sound.

---

## 📦 Dependencies

`python install.py` handles everything. Manual install:

```bash
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu  # for XTTS
pip install -r requirements.txt
```

You also need [**FFmpeg**](https://ffmpeg.org/) on your `PATH` (used for joining).

Core libraries: `piper-tts`, `coqui-tts`, `PyMuPDF`, `jenkspy`, `ebooklib`,
`beautifulsoup4`, `pydub`, `rich`.

---

## ⚠️ Notes

- **First run downloads models**: a Piper voice (~60 MB) or XTTS (~1.8 GB).
- **Resuming**: generation saves progress per block. Stop with Ctrl-C or `q` and
  rerun — it skips finished parts and continues. The engine/voice/speed are saved
  per book so resumed audio stays consistent.
- **GPU**: XTTS uses CUDA automatically if available. AMD/integrated GPUs and
  macOS Intel aren't supported for XTTS — use Piper there.
- `output/`, `prefs.json`, and `error.log` are git-ignored. If something
  unexpected happens, the full traceback is written to `error.log`.
