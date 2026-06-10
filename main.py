#!/usr/bin/env python3
"""PdfToAudiobook — Unified local audiobook generator.

Supports: PDF, EPUB, TXT, Markdown
Requires: ffmpeg in PATH  |  see requirements.txt / install.py
"""

import json
import re
import subprocess
import sys
from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

console = Console()
VERSION = "2.1.0"
SUPPORTED_FORMATS = {".pdf", ".epub", ".txt", ".md"}

# Silence (ms) appended after the last TTS chunk of each block
PAUSES_MS = {"header": 1000, "caption": 500, "body": 200}


# ─────────────────────────────── UI helpers ───────────────────────────────────

def _banner():
    console.clear()
    title = Text(justify="center")
    title.append("PdfToAudiobook", style="bold bright_white")
    title.append(f"  v{VERSION}", style="dim white")
    sub  = Text("Local AI Audiobook Generator", style="cyan", justify="center")
    fmt  = Text("PDF  ·  EPUB  ·  TXT  ·  Markdown", style="dim cyan", justify="center")
    console.print(
        Panel(
            Text.assemble("\n", title, "\n", sub, "\n", fmt, "\n"),
            box=box.DOUBLE_EDGE,
            border_style="bright_blue",
            padding=(0, 6),
        )
    )
    console.print()


def _menu(title: str, options: list) -> int:
    t = Table(
        box=box.ROUNDED, border_style="blue",
        show_header=False, padding=(0, 3), min_width=52,
    )
    t.add_column("num", style="bold cyan", justify="right", width=5)
    t.add_column("label", style="white")
    for i, opt in enumerate(options, 1):
        t.add_row(f"[{i}]", opt)
    console.print(Panel(t, title=f"[bold]{title}[/bold]", border_style="blue"))
    while True:
        raw = Prompt.ask("\n  [cyan]Enter choice[/cyan]").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw)
        console.print(f"  [red]Please enter a number between 1 and {len(options)}.[/red]")


def _pick_file() -> Path:
    cwd   = Path.cwd()
    files = sorted(f for f in cwd.iterdir() if f.is_file() and f.suffix.lower() in SUPPORTED_FORMATS)

    if files:
        t = Table(box=box.SIMPLE_HEAD, border_style="dim", show_header=True, padding=(0, 2))
        t.add_column("#",        style="bold cyan", justify="right", width=4)
        t.add_column("Filename", style="white")
        t.add_column("Type",     style="green", justify="center", width=7)
        for i, f in enumerate(files, 1):
            t.add_row(str(i), f.name, f.suffix.upper().lstrip("."))
        console.print(Panel(t, title="[bold]Books in Current Directory[/bold]", border_style="blue"))
        console.print("  [dim]Enter a number, or type a full path.[/dim]\n")
    else:
        console.print("  [yellow]No supported books found in current directory.[/yellow]")
        console.print("  [dim]Type the full path to your book file.[/dim]\n")

    while True:
        raw = Prompt.ask("  [cyan]Select file[/cyan]").strip().strip('"').strip("'")
        if raw.isdigit() and files and 1 <= int(raw) <= len(files):
            return files[int(raw) - 1]
        p = Path(raw)
        if p.is_file():
            if p.suffix.lower() in SUPPORTED_FORMATS:
                return p
            console.print(
                f"  [red]Unsupported format '{p.suffix}'. "
                f"Supported: {', '.join(sorted(SUPPORTED_FORMATS))}[/red]"
            )
        else:
            console.print(f"  [red]File not found: {raw}[/red]")


# ─────────────────────────────── Break-point prompt ───────────────────────────

def ask_break_points(total_units: int, unit_name: str = "page") -> list:
    """
    Ask user where to split the audio output.
    Returns a sorted list of integers (each is the *last* unit in that part),
    or [] to produce a single audio file.

    Example with total_units=300, unit_name="page":
        Input : 100, 200
        Output: [100, 200]
        Effect: part_01 → pages 1-100
                part_02 → pages 101-200
                part_03 → pages 201-300
    """
    format_word = "PDF" if unit_name == "page" else "file"
    console.print(
        f"\n  [dim]This {format_word} has [bold]{total_units}[/bold] {unit_name}s.[/dim]"
    )
    console.print(
        f"  [dim]Enter {unit_name} numbers where the audio should split into separate files.[/dim]"
    )
    console.print(
        f"  [dim]Example: [bold]100, 200[/bold]  →  3 files "
        f"({unit_name}s 1-100 · 101-200 · 201-{total_units})[/dim]"
    )
    console.print("  [dim]Press Enter with no input to produce one continuous audio file.[/dim]\n")

    while True:
        raw = Prompt.ask(f"  [cyan]Split at {unit_name}s[/cyan]", default="").strip()
        if not raw:
            return []

        try:
            points = [int(x.strip()) for x in raw.split(",") if x.strip()]
        except ValueError:
            console.print("  [red]Only integers separated by commas are accepted.[/red]")
            continue

        if not points:
            return []

        out_of_range = [p for p in points if not (1 <= p < total_units)]
        if out_of_range:
            console.print(
                f"  [red]Out of range: {out_of_range}. "
                f"Values must be between 1 and {total_units - 1}.[/red]"
            )
            continue

        if len(points) != len(set(points)):
            console.print("  [red]Duplicate values are not allowed.[/red]")
            continue

        sorted_points = sorted(points)

        # Show preview
        console.print(f"\n  [green]Will create {len(sorted_points) + 1} audio file(s):[/green]")
        prev = 0
        for i, bp in enumerate(sorted_points + [total_units]):
            console.print(f"  [dim]  Part {i + 1:>2}: {unit_name}s {prev + 1} – {bp}[/dim]")
            prev = bp
        console.print()

        if Confirm.ask("  [cyan]Use these split points?[/cyan]", default=True):
            return sorted_points

        console.print()  # loop again if user said no


# ─────────────────────────────── Extractors ───────────────────────────────────

def _extract_pdf(path: Path) -> list:
    import fitz  # PyMuPDF

    MIN_FONT_SIZE = 2
    blocks = []
    doc = fitz.open(str(path))

    for page_num, page in enumerate(doc, start=1):
        for tab in page.find_tables():
            page.add_redact_annot(tab.bbox)
        page.apply_redactions()

        for b in page.get_text("dict")["blocks"]:
            if "lines" not in b:
                continue
            font_sizes, texts = [], []
            for line in b["lines"]:
                for span in line["spans"]:
                    s = span["text"].strip()
                    if not s or span["size"] < MIN_FONT_SIZE:
                        continue
                    try:
                        int(s)   # skip lone page numbers
                        continue
                    except ValueError:
                        pass
                    font_sizes.append(span["size"])
                    texts.append(s)
            if not texts:
                continue
            avg = sum(font_sizes) / len(font_sizes)
            blocks.append({
                "page": page_num,
                "text": " ".join(texts),
                "avg_font_size": round(avg, 2),
            })

    doc.close()
    return blocks


def _classify_pdf(blocks: list) -> list:
    """
    Classify PDF text blocks using Jenks natural breaks on font size.
    Falls back to labelling everything "body" if there isn't enough
    font-size variance to form 4 distinct clusters.
    """
    import jenkspy

    sizes = [b["avg_font_size"] for b in blocks if b["avg_font_size"] > 0]
    unique = sorted(set(sizes))

    if len(unique) < 4:
        # Not enough variation — don't risk misclassifying real content
        console.print(
            "  [yellow]Font-size variance too low for auto-classification; "
            "treating all blocks as body text.[/yellow]"
        )
        for b in blocks:
            b["label"] = "body"
        return blocks

    breaks = jenkspy.jenks_breaks(sizes, n_classes=4)

    def label(s):
        if s < breaks[1]: return "other"
        if s < breaks[2]: return "caption"
        if s < breaks[3]: return "body"
        return "header"

    for b in blocks:
        b["label"] = label(b["avg_font_size"])

    # Safety check: if Jenks put the bulk of the text into "other", the
    # classification is unhelpful.  Warn and offer an override.
    narrated = sum(1 for b in blocks if b["label"] != "other")
    skipped  = len(blocks) - narrated
    if skipped > narrated:
        console.print(
            f"\n  [yellow]Warning:[/yellow] Jenks classified "
            f"[bold]{skipped}[/bold] / {len(blocks)} blocks as footnote-level "
            f"text ('other') — most of the book would be skipped."
        )
        if Confirm.ask(
            "  [yellow]Override: treat all blocks as body text?[/yellow]",
            default=True,
        ):
            for b in blocks:
                b["label"] = "body"

    return blocks


def _extract_epub(path: Path) -> list:
    import ebooklib
    from ebooklib import epub
    from bs4 import BeautifulSoup

    book   = epub.read_epub(str(path))
    blocks = []
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        soup = BeautifulSoup(item.get_content(), "lxml")
        for el in soup.find_all(["h1", "h2", "h3", "h4", "p"]):
            text = el.get_text(" ", strip=True)
            if not text:
                continue
            lbl = (
                "header"  if el.name == "h1"              else
                "caption" if el.name in ("h2", "h3", "h4") else
                "body"
            )
            blocks.append({"text": text, "label": lbl})
    return blocks


def _extract_txt(path: Path) -> list:
    text = path.read_text(encoding="utf-8", errors="replace")
    return [
        {"text": p.strip(), "label": "body"}
        for p in re.split(r"\n{2,}", text)
        if p.strip()
    ]


def _extract_md(path: Path) -> list:
    blocks, para = [], []

    def flush():
        if para:
            t = " ".join(para)
            t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)
            t = re.sub(r"\*(.+?)\*",     r"\1", t)
            t = re.sub(r"`(.+?)`",        r"\1", t)
            t = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", t)
            blocks.append({"text": t, "label": "body"})
            para.clear()

    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            flush()
            lbl = "header" if len(m.group(1)) == 1 else "caption"
            blocks.append({"text": m.group(2).strip(), "label": lbl})
        elif line == "":
            flush()
        else:
            para.append(line)

    flush()
    return blocks


def extract_text(path: Path) -> list:
    ext = path.suffix.lower()
    return {
        ".pdf":  _extract_pdf,
        ".epub": _extract_epub,
        ".txt":  _extract_txt,
        ".md":   _extract_md,
    }[ext](path)


# ─────────────────────────────── Part grouping ────────────────────────────────

def _group_into_parts(blocks: list, break_points: list, source_fmt: str) -> list:
    """
    Returns [(part_name, [blocks]), ...].

    PDF  — break_points are page numbers (inclusive end of each part).
    TXT  — break_points are paragraph numbers (1-based, inclusive end of each part).
    Other — single part, break_points ignored.
    """
    narrated = [b for b in blocks if b.get("label") != "other"]

    if not break_points or source_fmt not in ("pdf", "txt"):
        return [("part_01", narrated)]

    parts         = []
    sorted_breaks = sorted(set(break_points))

    if source_fmt == "pdf":
        prev = 0
        for i, bp in enumerate(sorted_breaks + [float("inf")]):
            group = [b for b in narrated if prev < b.get("page", 0) <= bp]
            if group:
                parts.append((f"part_{len(parts) + 1:02d}", group))
            prev = bp

    elif source_fmt == "txt":
        # break_points are 1-based paragraph numbers
        prev = 0
        for bp in sorted_breaks + [len(narrated)]:
            group = narrated[prev:bp]
            if group:
                parts.append((f"part_{len(parts) + 1:02d}", group))
            prev = bp

    return parts or [("part_01", narrated)]


# ─────────────────────────────── TTS helpers ──────────────────────────────────

def _split_text(text: str, max_len: int = 250) -> list:
    """Split text into TTS-friendly chunks of at most max_len characters."""
    text = re.sub(r"\s+", " ", text.strip())
    chunks = []

    for sent in re.split(r"(?<=[\.\?\!;])\s+", text):
        sent = sent.strip()
        if not sent:
            continue
        if len(sent) <= max_len:
            chunks.append(sent)
            continue
        temp = ""
        for part in re.split(r"(?<=,)\s+", sent):
            if len(temp) + len(part) + 1 <= max_len:
                temp = (temp + " " + part).strip() if temp else part
            else:
                if temp:
                    chunks.append(temp)
                temp = part
        if temp:
            chunks.append(temp)

    final = []
    for chunk in chunks:
        if len(chunk) <= max_len:
            final.append(chunk)
        else:
            temp = ""
            for word in chunk.split():
                if len(temp) + len(word) + 1 <= max_len:
                    temp = (temp + " " + word).strip() if temp else word
                else:
                    if temp:
                        final.append(temp)
                    temp = word
            if temp:
                final.append(temp)

    return final


def _concat_wavs(wav_files: list, output_path: Path) -> bool:
    """
    Concatenate WAV files on disk via FFmpeg (no AudioSegment memory limit).
    Returns True on success.
    """
    list_file = output_path.parent / f"_list_{output_path.stem}.txt"
    list_file.write_text(
        "\n".join(f"file '{f.name}'" for f in wav_files),
        encoding="utf-8",
    )
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    list_file.unlink(missing_ok=True)
    if result.returncode != 0:
        console.print(f"  [red]FFmpeg error:[/red]\n{result.stderr[-600:]}")
        return False
    return True


# ─────────────────────────────── Audio generation ─────────────────────────────

def generate_audio(
    blocks: list,
    output_dir: Path,
    break_points: list | None = None,
    source_fmt: str = "",
):
    """
    Generate audio for all parts.

    Each part becomes one WAV file (part_01.wav, part_02.wav, …).
    Within a part, every text block is synthesised into a private temp WAV
    (_p01b0000.wav …) so progress can be resumed block-by-block.
    After all blocks in a part are done, FFmpeg assembles them into the
    final part WAV and the temp files are deleted.
    """
    from TTS.api import TTS as CoquiTTS
    import torch
    from pydub import AudioSegment

    if break_points is None:
        break_points = []

    parts = _group_into_parts(blocks, break_points, source_fmt)

    narrated_total = sum(len(p[1]) for p in parts)
    skipped_total  = len(blocks) - narrated_total
    console.print(
        f"  [green]Blocks:[/green] {narrated_total} narrated"
        + (f", {skipped_total} skipped (footnotes / small text)" if skipped_total else "")
    )
    console.print(f"  [green]Parts:[/green]  {len(parts)} audio file(s)\n")

    # ── Load TTS model ────────────────────────────────────────────────────────
    device = "cuda" if torch.cuda.is_available() else "cpu"
    console.print(f"  [dim]Device: {device.upper()}[/dim]\n")

    with console.status("[cyan]Loading TTS model (first run downloads ~1.8 GB)…[/cyan]"):
        tts = CoquiTTS(
            "tts_models/multilingual/multi-dataset/xtts_v2",
            progress_bar=False,
        ).to(device)
    console.print("  [green]✓ TTS model ready[/green]\n")

    # ── Generate each part ────────────────────────────────────────────────────
    for part_name, part_blocks in parts:
        part_wav = output_dir / f"{part_name}.wav"

        if part_wav.exists():
            console.print(f"  [dim]✓ {part_name}.wav already complete — skipping[/dim]")
            continue

        console.print(
            Panel(
                f"[bold]{part_name}[/bold]  ({len(part_blocks)} blocks)",
                border_style="blue",
                padding=(0, 2),
            )
        )

        # Names for per-block temp WAVs: _p01b0000.wav …
        tag = part_name.replace("part_", "p")
        block_wavs = [
            output_dir / f"_{tag}b{i:04d}.wav"
            for i in range(len(part_blocks))
        ]
        done_set   = {p for p in block_wavs if p.exists()}
        to_do      = [(i, b) for i, b in enumerate(part_blocks) if block_wavs[i] not in done_set]

        if done_set:
            console.print(
                f"  [cyan]Resuming: {len(done_set)}/{len(part_blocks)} "
                "blocks already synthesised.[/cyan]\n"
            )

        if to_do:
            with Progress(
                SpinnerColumn(),
                TextColumn("[cyan]{task.description}[/cyan]"),
                BarColumn(),
                TaskProgressColumn(),
                TimeRemainingColumn(),
                console=console,
            ) as progress:
                task = progress.add_task("Synthesising…", total=len(to_do))

                for seq, (i, block) in enumerate(to_do, start=1):
                    label  = block.get("label", "body")
                    chunks = _split_text(block["text"])
                    tmp_files = []

                    for j, chunk in enumerate(chunks):
                        tmp = output_dir / f"_{tag}b{i:04d}_c{j:03d}.wav"
                        tts.tts_to_file(
                            text=chunk,
                            file_path=str(tmp),
                            speaker="Adde Michal",
                            language="en",
                        )
                        tmp_files.append(tmp)

                    # Combine TTS chunks for this block with pydub
                    # (we need it for the inter-chunk micro-pauses)
                    block_audio = AudioSegment.silent(0)
                    for ci, cf in enumerate(tmp_files):
                        seg   = AudioSegment.from_wav(str(cf))
                        pause = PAUSES_MS.get(label, 200) if ci == len(tmp_files) - 1 else 50
                        block_audio = block_audio + seg + AudioSegment.silent(pause)
                        cf.unlink()

                    block_audio.export(str(block_wavs[i]), format="wav")

                    progress.update(
                        task,
                        advance=1,
                        description=(
                            f"[{part_name}] block {seq}/{len(to_do)}  [{label}]"
                        ),
                    )

        # ── Assemble part WAV from block WAVs via FFmpeg ──────────────────────
        with console.status(f"[cyan]Assembling {part_name}.wav…[/cyan]"):
            ok = _concat_wavs(block_wavs, part_wav)

        if ok:
            # Clean up per-block temps
            for bw in block_wavs:
                bw.unlink(missing_ok=True)
            mb = part_wav.stat().st_size / (1024 ** 2)
            console.print(f"  [green]✓ {part_name}.wav[/green]  ({mb:.1f} MB)\n")
        else:
            console.print(f"  [red]✗ Failed to assemble {part_name}.wav[/red]\n")


# ─────────────────────────────── Join ─────────────────────────────────────────

def join_audio(output_dir: Path):
    """Concatenate all part WAVs → audiobook.mp3 via FFmpeg."""
    # Support both new (part_*.wav) and old (block_*.wav) naming
    wav_files = sorted(output_dir.glob("part_*.wav"), key=lambda f: f.name)
    if not wav_files:
        wav_files = sorted(
            output_dir.glob("block_*.wav"),
            key=lambda f: int(re.search(r"block_(\d+)", f.stem).group(1)),
        )

    if not wav_files:
        console.print("  [red]No audio files found in that folder.[/red]")
        return

    file_list = output_dir / "_join_list.txt"
    file_list.write_text(
        "\n".join(f"file '{f.name}'" for f in wav_files),
        encoding="utf-8",
    )

    out_mp3 = output_dir / "audiobook.mp3"
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(file_list),
        "-c:a", "libmp3lame", "-b:a", "192k",
        str(out_mp3),
    ]

    with console.status(f"[cyan]Joining {len(wav_files)} file(s) → MP3…[/cyan]"):
        result = subprocess.run(cmd, capture_output=True, text=True)

    file_list.unlink(missing_ok=True)

    if result.returncode == 0:
        mb = out_mp3.stat().st_size / (1024 ** 2)
        console.print(
            f"\n  [green]✓ Saved:[/green] [bold]{out_mp3}[/bold]  ({mb:.1f} MB)"
        )
    else:
        console.print(f"  [red]FFmpeg error:[/red]\n{result.stderr[-800:]}")


# ─────────────────────────────── Main flows ───────────────────────────────────

def _flow_convert():
    _banner()
    console.print(Panel("[bold]Convert Book to Audiobook[/bold]", border_style="blue", padding=(0, 2)))
    console.print()

    path       = _pick_file()
    book_name  = path.stem
    source_fmt = path.suffix.lower().lstrip(".")
    output_dir = Path.cwd() / book_name
    output_dir.mkdir(exist_ok=True)

    console.print(f"\n  [bold]Book:[/bold]   {path.name}")
    console.print(f"  [bold]Format:[/bold] {path.suffix.upper().lstrip('.')}")
    console.print(f"  [bold]Output:[/bold] {output_dir}\n")

    classified_json = output_dir / "classified_text.json"
    config_json     = output_dir / "generation_config.json"
    blocks          = None
    break_points    = []

    # ── Resume? ───────────────────────────────────────────────────────────────
    if classified_json.exists():
        done_parts = len(list(output_dir.glob("part_*.wav")))
        if Confirm.ask(
            f"  [yellow]Found existing session ({done_parts} part(s) complete). Resume?[/yellow]",
            default=True,
        ):
            blocks = json.loads(classified_json.read_text(encoding="utf-8"))
            if config_json.exists():
                cfg          = json.loads(config_json.read_text(encoding="utf-8"))
                break_points = cfg.get("break_points", [])
                bp_label     = ", ".join(str(b) for b in break_points) if break_points else "none"
                console.print(
                    f"  [cyan]Loaded {len(blocks)} blocks "
                    f"with break points: [{bp_label}][/cyan]\n"
                )
            else:
                console.print(f"  [cyan]Loaded {len(blocks)} blocks.[/cyan]\n")
        else:
            # Clear audio files and start fresh
            for f in output_dir.glob("part_*.wav"):
                f.unlink()
            for f in output_dir.glob("_p*.wav"):
                f.unlink()
            for f in output_dir.glob("block_*.wav"):   # old naming
                f.unlink()
            classified_json.unlink(missing_ok=True)
            config_json.unlink(missing_ok=True)

    # ── Extract + classify ────────────────────────────────────────────────────
    if blocks is None:
        with console.status(f"[cyan]Extracting text from [bold]{path.name}[/bold]…[/cyan]"):
            blocks = extract_text(path)

        if not blocks:
            console.print("  [red]No text could be extracted from this file.[/red]")
            Prompt.ask("\n  Press Enter to return to menu")
            return

        if source_fmt == "pdf":
            with console.status("[cyan]Classifying text blocks by font size…[/cyan]"):
                blocks = _classify_pdf(blocks)

        classified_json.write_text(
            json.dumps(blocks, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        narrated = sum(1 for b in blocks if b.get("label") != "other")
        console.print(
            f"\n  [green]✓ Extracted {len(blocks)} text blocks "
            f"({narrated} will be narrated)[/green]\n"
        )

        # ── Ask for break points (PDF and TXT only) ───────────────────────────
        if source_fmt == "pdf":
            total_pages  = max((b.get("page", 0) for b in blocks), default=1)
            break_points = ask_break_points(total_pages, "page")

        elif source_fmt == "txt":
            para_count = sum(1 for b in blocks if b.get("label") != "other")
            if para_count > 1:
                break_points = ask_break_points(para_count, "paragraph")

        # Save config so resume knows the break points
        config_json.write_text(
            json.dumps({"break_points": break_points, "source_fmt": source_fmt}),
            encoding="utf-8",
        )

    # ── Generate audio ────────────────────────────────────────────────────────
    generate_audio(blocks, output_dir, break_points=break_points, source_fmt=source_fmt)

    # ── Offer to join ─────────────────────────────────────────────────────────
    part_wavs = list(output_dir.glob("part_*.wav"))
    if part_wavs:
        if len(part_wavs) > 1 or Confirm.ask(
            "\n  [cyan]Join all parts into one MP3 now?[/cyan]", default=True
        ):
            join_audio(output_dir)

    Prompt.ask("\n  Press Enter to return to menu")


def _flow_join():
    _banner()
    console.print(Panel("[bold]Join Audio Parts  →  MP3[/bold]", border_style="blue", padding=(0, 2)))
    console.print()

    candidates = [
        (d, len(list(d.glob("part_*.wav"))) or len(list(d.glob("block_*.wav"))))
        for d in sorted(Path.cwd().iterdir())
        if d.is_dir() and (any(d.glob("part_*.wav")) or any(d.glob("block_*.wav")))
    ]

    if not candidates:
        console.print("  [yellow]No audiobook folders found in current directory.[/yellow]")
        Prompt.ask("\n  Press Enter to return to menu")
        return

    t = Table(box=box.SIMPLE_HEAD, border_style="dim", show_header=True, padding=(0, 2))
    t.add_column("#",       style="bold cyan", justify="right", width=4)
    t.add_column("Folder",  style="white")
    t.add_column("Parts",   style="green", justify="center", width=7)
    t.add_column("Status",  style="dim",   width=14)
    for i, (d, n) in enumerate(candidates, 1):
        status = "MP3 exists" if (d / "audiobook.mp3").exists() else ""
        t.add_row(str(i), d.name, str(n), status)

    console.print(Panel(t, title="[bold]Audiobook Folders[/bold]", border_style="blue"))

    while True:
        raw = Prompt.ask("  [cyan]Select folder[/cyan]").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(candidates):
            output_dir = candidates[int(raw) - 1][0]
            break
        console.print(f"  [red]Enter a number between 1 and {len(candidates)}.[/red]")

    join_audio(output_dir)
    Prompt.ask("\n  Press Enter to return to menu")


# ─────────────────────────────── Entry point ──────────────────────────────────

def main():
    while True:
        _banner()
        choice = _menu(
            "MAIN MENU",
            [
                "Convert Book to Audiobook",
                "Join Audio Parts  →  MP3",
                "Exit",
            ],
        )
        if choice == 1:
            _flow_convert()
        elif choice == 2:
            _flow_join()
        else:
            console.print("\n  [dim]Goodbye![/dim]\n")
            break


if __name__ == "__main__":
    main()
