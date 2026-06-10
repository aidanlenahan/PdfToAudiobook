# PdfToAudiobook — Product Requirements Document

**Version:** 2.2.0  
**Last updated:** 2026-06-10  
**Repository:** [aidanlenahan/PdfToAudiobook](https://github.com/aidanlenahan/PdfToAudiobook)  
**Forked from:** [Estikno/PdfToAudiobook](https://github.com/Estikno/PdfToAudiobook)

---

## 1. Overview

PdfToAudiobook is a **fully local, offline audiobook generator** that converts written books into narrated MP3 files using AI text-to-speech — no API keys, no cloud services, no per-use cost. It runs entirely on the user's machine using the Coqui XTTS v2 model.

The product is a single Python script (`main.py`) with a rich terminal interface. It accepts PDF, EPUB, TXT, and Markdown files and produces one or more WAV audio parts that can be joined into a final MP3.

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
║           PdfToAudiobook  v2.2.0                     ║
║         Local AI Audiobook Generator                 ║
║       PDF · EPUB · TXT · Markdown                    ║
╚══════════════════════════════════════════════════════╝

  MAIN MENU
  ┌──────────────────────────────────────────────────┐
  │  [1]  Convert Book to Audiobook                  │
  │  [2]  Join Audio Parts  →  MP3                   │
  │                                                  │
  │  [q]  Quit                                       │
  └──────────────────────────────────────────────────┘
```

All interaction is driven from this menu. No source-code editing required.

---

### 5.2 File picker (implemented)

On entering the Convert flow, the script scans the current working directory for supported files and presents a numbered list:

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

**Supported for:** PDF (by page number) and TXT (by paragraph number)  
**Not applicable to:** EPUB, Markdown (single output file)

After extraction the user is asked:

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

**Model:** `tts_models/multilingual/multi-dataset/xtts_v2` (Coqui XTTS v2)  
**Default speaker:** Adde Michal (Icelandic female voice)  
**Default language:** English (`"en"`)  
**First-run download:** ~1.8 GB (cached by Coqui's model manager)

**Text chunking:** Each text block is split into segments of ≤250 characters at sentence/clause/word boundaries before TTS synthesis, because XTTS v2 quality degrades on very long inputs.

**Pause durations appended after each block:**

| Block type | Pause |
|---|---|
| `header` | 1 000 ms |
| `caption` | 500 ms |
| `body` | 200 ms |

**Audio file architecture:**

1. Each text block → temp WAV `_p01b0000.wav` (includes inter-chunk pauses via pydub)
2. All block WAVs for a part → `part_01.wav` via `ffmpeg -c copy` (lossless, fast)
3. All parts → `audiobook.mp3` via `ffmpeg libmp3lame` at 192 kbps

This architecture eliminates the "hundreds of tiny WAV joins" problem from the original project and produces a continuous, natural-sounding audio stream within each part.

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

## 6. Technical Architecture

### 6.1 Component map

```
main.py
├── UI layer
│   ├── _banner()            Rich double-border header
│   ├── _menu()              Numbered + [q] quit menu
│   ├── _pick_file()         File browser + path input
│   ├── _confirm_quit()      y/n confirmation for q / Ctrl-C
│   └── _make_progress()     Shared progress bar factory
│
├── Extraction layer
│   ├── _extract_pdf()       PyMuPDF → blocks with page + font_size
│   ├── _classify_pdf()      Jenks breaks → other/caption/body/header labels
│   ├── _extract_epub()      ebooklib + BeautifulSoup → labelled blocks
│   ├── _extract_txt()       Paragraph split → body blocks
│   └── _extract_md()        Heading/paragraph parse → labelled blocks
│
├── Grouping layer
│   └── _group_into_parts()  Split narrated blocks by user break points
│
├── TTS layer
│   ├── _split_text()        250-char sentence-aware chunker
│   └── generate_audio()     Two-level progress, per-block temp WAVs,
│                            FFmpeg assembly per part
│
├── FFmpeg layer
│   ├── _concat_wavs()       -c copy assembly (spinner only)
│   ├── _wav_duration_s()    stdlib wave duration reader
│   └── _ffmpeg_progress()   -progress pipe:1 thread reader → live bar
│
└── Flow controllers
    ├── _flow_convert()      Full conversion pipeline
    └── _flow_join()         Standalone join flow
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
| `VERSION` | `"2.2.0"` | Shown in banner |
| `SUPPORTED_FORMATS` | `{".pdf", ".epub", ".txt", ".md"}` | File picker filter |
| `PAUSES_MS["header"]` | `1000` | Silence after heading blocks |
| `PAUSES_MS["caption"]` | `500` | Silence after caption blocks |
| `PAUSES_MS["body"]` | `200` | Silence after body blocks |
| `MIN_FONT_SIZE` | `2` | PDF spans below this are ignored |
| TTS chunk max | `250` chars | Input limit for XTTS v2 quality |
| MP3 bitrate | `192k` | FFmpeg output quality |
| TTS speaker | `"Adde Michal"` | Default XTTS v2 voice |
| TTS language | `"en"` | Default narration language |

---

## 7. Dependency Stack

| Package | Role | Notes |
|---|---|---|
| `PyMuPDF` (fitz) | PDF parsing | Table detection, font metadata |
| `jenkspy` | Jenks natural breaks | PDF font-size classification |
| `torch` | Deep learning runtime | Must install before `coqui-tts`; CUDA/MPS/CPU variant selected by `install.py` |
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
| `main.py` | Entire application — extraction, TTS, joining, UI |
| `install.py` | Smart cross-platform setup script |
| `requirements.txt` | Python dependencies (excludes torch; see install.py) |
| `TODO.md` | Developer task tracking |
| `PRD.md` | This document |
| `extract_text.py` | Legacy — superseded by `main.py` (pending deletion) |
| `classify.py` | Legacy — superseded by `main.py` (pending deletion) |
| `tts.py` | Legacy — superseded by `main.py` (pending deletion) |
| `join_audios.py` | Legacy — superseded by `main.py` (pending deletion) |

---

## 10. Known Limitations

| ID | Limitation | Severity | Notes |
|---|---|---|---|
| L1 | macOS Intel unsupported | High | `torchcodec` has no macOS x86_64 wheels |
| L2 | `transformers<5.0` pin | Medium | `isin_mps_friendly` removed in v5; will conflict if another package requires v5 |
| L3 | Speaker/language hardcoded | Medium | Defaults to Adde Michal / English; requires source edit to change |
| L4 | No TTS chunk-size setting | Low | Hardcoded 250 chars; some languages may need different values |
| L5 | EPUB chapter order | Low | Relies on spine order from `ebooklib`; some EPUBs use `<guide>` reordering |
| L6 | `pymupdf_layout` notice | Cosmetic | PyMuPDF prints an advisory about installing `pymupdf_layout`; does not affect output |
| L7 | Part-level resume only for interrupted parts | Low | If interrupted mid-part, the whole part restarts (block-level resume works within a session) |
| L8 | No progress on TTS model download | Low | First-run model download shows a spinner; the ~1.8 GB download has no per-byte progress |

---

## 11. Roadmap

### v2.3 — Polish & settings
- [ ] Delete legacy scripts (`extract_text.py`, `classify.py`, `tts.py`, `join_audios.py`)
- [ ] Update `README.md` to reflect single-script workflow
- [ ] Settings menu (option [3] before Exit):
  - Speaker voice selection from available XTTS voices
  - Narration language (`"en"`, `"es"`, `"fr"`, etc.)
  - Max TTS chunk length (default 250)
  - PDF minimum font size (default 2)
  - Pause durations (header / caption / body)
- [ ] Persist settings to `~/.config/pdftoaudiobook/settings.json`
- [ ] Suppress the `pymupdf_layout` advisory message

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

## 13. Open Questions

| # | Question | Owner | Status |
|---|---|---|---|
| OQ1 | Should the legacy scripts be kept indefinitely for reference, or deleted after v2.3 is confirmed stable? | @aidanlenahan | Open |
| OQ2 | Is Adde Michal the right default voice for all users, or should the first-run setup prompt for voice selection? | @aidanlenahan | Open |
| OQ3 | Should the output folder be next to the input file (current) or always in a configurable output directory? | @aidanlenahan | Open |
| OQ4 | Is 192 kbps MP3 the right quality target, or should the user be able to choose (128 / 192 / 320 / FLAC)? | @aidanlenahan | Open |
| OQ5 | Should EPUB and Markdown also support user-defined break points, or is a single output file acceptable? | @aidanlenahan | Open |
