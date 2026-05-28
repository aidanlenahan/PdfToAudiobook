# PdfToAudiobook — Refactor TODO

Tracking the full rewrite from 4 separate scripts into a single unified `main.py`.

---

## Done

- [x] Create `requirements.txt` with all dependencies (PyMuPDF, jenkspy, TTS, pydub, torch, ebooklib, beautifulsoup4, lxml, rich)
- [x] Write `main.py` — unified script with rich CLI menu
- [x] File picker: lists supported books in CWD, accepts number or typed path
- [x] EPUB extractor using `ebooklib` + `BeautifulSoup` (maps h1→header, h2/h3/h4→caption, p→body)
- [x] TXT extractor (double-newline paragraph splitting → all body)
- [x] Markdown extractor (# → header, ##/### → caption, body strips inline formatting)
- [x] PDF extractor ported from `extract_text.py` (table removal, font-size filtering, page-number skip)
- [x] PDF classification ported from `classify.py` (Jenks natural breaks, 4 classes)
- [x] TTS generation ported from `tts.py` (chunk splitting, pause durations, resume support)
- [x] Output folder: `./[book stem]/` created automatically, all files go inside
- [x] Resume logic: checks existing `block_N.wav` files, skips already-done blocks
- [x] Audio join ported from `join_audios.py` (FFmpeg concat → 192k MP3)
- [x] "Join existing" flow: scans CWD for folders with `block_*.wav`, lists them in menu

---

## Still To Do

### High priority

- [ ] **Install & smoke-test** — run `pip install -r requirements.txt` and try converting a small PDF
- [ ] **Test EPUB extraction** on a real `.epub` file; some EPUBs use non-standard HTML structure — may need to handle `<div>` tags or `<span>` tags that act as paragraphs
- [ ] **Test TXT/MD extraction** — verify paragraph splitting and markdown stripping produce clean TTS input
- [ ] **Delete old scripts** once `main.py` is confirmed working: `extract_text.py`, `classify.py`, `tts.py`, `join_audios.py`
- [ ] **Update README.md** to reflect the new single-script workflow

### Medium priority

- [ ] **Speaker selection** — expose `speaker` and `language` as an interactive setting in the menu (currently hardcoded to "Adde Michal" / "en")
- [ ] **Chunk size setting** — let user change max TTS chunk length (currently hardcoded to 250 chars)
- [ ] **Page range filter for PDF** — option to skip first N pages (front matter, copyright page)
- [ ] **Progress save on interrupt** — if user hits Ctrl+C mid-generation, existing `block_*.wav` files already allow resume, but a clean interrupt handler would print a "safe to resume" message instead of a traceback
- [ ] **EPUB table-of-contents ordering** — `ebooklib` spine order is usually correct, but some EPUBs have `<guide>` elements that reorder chapters; verify chapter order matches the book

### Low priority / nice to have

- [ ] **Settings menu** (option 3 before Exit) — persistent `settings.json` for speaker, language, chunk size, min font size
- [ ] **Batch mode** — convert all supported files in CWD without interactive prompts (`python main.py --batch`)
- [ ] **Voice preview** — play a 5-second sample of a selected speaker before committing to a full conversion
- [ ] **Word document (.docx) support** — add `python-docx` extractor (headings map to header/caption, paragraphs to body)

---

## Dependency Notes

| Package | Why |
|---|---|
| `PyMuPDF` | PDF text + font metadata extraction, table detection |
| `jenkspy` | Jenks natural breaks for PDF font-size classification |
| `TTS` (Coqui) | Local multilingual TTS via xtts_v2 model (~1.8 GB download) |
| `pydub` | WAV audio manipulation and concatenation |
| `torch` | Required by Coqui TTS; enables CUDA GPU acceleration |
| `ebooklib` | EPUB parsing |
| `beautifulsoup4` | HTML parsing for EPUB chapter content |
| `lxml` | Fast HTML parser backend for BeautifulSoup |
| `rich` | Styled CLI panels, progress bars, menus |
| `ffmpeg` | External binary (not pip) — must be in PATH; used for final MP3 encode |

---

## Quick-Start After Installing

```bash
pip install -r requirements.txt
python main.py
```

Select option `[1]`, pick your book, and the script will:
1. Extract text
2. Classify blocks (PDF only)
3. Generate audio chunks into `./[book-name]/`
4. Optionally join everything into `./[book-name]/audiobook.mp3`
