#!/usr/bin/env python3
"""PdfToAudiobook — Unified local audiobook generator.

Supports: PDF, EPUB, TXT, Markdown
Requires: ffmpeg in PATH, see requirements.txt
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
VERSION = "2.0.0"
SUPPORTED_FORMATS = {".pdf", ".epub", ".txt", ".md"}
PAUSES_MS = {"header": 1000, "caption": 500, "body": 200}


# ─────────────────────────────── UI Helpers ───────────────────────────────────

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


def _menu(title: str, options: list) -> int:
    t = Table(
        box=box.ROUNDED,
        border_style="blue",
        show_header=False,
        padding=(0, 3),
        min_width=52,
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
    cwd = Path.cwd()
    files = sorted(
        f for f in cwd.iterdir()
        if f.is_file() and f.suffix.lower() in SUPPORTED_FORMATS
    )

    if files:
        t = Table(
            box=box.SIMPLE_HEAD,
            border_style="dim",
            show_header=True,
            padding=(0, 2),
        )
        t.add_column("#", style="bold cyan", justify="right", width=4)
        t.add_column("Filename", style="white")
        t.add_column("Type", style="green", justify="center", width=7)
        for i, f in enumerate(files, 1):
            t.add_row(str(i), f.name, f.suffix.upper().lstrip("."))
        console.print(
            Panel(t, title="[bold]Books in Current Directory[/bold]", border_style="blue")
        )
        console.print("  [dim]Enter a number, or type a full path to select a file elsewhere.[/dim]\n")
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
                        int(s)  # skip lone page numbers
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
    import jenkspy

    sizes = [b["avg_font_size"] for b in blocks if b["avg_font_size"] > 0]
    if len(set(sizes)) < 4:
        for b in blocks:
            b.setdefault("label", "body")
        return blocks

    breaks = jenkspy.jenks_breaks(sizes, n_classes=4)

    def label(s):
        if s < breaks[1]:
            return "other"
        if s < breaks[2]:
            return "caption"
        if s < breaks[3]:
            return "body"
        return "header"

    for b in blocks:
        b["label"] = label(b["avg_font_size"])
    return blocks


def _extract_epub(path: Path) -> list:
    import ebooklib
    from ebooklib import epub
    from bs4 import BeautifulSoup

    book = epub.read_epub(str(path))
    blocks = []
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        soup = BeautifulSoup(item.get_content(), "lxml")
        for el in soup.find_all(["h1", "h2", "h3", "h4", "p"]):
            text = el.get_text(" ", strip=True)
            if not text:
                continue
            if el.name == "h1":
                lbl = "header"
            elif el.name in ("h2", "h3", "h4"):
                lbl = "caption"
            else:
                lbl = "body"
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
            t = re.sub(r"\*(.+?)\*", r"\1", t)
            t = re.sub(r"`(.+?)`", r"\1", t)
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
    dispatch = {
        ".pdf": _extract_pdf,
        ".epub": _extract_epub,
        ".txt": _extract_txt,
        ".md": _extract_md,
    }
    return dispatch[ext](path)


# ────────────────────────────── TTS / Audio ───────────────────────────────────

def _split_text(text: str, max_len: int = 250) -> list:
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


def generate_audio(blocks: list, output_dir: Path):
    from TTS.api import TTS as CoquiTTS
    import torch
    from pydub import AudioSegment

    device = "cuda" if torch.cuda.is_available() else "cpu"
    console.print(f"  [dim]Device: {device.upper()}[/dim]\n")

    with console.status("[cyan]Loading TTS model (first run downloads ~1.8 GB)…[/cyan]"):
        tts = CoquiTTS(
            "tts_models/multilingual/multi-dataset/xtts_v2",
            progress_bar=False,
        ).to(device)

    console.print("  [green]✓ TTS model ready[/green]\n")

    narrated = [(i, b) for i, b in enumerate(blocks, start=1) if b.get("label") != "other"]
    done = {f.stem for f in output_dir.glob("block_*.wav")}
    remaining = [(i, b) for i, b in narrated if f"block_{i}" not in done]

    if not remaining:
        console.print("  [yellow]All blocks already generated — nothing to do.[/yellow]")
        return

    skipped = len(narrated) - len(remaining)
    if skipped:
        console.print(
            f"  [cyan]Resuming: {skipped} blocks already done, "
            f"{len(remaining)} remaining.[/cyan]\n"
        )

    with Progress(
        SpinnerColumn(),
        TextColumn("[cyan]{task.description}[/cyan]"),
        BarColumn(),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Generating audio…", total=len(remaining))

        for seq, (i, block) in enumerate(remaining, start=1):
            label = block.get("label", "body")
            chunks = _split_text(block["text"])
            tmp_files = []

            for j, chunk in enumerate(chunks):
                tmp = output_dir / f"_tmp_{i}_{j}.wav"
                tts.tts_to_file(
                    text=chunk,
                    file_path=str(tmp),
                    speaker="Adde Michal",
                    language="en",
                )
                tmp_files.append(tmp)

            audio = AudioSegment.silent(duration=0)
            for ci, cf in enumerate(tmp_files):
                seg = AudioSegment.from_wav(str(cf))
                pause = PAUSES_MS.get(label, 200) if ci == len(tmp_files) - 1 else 50
                audio = audio + seg + AudioSegment.silent(pause)
                cf.unlink()

            audio.export(str(output_dir / f"block_{i}.wav"), format="wav")
            progress.update(
                task,
                advance=1,
                description=f"Block {seq}/{len(remaining)}  [{label}]",
            )


# ───────────────────────────────── Join ───────────────────────────────────────

def join_audio(output_dir: Path):
    wav_files = sorted(
        output_dir.glob("block_*.wav"),
        key=lambda f: int(re.search(r"block_(\d+)", f.stem).group(1)),
    )
    if not wav_files:
        console.print("  [red]No block_*.wav files found in that folder.[/red]")
        return

    file_list = output_dir / "_file_list.txt"
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

    with console.status(f"[cyan]Joining {len(wav_files)} chunks with FFmpeg…[/cyan]"):
        result = subprocess.run(cmd, capture_output=True, text=True)

    file_list.unlink(missing_ok=True)

    if result.returncode == 0:
        mb = out_mp3.stat().st_size / (1024 ** 2)
        console.print(
            f"\n  [green]✓ Saved:[/green] [bold]{out_mp3}[/bold]  ({mb:.1f} MB)"
        )
    else:
        console.print(f"  [red]FFmpeg error:[/red]\n{result.stderr[-800:]}")


# ──────────────────────────────── Main Flows ──────────────────────────────────

def _flow_convert():
    _banner()
    console.print(
        Panel("[bold]Convert Book to Audiobook[/bold]", border_style="blue", padding=(0, 2))
    )
    console.print()

    path = _pick_file()
    book_name = path.stem
    output_dir = Path.cwd() / book_name
    output_dir.mkdir(exist_ok=True)

    console.print(f"\n  [bold]Book:[/bold]   {path.name}")
    console.print(f"  [bold]Format:[/bold] {path.suffix.upper().lstrip('.')}")
    console.print(f"  [bold]Output:[/bold] {output_dir}\n")

    classified_json = output_dir / "classified_text.json"
    blocks = None

    if classified_json.exists():
        done_count = len(list(output_dir.glob("block_*.wav")))
        if Confirm.ask(
            f"  [yellow]Found existing session ({done_count} audio chunks). "
            "Resume from where we left off?[/yellow]",
            default=True,
        ):
            blocks = json.loads(classified_json.read_text(encoding="utf-8"))
            console.print(f"  [cyan]Loaded {len(blocks)} blocks — skipping already-done audio.[/cyan]\n")
        else:
            for f in output_dir.glob("block_*.wav"):
                f.unlink()
            classified_json.unlink()

    if blocks is None:
        with console.status(f"[cyan]Extracting text from [bold]{path.name}[/bold]…[/cyan]"):
            blocks = extract_text(path)

        if not blocks:
            console.print("  [red]No text could be extracted from this file.[/red]")
            Prompt.ask("\n  Press Enter to return to menu")
            return

        if path.suffix.lower() == ".pdf":
            with console.status("[cyan]Classifying text blocks by font size…[/cyan]"):
                blocks = _classify_pdf(blocks)

        classified_json.write_text(
            json.dumps(blocks, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        body = sum(1 for b in blocks if b.get("label") != "other")
        console.print(
            f"  [green]✓ Extracted {len(blocks)} text blocks "
            f"({body} will be narrated)[/green]\n"
        )

    generate_audio(blocks, output_dir)

    if Confirm.ask("\n  [cyan]Join all audio chunks into one MP3 now?[/cyan]", default=True):
        join_audio(output_dir)

    Prompt.ask("\n  Press Enter to return to menu")


def _flow_join():
    _banner()
    console.print(
        Panel("[bold]Join Audio Chunks  →  MP3[/bold]", border_style="blue", padding=(0, 2))
    )
    console.print()

    candidates = [
        (d, len(list(d.glob("block_*.wav"))))
        for d in sorted(Path.cwd().iterdir())
        if d.is_dir() and any(d.glob("block_*.wav"))
    ]

    if not candidates:
        console.print(
            "  [yellow]No audiobook folders with audio chunks found in current directory.[/yellow]"
        )
        Prompt.ask("\n  Press Enter to return to menu")
        return

    t = Table(
        box=box.SIMPLE_HEAD,
        border_style="dim",
        show_header=True,
        padding=(0, 2),
    )
    t.add_column("#", style="bold cyan", justify="right", width=4)
    t.add_column("Folder", style="white")
    t.add_column("Chunks", style="green", justify="center", width=8)
    t.add_column("Status", style="dim", width=14)
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


# ─────────────────────────────── Entry Point ──────────────────────────────────

def main():
    while True:
        _banner()
        choice = _menu(
            "MAIN MENU",
            [
                "Convert Book to Audiobook",
                "Join Audio Chunks  →  MP3",
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
