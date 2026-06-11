# PdfToAudiobook — Product Requirements Document

**Version:** 2.3.0  
**Last updated:** 2026-06-11  
**Repository:** [aidanlenahan/PdfToAudiobook](https://github.com/aidanlenahan/PdfToAudiobook)  
**Forked from:** [Estikno/PdfToAudiobook](https://github.com/Estikno/PdfToAudiobook)

---

## 1. Overview

PdfToAudiobook is a **fully local, offline audiobook generator** that converts written books into narrated MP3/FLAC files using AI text-to-speech — no API keys, no cloud services, no per-use cost. It runs entirely on the user's machine.

It offers **two TTS engines, chosen at runtime**: **Piper** (fast neural TTS that runs several times faster than real-time on CPU) and **Coqui XTTS v2** (highest quality / most natural, but slow on CPU). On machines without an NVIDIA GPU, Piper is the recommended default.

The product is a single Python script (`main.py`) with a rich terminal interface. It accepts PDF, EPUB, TXT, and Markdown files and produces one or more WAV audio parts that can be joined into a final MP3 or FLAC. User preferences (default engine/voice, output directory, audio quality, narration speed, pauses, split behaviour) are stored in `prefs.json` and editable from an in-app **Settings** menu.

---

## 2. Problem Statement

### Original project limitations (Estikno/PdfToAudiobook)
The upstream project shipped four separate scripts that had to be run manually in sequence:

1. `extract_text.py` — extract text from a hardcoded `book.pdf`
2. `classify.py` — classify blocks using Jenks natural breaks
3. `tts.py` — generate per-block WAV files into a `temp/` folder
4. `join_audios.py` — concatenate all WAVs into a single MP3

**Pain points:**
- Users had to edit Python source files to change the input filename
- No format support beyond PDF
- No progress indicators or ETAs during the multi-hour TTS step
- One WAV file per text block (hundreds of files) created choppy, fragmented audio
- Most of a book could be silently skipped if the Jenks classifier over-labelled text as `"other"`
- No graceful interrupt handling — Ctrl-C printed a traceback and left FFmpeg processes running
- No `requirements.txt`; dependency installation was undocumented
- The package required Python ≤3.11 (official Coqui TTS was abandoned)

---

## 3. Goals

| # | Goal |
|---|---|
| G1 | Replace four scripts with one interactive terminal application |
| G2 | Support PDF, EPUB, TXT, and Markdown as input formats |
| G3 | Let the user pick any file by number or path — no source-code edits |
| G4 | Produce continuous, smooth audio — not hundreds of choppy chunks |
| G5 | Let users define where audio splits into separate files (by page or paragraph) |
| G6 | Show accurate progress % and ETA at every step |
| G7 | Support resuming interrupted sessions at the block level |
| G8 | Work on Python 3.9–3.13, Windows, macOS (arm64), and Linux |
| G9 | Provide a smart installer that handles PyTorch variant selection |
| G10 | Exit gracefully on Ctrl-C or `q` — never show a raw traceback |
| G11 | Offer a fast CPU-friendly engine (Piper) alongside XTTS, selectable at runtime |
| G12 | Let users pick from multiple voices, with speed/quality trade-offs surfaced |
| G13 | Persist user preferences (`prefs.json`) editable via an in-app Settings menu |
| G14 | Configurable output directory and audio format/quality (MP3 128/192/320, FLAC) |
| G15 | Support user-defined split points for every format (page/chapter/section/paragraph) |
| G16 | Validate all config and inputs gracefully — never crash on bad values |

### Non-goals (v2.x)
- GUI application (this is a terminal tool by design)
- Cloud TTS or API-based narration
- Real-time streaming of audio during generation
- Android / iOS support
- Video output
- Multi-speaker audiobooks

---

## 4. Users

**Primary user:** A reader who wants to listen to a book during commutes, exercise, or chores, and is comfortable running a Python script from a terminal.

**Technical profile:** Basic Python knowledge (can run `pip install`, can navigate a terminal). Does not need to read or edit source code during normal use.

**Hardware expectations:** A modern PC or Mac. GPU (NVIDIA CUDA or Apple MPS) dramatically speeds up TTS generation but is not required — CPU works, just slower.

---

## 5. Features

### 5.1 Unified terminal menu (implemented)

A single entry point (`python main.py`) presents a rich-styled CMD menu:

```
╔══════════════════════════════════════════════════════╗
║           PdfToAudiobook  v2.3.0                     ║
║         Local AI Audiobook Generator                 ║
║       PDF · EPUB · TXT · Markdown                    ║
╚══════════════════════════════════════════════════════╝

  MAIN MENU
  ┌──────────────────────────────────────────────────┐
  │  [1]  Convert Book to Audiobook                  │
  │  [2]  Join Audio Parts  →  Audiobook             │
  │  [3]  Settings                                   │
  │  [4]  Exit                                       │
  │                                                  │
  │  [q]  Quit                                       │
  └──────────────────────────────────────────────────┘
```

All interaction is driven from this menu. No source-code editing required.

---

### 5.2 File picker (implemented)

On entering the Convert flow, the script scans the configured **input directory** (`input_dir`, default `input/`) for supported files and presents a numbered list:

```
  #    Filename                        Type
  ─────────────────────────────────────────
  1    my-book.pdf                      PDF
  2    another-book.epub               EPUB
  3    notes.md                          MD
```

The user may:
- Enter a number to select a listed file
- Type or drag-and-drop a full file path for a file elsewhere on the system
- Type `q` to return to the quit confirmation

**Supported extensions:** `.pdf` `.epub` `.txt` `.md`

---

### 5.3 Multi-format text extraction (implemented)

| Format | Extractor | How labels are assigned |
|--------|-----------|------------------------|
| **PDF** | PyMuPDF (`fitz`) | Jenks natural breaks on font size → `header` / `caption` / `body` / `other` |
| **EPUB** | `ebooklib` + BeautifulSoup | HTML tags: `h1`→header, `h2/h3/h4`→caption, `p`→body |
| **TXT** | stdlib `re` | All paragraphs (double-newline split) → body |
| **MD** | Regex | `#`→header, `##/###`→caption, paragraphs→body (strips `**bold**`, `*italic*`, `` `code` ``, `[link](url)`) |

**PDF-specific details:**
- Tables are removed via PyMuPDF redaction before text extraction to prevent table noise in narration
- Standalone integers (page numbers) are filtered out
- Text below `MIN_FONT_SIZE = 2` is ignored
- Jenks classification uses 4 clusters; falls back to all-body if font-size variance is too low
- A post-classification check warns and offers an override if >50 % of blocks are classified as `other`

Each extracted block is stored in `classified_text.json` inside the output folder so the extraction step only runs once.

---

### 5.4 Output folder organisation (implemented)

Every conversion creates a dedicated folder named after the book (file stem, no extension):

```
PdfToAudiobook/
└── My Book Title/
    ├── classified_text.json      ← extracted & labelled text (persists for resume)
    ├── generation_config.json    ← break points & format (persists for resume)
    ├── _p01b0000.wav             ← temp block WAVs (deleted after each part assembles)
    ├── _p01b0001.wav
    ├── part_01.wav               ← assembled audio part
    ├── part_02.wav
    └── audiobook.mp3             ← final joined MP3
```

No files are written to the script directory itself.

---

### 5.5 User-defined audio split points (implemented)

Supported for **all formats**, with the split *unit* depending on format and prefs:

| Format | Default unit | Alternate unit (prefs) |
|--------|--------------|------------------------|
| PDF | page number | — |
| EPUB | chapter (`epub_split=chapter`) | paragraph/block (`epub_split=paragraph`) |
| Markdown | heading section (`md_split=heading`) | paragraph/block (`md_split=paragraph`) |
| TXT | paragraph | — |

For Markdown, `md_heading_marker` controls section depth: `#` starts a new section
only at top-level headings, `##` also at subsections, `###` also at sub-subsections.

After extraction the user is asked (example for PDF):

```
  This PDF has 312 pages.
  Enter page numbers where the audio should split into separate files.
  Example: 100, 200  →  3 files (pages 1-100 · 101-200 · 201-312)
  Press Enter with no input to produce one continuous audio file.

  Split at pages:
```

Validation rules:
- Integers only (no floats, no ranges)
- Must be between 1 and (total − 1) inclusive
- No duplicates allowed
- A preview is shown before confirmation

The resulting break points are saved to `generation_config.json` so a resumed session reuses the same splits automatically.

---

### 5.6 TTS audio generation (implemented)

TTS is abstracted behind an **engine interface** (`PiperEngine` / `XttsEngine`),
chosen at runtime (or via `default_engine` pref). Each engine exposes
`chunks(text)`, `synth(text, path)`, and a `sample_rate`.

**Piper engine (default, fast):**
- Library: `piper-tts` (self-contained `abi3` wheels; bundles its phonemizer)
- Voices download on first use (~60 MB) to `~/.cache/pdftoaudiobook/piper`
- Runs ~5–28× real-time on CPU depending on voice tier (see §5.13)
- Handles long text internally — one synth call per block
- Non-speakable blocks (blank lines, `---`, lone `#`) emit a valid silent WAV

**XTTS engine (highest quality, slow on CPU):**
- Model: `tts_models/multilingual/multi-dataset/xtts_v2` (~1.8 GB first run)
- Speaker: Adde Michal · Language: English
- Text chunked to ≤120 chars (XTTS 400-token limit; auto-halves on overflow)

**Narration speed** (`narration_speed` pref, 0.5–2.0) applies to both engines
(Piper `length_scale`, XTTS `speed`).

**Pause durations** after each block come from prefs
(`pause_header_ms` / `pause_caption_ms` / `pause_body_ms`, default 1000/500/200).

**Audio file architecture:**

1. Each text block → temp WAV `_p01b0000.wav` (inter-chunk pauses via pydub)
2. All block WAVs for a part → `part_01.wav` via `ffmpeg -c copy` (lossless, fast)
3. All parts → `audiobook.mp3` (`libmp3lame`, 128/192/320 kbps) or `audiobook.flac`

This eliminates the "hundreds of tiny WAV joins" problem from the original project
and produces a continuous, natural-sounding stream within each part.

---

### 5.7 Session resume (implemented)

Interrupted sessions resume automatically at the next run:

- **Part level:** If `part_NN.wav` already exists, it is skipped entirely
- **Block level:** If `_p01b0000.wav` already exists, that block is skipped and the WAV is reused in assembly
- The user is shown how many blocks were already completed and how many remain
- If the user declines to resume, all audio files and config are cleared and a fresh run begins

---

### 5.8 Progress indicators (implemented)

Every operation shows a live rich progress bar with:

| Column | Content |
|---|---|
| Spinner | Animated activity indicator |
| Description | Current operation name |
| Bar | Visual fill (36 chars wide) |
| N/M count | e.g. `142/312` |
| Percentage | e.g. `46%` |
| Elapsed | Time since start |
| ETA | Estimated time remaining based on current speed |

**Coverage:**

| Step | Display |
|---|---|
| PDF page scan | Progress bar over pages |
| EPUB chapter scan | Progress bar over chapter items |
| Font-size classification | Progress bar over blocks |
| Classification override | Progress bar over blocks |
| TTS synthesis | **Two simultaneous bars:** overall book progress + current part |
| Block WAV assembly | Spinner (FFmpeg `-c copy` is near-instant) |
| MP3 encoding | Real-time FFmpeg progress via `-progress pipe:1` thread reader |

The FFmpeg MP3 encoding bar shows actual seconds-encoded / total-seconds so the ETA reflects true encode speed, not an estimate.

---

### 5.9 Graceful exit (implemented)

**`q` at any menu or file picker:**
```
  Quit? [y/N]:
```
Typing `n` or pressing Enter returns to the previous prompt. Typing `y` exits cleanly.

**Ctrl-C at a menu or prompt:**  
Triggers the same `Quit? [y/N]` confirmation. A second Ctrl-C at that prompt also confirms.

**Ctrl-C during TTS synthesis:**  
```
  Interrupted.  Synthesised blocks are saved — run again to resume.

  Goodbye!
```
The progress bar closes cleanly (context manager), temp WAVs from completed blocks are retained for resume, and no traceback is printed.

**Ctrl-C during FFmpeg encode:**  
The FFmpeg subprocess is terminated via `proc.terminate()` before the process exits, preventing orphaned encode jobs.

**Top-level handler in `main()`:**  
Any `KeyboardInterrupt` or `EOFError` that bubbles all the way up (e.g., from broken pipe or piped input) is caught and converted to a clean goodbye message + `sys.exit(0)`.

---

### 5.10 Join audio parts (implemented)

Menu option [2] scans the current directory for subfolders containing `part_*.wav` files and presents them as a numbered list with chunk counts and existing-MP3 status. The selected folder's parts are joined into `audiobook.mp3` via FFmpeg with the same real-time progress bar as the conversion flow.

Backward compatibility: folders with the old `block_*.wav` naming (from pre-v2 runs) are also detected and joined correctly.

---

### 5.11 Cross-platform installer (implemented)

`python install.py` handles platform-specific PyTorch installation before `pip install -r requirements.txt`:

| Detection method | Action |
|---|---|
| `platform.system() == "Darwin"` and `machine == "arm64"` | PyPI torch (includes MPS for Apple Silicon) |
| `nvidia-smi` in PATH + `nvcc` version | pytorch.org CUDA wheels (`cu118` / `cu121` / `cu124`) |
| `nvidia-smi` without `nvcc` | Interactive CUDA version menu |
| Fallback | pytorch.org CPU-only wheels |

After installation, `ffmpeg` availability is checked and OS-specific install instructions are printed if missing.

**Explicitly unsupported:** macOS Intel (x86_64) — `torchcodec` (required by `coqui-tts[codec]`) has no macOS Intel wheels.

---

### 5.12 Preferences & Settings menu (implemented)

User preferences live in `prefs.json` (next to `main.py`) and are editable from
**main menu → [3] Settings**. Each setting is self-documented via an inline
`_help` block in the file. The Settings menu shows every setting, its current
value, and a description; users can edit any value, reset all to defaults, or go back.

**Robust config handling:** invalid JSON is backed up (`prefs.json.bak`) and
recreated with defaults; individual out-of-range/invalid values are reset to
their default with a warning. Bad config never crashes the app.

| Setting | Default | Meaning |
|---|---|---|
| `default_engine` | *(blank = ask)* | `piper` / `xtts` |
| `default_voice` | *(blank = ask)* | Piper voice key |
| `input_dir` | `input` | folder the picker lists books from; blank = `input/` |
| `output_dir` | `output` | base output dir; blank = ask each run |
| `audio_format` | `mp3` | `mp3` / `flac` |
| `mp3_bitrate` | `192k` | `128k` / `192k` / `320k` |
| `narration_speed` | `1.0` | 0.5–2.0 multiplier |
| `auto_join` | `true` | auto-join parts when generation finishes |
| `pause_header_ms` / `pause_caption_ms` / `pause_body_ms` | `1000`/`500`/`200` | per-block silence |
| `epub_split` | `chapter` | `chapter` / `paragraph` |
| `md_split` | `heading` | `heading` / `paragraph` |
| `md_heading_marker` | `#` | section depth for MD heading splits |

---

### 5.13 TTS engines & voice selection (implemented)

After break points, the user picks an **engine** (unless `default_engine` is set),
and for Piper a **voice** (unless `default_voice` is set). The voice picker lists
8 curated voices and accepts any Piper catalog key.

Measured Piper speed on CPU (AMD Ryzen 7 6800H) by quality tier:

| Tier | Sample rate | Speed | Quality |
|------|-------------|-------|---------|
| `low` | 16 kHz | ~28× real-time | fastest, lower fidelity |
| `medium` | 22 kHz | ~22× real-time | balanced (recommended) |
| `high` | 22 kHz | ~5× real-time | best, larger model |

XTTS on the same CPU runs slower than real-time (RTF ≈ 2), i.e. even `high`-tier
Piper is ~40× faster. The chosen engine/voice/speed are saved per book in
`generation_config.json` so resumed audio stays consistent.

---

### 5.14 Configurable input & output directories (implemented)

**Input** (`input_dir`, default `input`): the file picker lists supported books
from this folder (users drop files there). Blank → `input/`. The user can still
type a full path to any file.

**Output** (`output_dir`, default `output`): audio goes to
`<output_dir>/<book name>/`. Blank → the user is prompted each run (default
`output`).

**Directory policy (shared by input & output):**
- The **default** `input/` / `output/` folders are **auto-created** if missing.
- A **custom** folder that doesn't exist produces a friendly error and returns to
  the menu — it is *not* silently created.
- Non-directory paths and permission errors are reported cleanly (output is
  write-probed before generation). No raw tracebacks.

**First run:** if `prefs.json` doesn't exist, the app prints *"No user
preferences detected — starting from scratch"* and writes a default `prefs.json`.

---

## 6. Technical Architecture

### 6.1 Component map

```
main.py
├── Preferences layer
│   ├── PREFS_SCHEMA         Settings + defaults + validators + help text
│   ├── load_prefs()         Validate/coerce prefs.json (graceful on bad config)
│   └── save_prefs()         Write prefs.json with inline _help block
│
├── UI layer
│   ├── _banner()            Rich double-border header
│   ├── _menu()              Numbered + [q] quit menu
│   ├── _pick_file()         File browser + path input
│   ├── ask_engine() / ask_piper_voice() / ask_output_dir() / ask_break_points()
│   ├── _confirm_quit()      y/n confirmation for q / Ctrl-C
│   ├── StackedProgress      Live label-above-bar display w/ smoothed ETA
│   └── _make_progress()     Shared progress factory
│
├── Extraction layer
│   ├── _extract_pdf()       PyMuPDF → blocks with page + font_size
│   ├── _classify_pdf()      Jenks breaks → other/caption/body/header labels
│   ├── _extract_epub()      ebooklib + BeautifulSoup → blocks (+chapter)
│   ├── _extract_txt()       Paragraph split → body blocks
│   └── _extract_md()        Heading/paragraph parse → blocks (+heading_level)
│
├── Grouping layer
│   ├── _assign_units()      Per-block split unit (page/chapter/section/index)
│   ├── _break_unit_info()   (total_units, unit_name) for the prompt
│   └── _group_into_parts()  Split narrated blocks by user break points
│
├── TTS engine layer
│   ├── PiperEngine          Fast engine (length_scale = speed); auto voice download
│   ├── XttsEngine           XTTS v2 (120-char chunker, speed)
│   ├── _load_engine()       Construct chosen engine w/ messaging + ImportError help
│   └── generate_audio()     Two-level progress, per-block temp WAVs, FFmpeg assembly
│
├── FFmpeg layer
│   ├── _concat_wavs()       -c copy assembly
│   ├── _wav_duration_s()    stdlib wave duration reader
│   └── _ffmpeg_progress()   -progress pipe:1 thread reader → live bar
│
└── Flow controllers
    ├── _flow_convert()      Full conversion pipeline
    ├── _flow_join()         Standalone join (mp3/flac, nested-dir scan)
    ├── _flow_settings()     Edit prefs.json in-app
    └── main()               Menu loop + per-flow error handling (error.log)
```

### 6.2 Data flow

```
Input file
    │
    ▼  _extract_*()
classified_text.json  ←─── saved to disk (enables resume)
    │
    ▼  _group_into_parts()
[(part_01, [block, block, …]), (part_02, […]), …]
    │
    ▼  generate_audio()  [for each part]
    │
    ├── tts.tts_to_file() × N chunks  →  _p01b0000_c000.wav  (temp, deleted)
    ├── pydub merge chunks + pauses   →  _p01b0000.wav        (block temp)
    │   …
    └── ffmpeg -c copy                →  part_01.wav          (part WAV)

[all parts complete]
    │
    ▼  join_audio()
ffmpeg libmp3lame 192k               →  audiobook.mp3
```

### 6.3 Key constants

| Constant | Value | Purpose |
|---|---|---|
| `VERSION` | `"2.3.0"` | Shown in banner |
| `SUPPORTED_FORMATS` | `{".pdf", ".epub", ".txt", ".md"}` | File picker filter |
| `PAUSES_MS` | `1000/500/200` | Default per-block silence (overridable via prefs) |
| `DEFAULT_PIPER_VOICE` | `"en_US-lessac-medium"` | Default Piper voice |
| `PIPER_CACHE` | `~/.cache/pdftoaudiobook/piper` | Downloaded Piper voice models |
| `PREFS_PATH` | `<app>/prefs.json` | User preferences file |
| `MIN_FONT_SIZE` | `2` | PDF spans below this are ignored |
| XTTS chunk max | `120` chars | Input limit for XTTS 400-token cap |
| Default MP3 bitrate | `192k` | Overridable via prefs |
| XTTS speaker / language | `"Adde Michal"` / `"en"` | XTTS defaults |

---

## 7. Dependency Stack

| Package | Role | Notes |
|---|---|---|
| `PyMuPDF` (fitz) | PDF parsing | Table detection, font metadata |
| `jenkspy` | Jenks natural breaks | PDF font-size classification |
| `piper-tts` | Piper TTS engine (default) | Self-contained `abi3` wheels; bundles phonemizer; pulls `onnxruntime` |
| `torch` | Deep learning runtime | XTTS only; install before `coqui-tts`; CUDA/MPS/CPU variant selected by `install.py` |
| `torchaudio` | Audio I/O for torch | Must match torch version |
| `transformers` `>=4.40,<5.0` | Transformer models | Pinned to 4.x; coqui-tts uses `isin_mps_friendly` removed in v5 |
| `coqui-tts[codec]` | XTTS v2 TTS engine | Community fork of abandoned Coqui TTS; `[codec]` pulls in `torchcodec` |
| `pydub` | WAV manipulation | Inter-chunk pause insertion |
| `ebooklib` | EPUB parsing | Chapter item iteration |
| `beautifulsoup4` + `lxml` | HTML parsing | EPUB chapter content |
| `rich` | Terminal UI | Progress bars, panels, tables, prompts |
| `ffmpeg` (external binary) | Audio concatenation + MP3 encoding | Must be in PATH; not a pip package |

---

## 8. Platform Support Matrix

| Platform | CPU | CUDA GPU | Notes |
|---|---|---|---|
| Windows x86_64 | ✅ | ✅ | `install.py` auto-detects CUDA |
| macOS arm64 (Apple Silicon) | ✅ | ✅ MPS | PyPI torch includes MPS |
| macOS x86_64 (Intel) | ❌ | ❌ | `torchcodec` has no Intel Mac wheels |
| Linux x86_64 | ✅ | ✅ | |
| Linux aarch64 | ✅ | ✅ | |
| Python 3.9–3.13 | ✅ | | Tested on 3.13; 3.9–3.12 expected to work |
| Python 3.8 or older | ❌ | | `coqui-tts` minimum is 3.9 |
| Python 3.14+ | ⚠️ | | Untested; may work |

---

## 9. File Reference

| File | Purpose |
|---|---|
| `main.py` | Entire application — extraction, TTS engines, joining, settings, UI |
| `install.py` | Smart cross-platform setup script |
| `requirements.txt` | Python dependencies (excludes torch; see install.py) |
| `prefs.json` | User preferences (git-ignored; auto-created with `_help` block) |
| `TODO.md` | Developer task tracking |
| `PRD.md` | This document |

Legacy scripts (`extract_text.py`, `classify.py`, `tts.py`, `join_audios.py`) were
**removed in v2.3** — all functionality lives in `main.py`.

---

## 10. Known Limitations

| ID | Limitation | Severity | Notes |
|---|---|---|---|
| L1 | macOS Intel unsupported | High | `torchcodec` has no macOS x86_64 wheels |
| L2 | `transformers<5.0` pin | Medium | `isin_mps_friendly` removed in v5; will conflict if another package requires v5 |
| L3 | XTTS speaker/language hardcoded | Low | Piper now has full voice selection; XTTS still fixed to Adde Michal / English (OQ6) |
| L4 | No TTS chunk-size setting | Low | XTTS hardcoded 120 chars; Piper chunks per block internally |
| L5 | EPUB chapter order | Low | Relies on spine order from `ebooklib`; some EPUBs use `<guide>` reordering |
| L6 | `pymupdf_layout` notice | Cosmetic | PyMuPDF prints an advisory about installing `pymupdf_layout`; does not affect output |
| L7 | Part-level resume only for interrupted parts | Low | If interrupted mid-part, the whole part restarts (block-level resume works within a session) |
| L8 | No progress on TTS model download | Low | First-run model download shows a spinner; the ~1.8 GB download has no per-byte progress |

---

## 11. Roadmap

### v2.3 — Engines, voices & settings (shipped)
- [x] Add Piper as a fast, CPU-friendly engine alongside XTTS (runtime choice)
- [x] Voice selection — 8 curated Piper voices + any catalog key
- [x] Settings menu (option [3]) backed by `prefs.json` with inline `_help`
- [x] Configurable input & output directories (`input/`, `output/<book>/`) with
      create-default / error-on-missing-custom policy and a first-run setup message
- [x] Configurable audio format/quality (MP3 128/192/320, FLAC)
- [x] Narration speed and per-block pause durations as prefs
- [x] User-defined split points for EPUB (chapter) and Markdown (heading section)
- [x] Graceful config validation — bad values reset, no crashes
- [x] Delete legacy scripts; rewrite `README.md`
- [ ] Suppress the `pymupdf_layout` advisory message
- [ ] Narration language pref for XTTS (OQ6)

### v2.4 — Format & processing
- [ ] Word document (`.docx`) support via `python-docx`
  - Heading styles → header/caption labels
  - Paragraph text → body
- [ ] PDF page-range filter (skip first N pages: cover, TOC, copyright)
- [ ] EPUB: validate and correct chapter order via `<spine>` and `<guide>` elements
- [ ] Batch mode: `python main.py --batch` converts all supported files in CWD non-interactively

### v2.5 — UX improvements
- [ ] Voice preview: synthesise 5-second sample before committing to a full conversion
- [ ] Show estimated total generation time after extraction (based on block count × avg seconds/block from recent runs)
- [ ] Save per-book generation speed to `~/.config/pdftoaudiobook/stats.json` to improve estimates
- [ ] `--resume` CLI flag to skip the interactive resume prompt and always resume
- [ ] Named output profile: `--output-dir /path/to/dir` to override the default beside-script folder

### v3.0 — Multi-voice & quality
- [ ] Chapter-level voice assignment (e.g., narrator vs. character voices for fiction)
- [ ] Configurable per-chapter speaker
- [ ] Support additional TTS backends (e.g., Kokoro-ONNX for faster CPU inference)
- [ ] Optional noise reduction pass on generated audio (via `noisereduce` or `resemble-enhance`)

---

## 12. Installation Quick Reference

```bash
# 1. Clone
git clone https://github.com/aidanlenahan/PdfToAudiobook.git
cd PdfToAudiobook

# 2. Install (handles PyTorch variant selection automatically)
python install.py

# 3. Install ffmpeg (if not already present)
#    Windows:  winget install ffmpeg
#    macOS:    brew install ffmpeg
#    Linux:    sudo apt install ffmpeg

# 4. Run
python main.py
```

**Manual CPU-only install** (skip install.py):
```bash
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt
```

---

## 13. Resolved Decisions (v2.3)

| # | Question | Decision |
|---|---|---|
| OQ1 | Keep or delete the legacy scripts? | **Deleted** in v2.3. All functionality is in `main.py`. |
| OQ2 | Fixed default voice, or prompt for selection? | **Prompt by default.** `default_voice` (and `default_engine`) prefs can pre-set a choice to skip the prompt; blank = always ask. Voice selection added (8 curated + any catalog key). |
| OQ3 | Output beside input, or a configurable directory? | **Configurable `output_dir` pref**, default `output/`. A per-book subfolder is created inside it. Blank = prompt each run. Includes permission/invalid-path error handling. |
| OQ4 | Fixed 192 kbps MP3, or user choice? | **Configurable.** `audio_format` (`mp3`/`flac`) and `mp3_bitrate` (`128k`/`192k`/`320k`) prefs; default 192 kbps MP3. All config is validated (out-of-bounds values reset to defaults with a warning, no crash). |
| OQ5 | Break points for EPUB/Markdown too? | **Yes.** EPUB splits by chapter (or paragraph), Markdown by heading section (or paragraph), with a configurable `md_heading_marker` for section depth. See §5.5. |

### Remaining open questions

| # | Question | Owner | Status |
|---|---|---|---|
| OQ6 | Should narration language (non-English) be exposed as a pref / per-voice for XTTS? | @aidanlenahan | Open |
| OQ7 | Should `prefs.json` live next to `main.py` (current) or in a per-user config dir (`~/.config`)? | @aidanlenahan | Open |
