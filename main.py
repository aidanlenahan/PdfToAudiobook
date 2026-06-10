#!/usr/bin/env python3
"""PdfToAudiobook — Unified local audiobook generator.

Supports: PDF, EPUB, TXT, Markdown
Requires: ffmpeg in PATH  |  see requirements.txt / install.py
"""

import json
import re
import subprocess
import sys
import threading
import time
import wave as _wave
from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

console = Console()
VERSION = "2.2.0"
SUPPORTED_FORMATS = {".pdf", ".epub", ".txt", ".md"}

# Silence (ms) appended after the last TTS chunk of each block
PAUSES_MS = {"header": 1000, "caption": 500, "body": 200}


# ─────────────────────────────── Progress factories ───────────────────────────

def _make_progress(**kwargs) -> Progress:
    """Standard progress bar used everywhere in the app."""
    return Progress(
        SpinnerColumn(),
        TextColumn("[cyan]{task.description}[/cyan]"),
        BarColumn(bar_width=36),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
        **kwargs,
    )


# ─────────────────────────────── UI helpers ───────────────────────────────────

def _banner():
    console.clear()
    title = Text(justify="center")
    title.append("PdfToAudiobook", style="bold bright_white")
    title.append(f"  v{VERSION}", style="dim white")
    sub = Text("Local AI Audiobook Generator", style="cyan", justify="center")
    fmt = Text("PDF  ·  EPUB  ·  TXT  ·  Markdown", style="dim cyan", justify="center")
    console.print(
        Panel(
            Text.assemble("\n", title, "\n", sub, "\n", fmt, "\n"),
            box=box.DOUBLE_EDGE,
            border_style="bright_blue",
            padding=(0, 6),
        )
    )
    console.print()


def _confirm_quit() -> None:
    """Ask for confirmation, then exit.  A second Ctrl-C also confirms."""
    console.print()
    try:
        ans = Prompt.ask("  [yellow]Quit?[/yellow]", choices=["y", "n"], default="n")
    except (KeyboardInterrupt, EOFError):
        ans = "y"
    if ans.lower() == "y":
        console.print("\n  [dim]Goodbye![/dim]\n")
        sys.exit(0)


def _menu(title: str, options: list) -> int:
    t = Table(
        box=box.ROUNDED, border_style="blue",
        show_header=False, padding=(0, 3), min_width=52,
    )
    t.add_column("num", style="bold cyan", justify="right", width=5)
    t.add_column("label", style="white")
    for i, opt in enumerate(options, 1):
        t.add_row(f"[{i}]", opt)
    t.add_row("", "")
    t.add_row("[q]", "[dim]Quit[/dim]")
    console.print(Panel(t, title=f"[bold]{title}[/bold]", border_style="blue"))
    while True:
        try:
            raw = Prompt.ask("\n  [cyan]Enter choice[/cyan]").strip()
        except (KeyboardInterrupt, EOFError):
            _confirm_quit()
            continue
        if raw.lower() == "q":
            _confirm_quit()
            continue
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw)
        console.print(f"  [red]Please enter a number between 1 and {len(options)}, or q to quit.[/red]")


def _pick_file() -> Path:
    cwd   = Path.cwd()
    files = sorted(
        f for f in cwd.iterdir()
        if f.is_file() and f.suffix.lower() in SUPPORTED_FORMATS
    )

    if files:
        t = Table(box=box.SIMPLE_HEAD, border_style="dim", show_header=True, padding=(0, 2))
        t.add_column("#",        style="bold cyan", justify="right", width=4)
        t.add_column("Filename", style="white")
        t.add_column("Type",     style="green", justify="center", width=7)
        for i, f in enumerate(files, 1):
            t.add_row(str(i), f.name, f.suffix.upper().lstrip("."))
        console.print(Panel(t, title="[bold]Books in Current Directory[/bold]", border_style="blue"))
        console.print("  [dim]Enter a number, type a full path, or [bold]q[/bold] to quit.[/dim]\n")
    else:
        console.print("  [yellow]No supported books found in current directory.[/yellow]")
        console.print("  [dim]Type the full path to your book file, or [bold]q[/bold] to quit.[/dim]\n")

    while True:
        try:
            raw = Prompt.ask("  [cyan]Select file[/cyan]").strip().strip('"').strip("'")
        except (KeyboardInterrupt, EOFError):
            _confirm_quit()
            continue
        if raw.lower() == "q":
            _confirm_quit()
            continue
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
    Ask the user where to split the audio output.
    Returns a sorted list of integers (each is the *last* unit in that part),
    or [] to produce a single audio file.

    Example  total_units=300, unit_name="page":
        Input : 100, 200
        Effect: part_01 → pages 1-100  |  part_02 → 101-200  |  part_03 → 201-300
    """
    fmt_word = "PDF" if unit_name == "page" else "file"
    console.print(f"\n  [dim]This {fmt_word} has [bold]{total_units}[/bold] {unit_name}s.[/dim]")
    console.print(f"  [dim]Enter {unit_name} numbers where the audio should split into separate files.[/dim]")
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

        bad = [p for p in points if not (1 <= p < total_units)]
        if bad:
            console.print(
                f"  [red]Out of range: {bad}. Values must be between 1 and {total_units - 1}.[/red]"
            )
            continue

        if len(points) != len(set(points)):
            console.print("  [red]Duplicate values are not allowed.[/red]")
            continue

        sp = sorted(points)
        console.print(f"\n  [green]Will create {len(sp) + 1} audio file(s):[/green]")
        prev = 0
        for i, bp in enumerate(sp + [total_units]):
            console.print(f"  [dim]  Part {i + 1:>2}: {unit_name}s {prev + 1} – {bp}[/dim]")
            prev = bp
        console.print()

        if Confirm.ask("  [cyan]Use these split points?[/cyan]", default=True):
            return sp

        console.print()  # loop again if user said no


# ─────────────────────────────── Extractors ───────────────────────────────────

def _extract_pdf(path: Path) -> list:
    """Extract text blocks from a PDF with a per-page progress bar."""
    import fitz  # PyMuPDF

    MIN_FONT_SIZE = 2
    blocks = []
    doc    = fitz.open(str(path))
    total  = len(doc)

    with _make_progress() as bar:
        task = bar.add_task(f"Scanning PDF  ({total} pages)", total=total)

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

            bar.advance(task)

    doc.close()
    return blocks


def _classify_pdf(blocks: list) -> list:
    """
    Classify blocks using Jenks natural breaks on font size.
    Falls back to all-body when variance is too low to form 4 clusters.
    """
    import jenkspy

    sizes  = [b["avg_font_size"] for b in blocks if b["avg_font_size"] > 0]
    unique = sorted(set(sizes))

    if len(unique) < 4:
        console.print(
            "  [yellow]Font-size variance too low for auto-classification; "
            "treating all blocks as body text.[/yellow]"
        )
        with _make_progress() as bar:
            task = bar.add_task(f"Labelling {len(blocks)} blocks", total=len(blocks))
            for b in blocks:
                b["label"] = "body"
                bar.advance(task)
        return blocks

    # Jenks break computation is instant even for large block sets
    breaks = jenkspy.jenks_breaks(sizes, n_classes=4)

    def label(s):
        if s < breaks[1]: return "other"
        if s < breaks[2]: return "caption"
        if s < breaks[3]: return "body"
        return "header"

    with _make_progress() as bar:
        task = bar.add_task(f"Classifying {len(blocks)} blocks by font size", total=len(blocks))
        for b in blocks:
            b["label"] = label(b["avg_font_size"])
            bar.advance(task)

    # Warn if Jenks accidentally silences most of the book
    narrated = sum(1 for b in blocks if b["label"] != "other")
    skipped  = len(blocks) - narrated
    if skipped > narrated:
        console.print(
            f"\n  [yellow]Warning:[/yellow] Jenks classified [bold]{skipped}[/bold] / "
            f"{len(blocks)} blocks as footnote-level text — most of the book would be skipped."
        )
        if Confirm.ask(
            "  [yellow]Override: treat all blocks as body text?[/yellow]",
            default=True,
        ):
            with _make_progress() as bar:
                task = bar.add_task(f"Overriding {len(blocks)} blocks", total=len(blocks))
                for b in blocks:
                    b["label"] = "body"
                    bar.advance(task)

    return blocks


def _extract_epub(path: Path) -> list:
    """Extract text from an EPUB with a per-chapter progress bar."""
    import ebooklib
    from ebooklib import epub
    from bs4 import BeautifulSoup

    book  = epub.read_epub(str(path))
    items = list(book.get_items_of_type(ebooklib.ITEM_DOCUMENT))
    blocks: list = []

    with _make_progress() as bar:
        task = bar.add_task(f"Scanning EPUB  ({len(items)} chapters)", total=len(items))

        for item in items:
            soup = BeautifulSoup(item.get_content(), "lxml")
            for el in soup.find_all(["h1", "h2", "h3", "h4", "p"]):
                text = el.get_text(" ", strip=True)
                if not text:
                    continue
                lbl = (
                    "header"  if el.name == "h1"               else
                    "caption" if el.name in ("h2", "h3", "h4") else
                    "body"
                )
                blocks.append({"text": text, "label": lbl})
            bar.advance(task)

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
            t = re.sub(r"\*(.+?)\*",       r"\1", t)
            t = re.sub(r"`(.+?)`",          r"\1", t)
            t = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", t)
            blocks.append({"text": t, "label": "body"})
            para.clear()

    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        m    = re.match(r"^(#{1,6})\s+(.*)", line)
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
    return {
        ".pdf":  _extract_pdf,
        ".epub": _extract_epub,
        ".txt":  _extract_txt,
        ".md":   _extract_md,
    }[path.suffix.lower()](path)


# ─────────────────────────────── Part grouping ────────────────────────────────

def _group_into_parts(blocks: list, break_points: list, source_fmt: str) -> list:
    """
    Returns [(part_name, [blocks]), ...].

    PDF  — break_points are page numbers (inclusive end of each part).
    TXT  — break_points are paragraph numbers (1-based, inclusive end).
    Other — single part, break_points ignored.
    """
    narrated = [b for b in blocks if b.get("label") != "other"]

    if not break_points or source_fmt not in ("pdf", "txt"):
        return [("part_01", narrated)]

    parts         = []
    sorted_breaks = sorted(set(break_points))

    if source_fmt == "pdf":
        prev = 0
        for bp in sorted_breaks + [float("inf")]:
            group = [b for b in narrated if prev < b.get("page", 0) <= bp]
            if group:
                parts.append((f"part_{len(parts) + 1:02d}", group))
            prev = bp

    elif source_fmt == "txt":
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
    text   = re.sub(r"\s+", " ", text.strip())
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


# ─────────────────────────────── FFmpeg helpers ───────────────────────────────

def _wav_duration_s(path: Path) -> float:
    """Duration of a WAV file in seconds (0.0 on any error)."""
    try:
        with _wave.open(str(path), "rb") as wf:
            return wf.getnframes() / max(wf.getframerate(), 1)
    except Exception:
        return 0.0


def _ffmpeg_progress(cmd: list, total_s: float, label: str) -> tuple:
    """
    Run an FFmpeg command with a real-time rich progress bar.

    Injects  -progress pipe:1 -nostats  so FFmpeg writes machine-readable
    progress to stdout while we display it.  stderr is captured and returned
    for error-reporting.

    Returns (exit_code: int, stderr: str).
    """
    full_cmd = [cmd[0], "-progress", "pipe:1", "-nostats"] + cmd[1:]

    completed = [0.0]   # shared cell updated by the reader thread

    proc = subprocess.Popen(
        full_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    def _reader():
        for line in proc.stdout:
            if line.startswith("out_time_us="):
                try:
                    completed[0] = max(0, int(line.split("=")[1])) / 1_000_000
                except ValueError:
                    pass

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    safe_total = max(total_s, 1.0)

    try:
        with _make_progress() as bar:
            task = bar.add_task(label, total=safe_total)
            while proc.poll() is None:
                bar.update(task, completed=min(completed[0], safe_total))
                time.sleep(0.15)
            bar.update(task, completed=safe_total)
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait()
        raise

    reader.join(timeout=2)
    stderr = proc.stderr.read()
    return proc.returncode, stderr


def _concat_wavs(wav_files: list, output_path: Path) -> bool:
    """
    Concatenate WAV files via FFmpeg -c copy (no re-encode, near-instant).
    Shows a simple spinner since duration is typically under 1 second.
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
    with console.status(f"[cyan]Assembling {output_path.name}…[/cyan]"):
        result = subprocess.run(cmd, capture_output=True, text=True)
    list_file.unlink(missing_ok=True)
    if result.returncode != 0:
        console.print(f"  [red]FFmpeg error:[/red]\n{result.stderr[-600:]}")
        return False
    return True


# ─────────────────────────────── Audio generation ─────────────────────────────

def generate_audio(
    blocks:       list,
    output_dir:   Path,
    break_points: list | None = None,
    source_fmt:   str  = "",
):
    """
    Synthesise all parts with a two-level progress display:

        Overall  ░░░░░░░░░░░░░░░░░░░░  45/320  14%  0:01:23  ETA 8:42
        part_01  ░░░░░░░░░░░░░░░░░░░░  45/120  37%  0:01:23  ETA 2:05

    Each part is written to part_NN.wav.  Per-block temp WAVs (_p01b0000.wav)
    allow block-level resume: interrupt at any point and restart to continue.
    """
    from TTS.api import TTS as CoquiTTS
    import torch
    from pydub import AudioSegment

    if break_points is None:
        break_points = []

    all_parts = _group_into_parts(blocks, break_points, source_fmt)

    # Split into already-complete and still-needed
    parts_done = [(n, b) for n, b in all_parts if (output_dir / f"{n}.wav").exists()]
    parts_todo = [(n, b) for n, b in all_parts if not (output_dir / f"{n}.wav").exists()]

    # Summary
    narrated_total = sum(len(p[1]) for p in all_parts)
    skipped_total  = len(blocks) - narrated_total
    console.print(
        f"  [green]Blocks:[/green] {narrated_total} narrated"
        + (f", {skipped_total} skipped (footnotes/small text)" if skipped_total else "")
    )
    console.print(f"  [green]Parts:[/green]  {len(all_parts)} total, {len(parts_done)} complete, {len(parts_todo)} remaining\n")

    if not parts_todo:
        console.print("  [yellow]All parts already generated — nothing to synthesise.[/yellow]")
        return

    # Pre-scan temp WAVs to get the true remaining block count (for accurate ETA)
    def _todo_for(part_name, part_blocks):
        tag = part_name.replace("part_", "p")
        return [
            (i, b) for i, b in enumerate(part_blocks)
            if not (output_dir / f"_{tag}b{i:04d}.wav").exists()
        ]

    total_blocks_remaining = sum(len(_todo_for(n, b)) for n, b in parts_todo)

    # ── Load TTS model ────────────────────────────────────────────────────────
    device = "cuda" if torch.cuda.is_available() else "cpu"
    console.print(f"  [dim]Device: {device.upper()}[/dim]\n")

    with console.status("[cyan]Loading TTS model (first run downloads ~1.8 GB)…[/cyan]"):
        tts = CoquiTTS(
            "tts_models/multilingual/multi-dataset/xtts_v2",
            progress_bar=False,
        ).to(device)
    console.print("  [green]✓ TTS model ready[/green]\n")

    # ── Two-level progress (one context spans ALL parts) ──────────────────────
    with _make_progress() as bar:
        overall_task = bar.add_task(
            f"[bold white]Book[/bold white]  [dim]({len(parts_todo)} part(s) left)[/dim]",
            total=max(total_blocks_remaining, 1),
        )
        part_task = bar.add_task("Initialising…", total=1)

        for part_idx, (part_name, part_blocks) in enumerate(parts_todo, start=1):
            tag        = part_name.replace("part_", "p")
            block_wavs = [output_dir / f"_{tag}b{i:04d}.wav" for i in range(len(part_blocks))]
            done_set   = {p for p in block_wavs if p.exists()}
            to_do      = [(i, b) for i, b in enumerate(part_blocks) if block_wavs[i] not in done_set]

            # Reset the per-part bar
            bar.reset(part_task, total=max(len(to_do), 1))
            bar.update(
                part_task,
                description=(
                    f"[cyan]{part_name}[/cyan]  "
                    f"[dim]{part_idx}/{len(parts_todo)} parts[/dim]"
                ),
            )

            if done_set:
                bar.print(
                    f"  [dim]Resuming {part_name}: "
                    f"{len(done_set)} blocks already done, {len(to_do)} remaining[/dim]"
                )

            # Synthesise each outstanding block
            try:
                for i, block in to_do:
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

                    # Merge TTS chunks into one block WAV (pydub handles micro-pauses)
                    block_audio = AudioSegment.silent(0)
                    for ci, cf in enumerate(tmp_files):
                        seg   = AudioSegment.from_wav(str(cf))
                        pause = PAUSES_MS.get(label, 200) if ci == len(tmp_files) - 1 else 50
                        block_audio = block_audio + seg + AudioSegment.silent(pause)
                        cf.unlink()

                    block_audio.export(str(block_wavs[i]), format="wav")

                    bar.advance(part_task)
                    bar.advance(overall_task)

            except KeyboardInterrupt:
                bar.print(
                    "\n  [yellow]Interrupted.[/yellow]  "
                    "Synthesised blocks are saved — run again to resume."
                )
                raise

            # Assemble all block WAVs → part WAV
            bar.update(overall_task, description=f"[bold white]Assembling {part_name}…[/bold white]")
            ok = _concat_wavs(block_wavs, output_dir / f"{part_name}.wav")

            if ok:
                for bw in block_wavs:
                    bw.unlink(missing_ok=True)
                mb = (output_dir / f"{part_name}.wav").stat().st_size / 1024 ** 2
                bar.print(f"  [green]✓ {part_name}.wav[/green]  ({mb:.1f} MB)")
            else:
                bar.print(f"  [red]✗ Failed to assemble {part_name}.wav[/red]")

            bar.update(
                overall_task,
                description=(
                    f"[bold white]Book[/bold white]  "
                    f"[dim]({len(parts_todo) - part_idx} part(s) left)[/dim]"
                ),
            )

    console.print()


# ─────────────────────────────── Join ─────────────────────────────────────────

def join_audio(output_dir: Path):
    """
    Concatenate all part WAVs → audiobook.mp3.

    Shows a real-time FFmpeg encoding progress bar: measures the total audio
    duration of all source WAVs, then streams FFmpeg's out_time_us output
    to display X% done + ETA.
    """
    # Prefer new part_*.wav naming; fall back to old block_*.wav
    wav_files = sorted(output_dir.glob("part_*.wav"), key=lambda f: f.name)
    if not wav_files:
        wav_files = sorted(
            output_dir.glob("block_*.wav"),
            key=lambda f: int(re.search(r"block_(\d+)", f.stem).group(1)),
        )

    if not wav_files:
        console.print("  [red]No audio files found in that folder.[/red]")
        return

    # Total audio duration for progress bar
    console.print(f"  [dim]Measuring duration of {len(wav_files)} file(s)…[/dim]")
    total_s = sum(_wav_duration_s(f) for f in wav_files)
    mins, secs = divmod(int(total_s), 60)
    hours, mins = divmod(mins, 60)
    dur_label = (
        f"{hours}h {mins:02d}m {secs:02d}s" if hours
        else f"{mins}m {secs:02d}s"
    )
    console.print(f"  [dim]Total audio: {dur_label}[/dim]\n")

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

    exit_code, stderr = _ffmpeg_progress(
        cmd,
        total_s=total_s,
        label=f"Encoding MP3  ({len(wav_files)} parts)",
    )

    file_list.unlink(missing_ok=True)

    if exit_code == 0:
        mb = out_mp3.stat().st_size / 1024 ** 2
        console.print(
            f"\n  [green]✓ Saved:[/green] [bold]{out_mp3}[/bold]  ({mb:.1f} MB)"
        )
    else:
        console.print(f"  [red]FFmpeg error:[/red]\n{stderr[-800:]}")


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
                bp_label     = ", ".join(str(b) for b in break_points) or "none"
                console.print(
                    f"  [cyan]Loaded {len(blocks)} blocks "
                    f"with break points: [{bp_label}][/cyan]\n"
                )
            else:
                console.print(f"  [cyan]Loaded {len(blocks)} blocks.[/cyan]\n")
        else:
            for pat in ("part_*.wav", "_p*.wav", "block_*.wav"):
                for f in output_dir.glob(pat):
                    f.unlink()
            classified_json.unlink(missing_ok=True)
            config_json.unlink(missing_ok=True)

    # ── Extract + classify ────────────────────────────────────────────────────
    if blocks is None:
        # PDF and EPUB show their own progress bars internally.
        # TXT and MD are instant — just print a status line.
        if source_fmt in ("txt", "md"):
            with console.status(f"[cyan]Reading {path.name}…[/cyan]"):
                blocks = extract_text(path)
        else:
            console.print(f"  [cyan]Extracting text from [bold]{path.name}[/bold]…[/cyan]")
            blocks = extract_text(path)

        if not blocks:
            console.print("  [red]No text could be extracted from this file.[/red]")
            Prompt.ask("\n  Press Enter to return to menu")
            return

        if source_fmt == "pdf":
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
    t.add_column("#",      style="bold cyan", justify="right", width=4)
    t.add_column("Folder", style="white")
    t.add_column("Parts",  style="green", justify="center", width=7)
    t.add_column("Status", style="dim",   width=14)
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
    try:
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
    except (KeyboardInterrupt, EOFError):
        console.print("\n\n  [dim]Goodbye![/dim]\n")
        sys.exit(0)


if __name__ == "__main__":
    main()
