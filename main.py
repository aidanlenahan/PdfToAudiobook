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
from rich.console import Console, Group
from rich.live import Live
from rich.padding import Padding
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

console = Console()
VERSION = "2.3.0"
SUPPORTED_FORMATS = {".pdf", ".epub", ".txt", ".md"}
HERE = Path(__file__).resolve().parent

# Default silence (ms) after each block; overridable via prefs (pause_*_ms)
PAUSES_MS = {"header": 1000, "caption": 500, "body": 200}

# Output-size estimation (XTTS v2 renders 24 kHz / 16-bit / mono)
XTTS_SAMPLE_RATE   = 24_000
PIPER_SAMPLE_RATE  = 22_050                    # typical for *-medium Piper voices
WAV_BYTES_PER_SEC  = XTTS_SAMPLE_RATE * 2     # 16-bit mono PCM (XTTS default)
MP3_BYTES_PER_SEC  = 192_000 // 8             # libmp3lame 192 kbps CBR
CHARS_PER_SECOND   = 14.0                      # ≈ 150 wpm narration
AVG_BLOCK_PAUSE_S  = 0.4                       # trailing silence per block

# TTS engines
ENGINE_PIPER = "piper"   # fast, runs faster than real-time on CPU
ENGINE_XTTS  = "xtts"    # slow on CPU, highest quality / most natural
DEFAULT_PIPER_VOICE = "en_US-lessac-medium"
PIPER_CACHE = Path.home() / ".cache" / "pdftoaudiobook" / "piper"

# Curated Piper voices.  key -> (label, accent, gender, tier, note)
# Tier drives the speed/quality trade-off (measured on CPU, ~realtime multiple):
#   low ≈ 28×   medium ≈ 22×   high ≈ 5×   (all far faster than XTTS)
PIPER_VOICES = {
    "en_US-lessac-medium":     ("Lessac",      "US", "neutral", "medium", "balanced · recommended"),
    "en_US-amy-medium":        ("Amy",         "US", "female",  "medium", "balanced"),
    "en_US-hfc_female-medium": ("HFC Female",  "US", "female",  "medium", "balanced · very clear"),
    "en_US-joe-medium":        ("Joe",         "US", "male",    "medium", "balanced"),
    "en_GB-alan-medium":       ("Alan",        "GB", "male",    "medium", "balanced"),
    "en_GB-cori-high":         ("Cori",        "GB", "female",  "high",   "best quality · ~5× slower"),
    "en_US-ryan-high":         ("Ryan",        "US", "male",    "high",   "best quality · ~5× slower"),
    "en_US-lessac-low":        ("Lessac Lite", "US", "neutral", "low",    "fastest · lower fidelity"),
}


def _piper_voice_sr(voice: str) -> int:
    """Sample rate implied by a Piper voice's quality tier."""
    return 16_000 if voice.endswith(("-low", "-x_low")) else PIPER_SAMPLE_RATE


# ─────────────────────────────── Preferences ──────────────────────────────────

PREFS_PATH = HERE / "prefs.json"

# key -> (default, description, validator)
# validator is one of: a list of allowed values, or a string kind:
#   "voice"  "path"  "speed"  "pause"  "marker"
PREFS_SCHEMA = {
    "default_engine":   ("",        "Engine to use without asking. Blank = ask every run. "
                                    "Options: piper (fast) or xtts (slow, top quality).",
                                    ["", "piper", "xtts"]),
    "default_voice":    ("",        "Piper voice used without asking (only when engine is piper). "
                                    "Blank = ask every run. e.g. en_US-amy-medium.",
                                    "voice"),
    "output_dir":       ("output",  "Folder that holds generated audiobooks; a per-book subfolder "
                                    "is created inside it. Blank = ask every run. Relative paths "
                                    "are taken from where you run the app.",
                                    "path"),
    "audio_format":     ("mp3",     "Format of the final joined file.", ["mp3", "flac"]),
    "mp3_bitrate":      ("192k",    "MP3 quality (ignored for FLAC). Higher = larger, better.",
                                    ["128k", "192k", "320k"]),
    "narration_speed":  (1.0,       "Speech speed multiplier. 1.0 = normal, 0.9 = slower, "
                                    "1.15 = faster. Allowed range 0.5–2.0.",
                                    "speed"),
    "auto_join":        (True,      "Join all parts into one file automatically when generation "
                                    "finishes (no prompt).", [True, False]),
    "pause_header_ms":  (1000,      "Silence (ms) after a chapter/heading. Range 0–5000.", "pause"),
    "pause_caption_ms": (500,       "Silence (ms) after a sub-heading/caption. Range 0–5000.", "pause"),
    "pause_body_ms":    (200,       "Silence (ms) after a normal paragraph. Range 0–5000.", "pause"),
    "epub_split":       ("chapter", "How EPUBs are divided for split points: 'chapter' (natural) "
                                    "or 'paragraph' (by block number).", ["chapter", "paragraph"]),
    "md_split":         ("heading", "How Markdown is divided for split points: 'heading' (by "
                                    "section) or 'paragraph' (by block number).",
                                    ["heading", "paragraph"]),
    "md_heading_marker":("#",       "When md_split = heading, a new section starts at headings "
                                    "this deep or shallower. '#' = only top-level, '##' = also "
                                    "subsections, '###' = also sub-subsections.", "marker"),
}

# Keys whose blank value means "ask every run"
PREFS_BLANK_PROMPTS = {"default_engine", "default_voice", "output_dir"}


def _coerce_pref(key: str, value):
    """Validate/coerce one preference value. Returns (ok, coerced)."""
    _, _, rule = PREFS_SCHEMA[key]
    try:
        if isinstance(rule, list):
            return (value in rule, value)
        if rule == "voice":
            if value in ("", None):
                return True, ""
            return (isinstance(value, str) and "-" in value, value)
        if rule == "path":
            return (isinstance(value, str), value)
        if rule == "marker":
            return (isinstance(value, str) and 1 <= len(value) <= 6 and set(value) == {"#"}, value)
        if rule == "speed":
            f = float(value)
            return (0.5 <= f <= 2.0, f)
        if rule == "pause":
            if isinstance(value, bool):
                return False, None
            i = int(value)
            return (0 <= i <= 5000, i)
    except (ValueError, TypeError):
        return False, None
    return True, value


def _default_prefs() -> dict:
    return {k: spec[0] for k, spec in PREFS_SCHEMA.items()}


def save_prefs(prefs: dict) -> None:
    """Write prefs.json with an inline _help block documenting each setting."""
    data = {"_help": {k: spec[1] for k, spec in PREFS_SCHEMA.items()}}
    for k in PREFS_SCHEMA:
        data[k] = prefs.get(k, PREFS_SCHEMA[k][0])
    try:
        PREFS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as e:
        console.print(f"  [yellow]Could not save preferences to {PREFS_PATH}: {e}[/yellow]")


def load_prefs() -> dict:
    """Load prefs.json, validating every value and falling back to defaults.

    Invalid file → backed up and recreated.  Invalid individual values →
    reset to default with a warning.  Never raises on bad config.
    """
    prefs = _default_prefs()
    if not PREFS_PATH.exists():
        save_prefs(prefs)
        return prefs
    try:
        raw = json.loads(PREFS_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("top-level value is not an object")
    except (json.JSONDecodeError, ValueError, OSError) as e:
        console.print(f"  [yellow]prefs.json is invalid ({e}); using defaults and backing it up.[/yellow]")
        try:
            PREFS_PATH.replace(PREFS_PATH.with_suffix(".json.bak"))
        except OSError:
            pass
        save_prefs(prefs)
        return prefs

    reset = []
    for k in PREFS_SCHEMA:
        if k in raw:
            ok, val = _coerce_pref(k, raw[k])
            if ok:
                prefs[k] = val
            else:
                reset.append(k)
    if reset:
        console.print(
            "  [yellow]Some settings in prefs.json were invalid and reset to defaults: "
            f"{', '.join(reset)}.[/yellow]"
        )
    return prefs


def prefs_pauses(prefs: dict) -> dict:
    """Per-label trailing pause (ms) from prefs."""
    return {
        "header":  prefs.get("pause_header_ms", 1000),
        "caption": prefs.get("pause_caption_ms", 500),
        "body":    prefs.get("pause_body_ms", 200),
    }


# ─────────────────────────────── Formatting helpers ───────────────────────────

def _fmt_hms(seconds: float) -> str:
    """Format a duration as H:MM:SS (or M:SS under an hour)."""
    if seconds is None or seconds < 0:
        seconds = 0
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s   = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _human_size(num_bytes: float) -> str:
    """Format a byte count as a human-readable size."""
    n = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _estimate_seconds(blocks: list) -> float:
    """Rough narration duration for a set of blocks, from character count."""
    chars = sum(len(b.get("text", "")) for b in blocks)
    return chars / CHARS_PER_SECOND + AVG_BLOCK_PAUSE_S * len(blocks)


# ─────────────────────────────── Progress factories ───────────────────────────

class _SmoothETA(ProgressColumn):
    """ETA column with an exponential moving average of per-step time.

    Rich's default estimate divides total elapsed by progress fraction, so it
    swings wildly early on and never recovers from a slow start.  This tracks
    the time between completed steps with an EMA, giving a stable estimate from
    the very first step and adapting smoothly to speed changes.
    """

    def __init__(self, alpha: float = 0.25):
        super().__init__()
        self._alpha = alpha
        self._state: dict = {}   # task.id -> {"c", "t", "ema"}

    def render(self, task) -> Text:
        if not task.total:
            return Text("~ --:--", style="cyan")
        if task.completed >= task.total:
            return Text("~ 0:00", style="cyan")
        now = time.monotonic()
        st  = self._state.get(task.id)
        if st is None:
            self._state[task.id] = {"c": task.completed, "t": now, "ema": None}
            return Text("~ --:--", style="cyan")
        if task.completed < st["c"]:          # bar was reset (e.g. next part)
            st["c"], st["t"] = task.completed, now
        elif task.completed > st["c"]:
            dt    = now - st["t"]
            steps = task.completed - st["c"]
            if dt > 0:
                per      = dt / steps
                st["ema"] = per if st["ema"] is None else \
                    self._alpha * per + (1 - self._alpha) * st["ema"]
            st["c"], st["t"] = task.completed, now
        if st["ema"] is None:
            return Text("~ --:--", style="cyan")
        remaining = max(0.0, (task.total - task.completed) * st["ema"])
        return Text(f"~ {_fmt_hms(remaining)}", style="cyan")


class StackedProgress:
    """Progress display where each task's label sits on its own line.

        ⠧ Book  (1 part left)
          ━━━━━━━━━━  45/320  14%  0:01:23  ~ 8:42
        ⠧ part_01  1/2 parts
          ━━━━━━━━━━  45/120  37%  0:01:23  ~ 2:05

    Mimics the subset of the rich ``Progress`` API used in this app so call
    sites read the same; rendering is driven by a ``Live`` via ``__rich__``.
    """

    def __init__(self, **kwargs):
        self.progress = Progress(
            SpinnerColumn(),
            BarColumn(bar_width=40),
            MofNCompleteColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            _SmoothETA(),
            console=console,
        )
        self._labels: dict = {}
        self._live = Live(
            self, console=console, refresh_per_second=10, transient=False, **kwargs
        )

    # ── rendering ──────────────────────────────────────────────────────────
    def __rich__(self) -> Group:
        rows = []
        for task in self.progress.tasks:
            if not task.visible:
                continue
            label = self._labels.get(task.id, task.description)
            rows.append(Text.from_markup(f"  {label}"))
            rows.append(Padding(self.progress.make_tasks_table([task]), (0, 0, 0, 2)))
        return Group(*rows)

    # ── Progress-like API ──────────────────────────────────────────────────
    def add_task(self, description, total=None, **fields):
        tid = self.progress.add_task(description, total=total, **fields)
        self._labels[tid] = description
        return tid

    def advance(self, task_id, advance=1):
        self.progress.advance(task_id, advance)

    def update(self, task_id, *, description=None, **kwargs):
        if description is not None:
            self._labels[task_id] = description
        self.progress.update(task_id, description=description, **kwargs)

    def reset(self, task_id, **kwargs):
        self.progress.reset(task_id, **kwargs)

    def print(self, *args, **kwargs):
        self._live.console.print(*args, **kwargs)

    @property
    def tasks(self):
        return self.progress.tasks

    # ── context manager ────────────────────────────────────────────────────
    def __enter__(self):
        self._live.__enter__()
        return self

    def __exit__(self, *exc):
        self._live.__exit__(*exc)


def _make_progress(**kwargs) -> StackedProgress:
    """Standard stacked-label progress display used everywhere in the app."""
    return StackedProgress(**kwargs)


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

def ask_engine() -> str:
    """Ask which TTS engine to use. Returns ENGINE_PIPER or ENGINE_XTTS."""
    table = Table(box=box.SIMPLE, show_edge=False, pad_edge=False, padding=(0, 2))
    table.add_column("#", style="bold cyan", justify="right")
    table.add_column("Engine", style="bold")
    table.add_column("Speed", justify="left")
    table.add_column("Quality", justify="left")
    table.add_row("1", "Piper", "[green]Fast[/green] (≈ real-time on CPU)", "Good, clear")
    table.add_row("2", "XTTS",  "[red]Slow[/red] on CPU (no GPU here)", "Highest / most natural")

    console.print("\n  [bold]Choose a voice engine:[/bold]")
    console.print(table)
    console.print("  [dim]Tip: on CPU, Piper is ~10–15× faster than XTTS for very similar audiobook quality.[/dim]")
    choice = Prompt.ask("\n  [cyan]Engine[/cyan]", choices=["1", "2"], default="1")
    return ENGINE_PIPER if choice == "1" else ENGINE_XTTS


def ask_piper_voice() -> str:
    """Ask which Piper voice to use. Returns a voice key (e.g. en_US-amy-medium)."""
    keys = list(PIPER_VOICES)

    table = Table(box=box.SIMPLE, show_edge=False, pad_edge=False, padding=(0, 2))
    table.add_column("#", style="bold cyan", justify="right")
    table.add_column("Voice", style="bold")
    table.add_column("Accent")
    table.add_column("Gender")
    table.add_column("Tier")
    table.add_column("Speed / notes", style="dim")

    tier_speed = {"low": "[green]fastest[/green]", "medium": "[green]fast[/green]", "high": "[yellow]slower[/yellow]"}
    for i, key in enumerate(keys, start=1):
        label, accent, gender, tier, note = PIPER_VOICES[key]
        table.add_row(str(i), label, accent, gender, f"{tier_speed.get(tier, '')} {tier}", note)

    console.print("\n  [bold]Choose a Piper voice:[/bold]")
    console.print(table)
    console.print("  [dim]Tip: 'medium' tiers run ~22× real-time; 'high' ~5×; 'low' ~28× (lower fidelity).[/dim]")
    console.print("  [dim]You can also type any Piper voice key, e.g. en_GB-jenny_dioco-medium.[/dim]")

    default_idx = str(keys.index(DEFAULT_PIPER_VOICE) + 1)
    while True:
        raw = Prompt.ask("\n  [cyan]Voice[/cyan]", default=default_idx).strip()
        if raw.lower() == "q":
            _confirm_quit()
            continue
        if raw.isdigit() and 1 <= int(raw) <= len(keys):
            return keys[int(raw) - 1]
        if "-" in raw and raw.lower().startswith("en"):   # looks like a voice key
            return raw
        console.print(f"  [yellow]Enter 1–{len(keys)} or a valid voice key.[/yellow]")


def ask_output_dir(default: str = "output") -> str:
    """Prompt for the base output directory (a per-book subfolder goes inside)."""
    console.print("\n  [dim]Output directory — a subfolder named after the book is created inside it.[/dim]")
    while True:
        raw = Prompt.ask("  [cyan]Output directory[/cyan]", default=default).strip().strip('"').strip("'")
        if raw.lower() == "q":
            _confirm_quit()
            continue
        return raw or default


def _safe_mkdir(path: Path) -> bool:
    """Create a directory and confirm it is writable. Friendly errors, no crash."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except PermissionError:
        console.print(
            f"  [red]Permission denied:[/red] cannot write to [bold]{path}[/bold].\n"
            f"  Pick a different folder (Settings → output_dir) or run with the right permissions."
        )
    except OSError as e:
        console.print(
            f"  [red]Cannot use output directory[/red] [bold]{path}[/bold]: {e}.\n"
            f"  Check the path (drive exists, name is valid) or change it in Settings."
        )
    return False


def _resolve_output_base(prefs: dict) -> Path | None:
    """Return the base output dir from prefs, prompting if blank. None on cancel."""
    base = (prefs.get("output_dir") or "").strip()
    if not base:
        base = ask_output_dir()
    p = Path(base).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    return p


def ask_break_points(total_units: int, unit_name: str = "page") -> list:
    """
    Ask the user where to split the audio output.
    Returns a sorted list of integers (each is the *last* unit in that part),
    or [] to produce a single audio file.

    Example  total_units=300, unit_name="page":
        Input : 100, 200
        Effect: part_01 → pages 1-100  |  part_02 → 101-200  |  part_03 → 201-300
    """
    console.print(f"\n  [dim]This book has [bold]{total_units}[/bold] {unit_name}s.[/dim]")
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

    # Safety net: if Jenks put >50% of blocks into "other", those blocks ARE the
    # body text — a real book can't be 75% footnotes.  The lowest Jenks break
    # simply landed inside the body-text cluster (common when footnotes are rare
    # or absent).  Automatically promote rather than silencing most of the book.
    narrated = sum(1 for b in blocks if b["label"] != "other")
    skipped  = len(blocks) - narrated
    if skipped > narrated:
        console.print(
            f"  [dim]Jenks put {skipped}/{len(blocks)} blocks in the lowest cluster "
            f"(no distinct footnote size found) — promoting to body text.[/dim]"
        )
        for b in blocks:
            if b["label"] == "other":
                b["label"] = "body"

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

        chapter = 0
        for item in items:
            soup = BeautifulSoup(item.get_content(), "lxml")
            els  = [el for el in soup.find_all(["h1", "h2", "h3", "h4", "p"])
                    if el.get_text(" ", strip=True)]
            if els:
                chapter += 1   # number only chapters that actually have text
            for el in els:
                lbl = (
                    "header"  if el.name == "h1"               else
                    "caption" if el.name in ("h2", "h3", "h4") else
                    "body"
                )
                blocks.append({
                    "text":    el.get_text(" ", strip=True),
                    "label":   lbl,
                    "chapter": chapter,
                })
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
            level = len(m.group(1))
            lbl   = "header" if level == 1 else "caption"
            blocks.append({
                "text":          m.group(2).strip(),
                "label":         lbl,
                "heading_level": level,
            })
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

def _assign_units(narrated: list, source_fmt: str, prefs: dict) -> list:
    """Assign each narrated block a 1-based 'unit' number for split points.

    The unit depends on format + prefs:
      PDF                      → page number
      EPUB  (epub_split)       → chapter   | paragraph (block index)
      MD    (md_split)         → section   | paragraph (block index)
      TXT                      → paragraph (block index)
    Section numbers for Markdown advance at each heading whose level is
    <= len(md_heading_marker).
    """
    if source_fmt == "pdf":
        return [b.get("page", 1) for b in narrated]

    if source_fmt == "epub" and prefs.get("epub_split", "chapter") == "chapter":
        return [b.get("chapter", 1) for b in narrated]

    if source_fmt == "md" and prefs.get("md_split", "heading") == "heading":
        level = len(prefs.get("md_heading_marker", "#"))
        units, sec = [], 0
        for b in narrated:
            hl = b.get("heading_level")
            if hl is not None and hl <= level:
                sec += 1
            units.append(max(sec, 1))
        return units

    # paragraph / block-index fallback (TXT, and paragraph mode for EPUB/MD)
    return list(range(1, len(narrated) + 1))


def _break_unit_info(blocks: list, source_fmt: str, prefs: dict) -> tuple:
    """Return (total_units, unit_name) for the split-point prompt."""
    narrated = [b for b in blocks if b.get("label") != "other"]
    if source_fmt == "pdf":
        return max((b.get("page", 0) for b in blocks), default=1), "page"
    if source_fmt == "epub":
        if prefs.get("epub_split", "chapter") == "chapter":
            return max((b.get("chapter", 1) for b in narrated), default=1), "chapter"
        return len(narrated), "block"
    if source_fmt == "md":
        if prefs.get("md_split", "heading") == "heading":
            units = _assign_units(narrated, "md", prefs)
            return (max(units) if units else 1), "section"
        return len(narrated), "block"
    if source_fmt == "txt":
        return len(narrated), "paragraph"
    return 1, "part"


def _group_into_parts(blocks: list, break_points: list, source_fmt: str, prefs: dict | None = None) -> list:
    """
    Returns [(part_name, [blocks]), ...].

    break_points are unit numbers (page/chapter/section/block depending on the
    format + prefs); each is the inclusive end of a part.  Empty → single file.
    """
    prefs    = prefs or {}
    narrated = [b for b in blocks if b.get("label") != "other"]
    if not narrated:
        return [("part_01", [])]
    if not break_points:
        return [("part_01", narrated)]

    units = _assign_units(narrated, source_fmt, prefs)
    parts = []
    prev  = 0
    for bp in sorted(set(break_points)) + [max(units)]:
        group = [b for b, u in zip(narrated, units) if prev < u <= bp]
        if group:
            parts.append((f"part_{len(parts) + 1:02d}", group))
        prev = bp

    return parts or [("part_01", narrated)]


# ─────────────────────────────── TTS helpers ──────────────────────────────────

def _split_text(text: str, max_len: int = 120) -> list:
    """Split text into TTS-friendly chunks of at most max_len characters.

    XTTS2 has a hard 400-token limit but its tokenizer phoneme-expands text,
    so the character-to-token ratio is >1.  120 chars gives comfortable headroom.
    """
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


def _tts_chunk(tts, text: str, path: str, speed: float = 1.0, _depth: int = 0) -> None:
    """Write a single TTS chunk to *path*, halving the text if XTTS rejects it."""
    try:
        tts.tts_to_file(text=text, file_path=path, speaker="Adde Michal",
                        language="en", speed=speed)
    except AssertionError:
        if _depth >= 4 or len(text) < 10:
            raise
        mid   = len(text) // 2
        split = text.rfind(" ", 0, mid) or mid
        left, right = text[:split].strip(), text[split:].strip()
        base  = path.rsplit(".", 1)[0]
        lp, rp = f"{base}_L.wav", f"{base}_R.wav"
        _tts_chunk(tts, left,  lp, speed, _depth + 1)
        _tts_chunk(tts, right, rp, speed, _depth + 1)
        combined = AudioSegment.from_wav(lp) + AudioSegment.from_wav(rp)
        combined.export(path, format="wav")
        Path(lp).unlink(missing_ok=True)
        Path(rp).unlink(missing_ok=True)


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
    # No console.status here: this runs inside a Live progress display, and rich
    # permits only one live region at a time.  -c copy is near-instant anyway.
    result = subprocess.run(cmd, capture_output=True, text=True)
    list_file.unlink(missing_ok=True)
    if result.returncode != 0:
        console.print(f"  [red]FFmpeg error:[/red]\n{result.stderr[-600:]}")
        return False
    return True


# ─────────────────────────────── TTS engines ──────────────────────────────────

def _engine_sample_rate(engine_name: str, voice: str = DEFAULT_PIPER_VOICE) -> int:
    """Nominal output sample rate, used for pre-flight size estimates."""
    if engine_name == ENGINE_XTTS:
        return XTTS_SAMPLE_RATE
    return _piper_voice_sr(voice)


def _ensure_piper_voice(voice: str = DEFAULT_PIPER_VOICE) -> Path:
    """Return the local path to a Piper voice model, downloading it if missing."""
    from piper.download_voices import download_voice

    PIPER_CACHE.mkdir(parents=True, exist_ok=True)
    onnx = PIPER_CACHE / f"{voice}.onnx"
    if not onnx.exists() or not (PIPER_CACHE / f"{voice}.onnx.json").exists():
        with console.status(f"[cyan]Downloading Piper voice '{voice}' (~60 MB, one time)…[/cyan]"):
            download_voice(voice, PIPER_CACHE)
    return onnx


class PiperEngine:
    """Fast neural TTS. Runs several times faster than real-time on CPU."""

    name = ENGINE_PIPER

    def __init__(self, voice: str = DEFAULT_PIPER_VOICE, speed: float = 1.0):
        from piper import PiperVoice, SynthesisConfig

        model_path = _ensure_piper_voice(voice)
        self.voice = PiperVoice.load(str(model_path))
        self.sample_rate = self.voice.config.sample_rate
        self.device = "cpu"
        # length_scale > 1 slows speech, < 1 speeds it up — inverse of speed.
        self._syn = SynthesisConfig(length_scale=1.0 / max(speed, 0.1)) if speed != 1.0 else None

    def chunks(self, text: str) -> list:
        # Piper splits long text into sentences internally — one chunk per block.
        return [text]

    def synth(self, text: str, out_path: str) -> None:
        text = (text or "").strip()
        with _wave.open(out_path, "wb") as wf:
            # Set the format up front so the file is always a valid WAV — even
            # when the text yields no audio (blank lines, "---" rules, lone "#"
            # from markdown, etc.).  Otherwise wave.close() raises
            # "# channels not specified".
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.sample_rate)
            if text:
                self.voice.synthesize_wav(text, wf, syn_config=self._syn, set_wav_format=False)


class XttsEngine:
    """Coqui XTTS v2. Highest quality / most natural, but slow on CPU."""

    name = ENGINE_XTTS

    def __init__(self, speed: float = 1.0):
        from TTS.api import TTS as CoquiTTS
        import torch

        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.tts = CoquiTTS(
            "tts_models/multilingual/multi-dataset/xtts_v2",
            progress_bar=False,
        ).to(self.device)
        self.sample_rate = XTTS_SAMPLE_RATE
        self.speed = speed

    def chunks(self, text: str) -> list:
        return _split_text(text)

    def synth(self, text: str, out_path: str) -> None:
        if not (text or "").strip():
            # XTTS errors on empty input — emit a valid silent WAV instead.
            with _wave.open(out_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(self.sample_rate)
            return
        _tts_chunk(self.tts, text, out_path, speed=self.speed)


def _load_engine(engine_name: str, voice: str = DEFAULT_PIPER_VOICE, speed: float = 1.0):
    """Construct and return the chosen TTS engine, with status messaging."""
    try:
        if engine_name == ENGINE_XTTS:
            with console.status("[cyan]Loading XTTS model (first run downloads ~1.8 GB)…[/cyan]"):
                engine = XttsEngine(speed=speed)
            console.print(f"  [dim]Device: {engine.device.upper()}[/dim]")
        else:
            _ensure_piper_voice(voice)   # downloads under its own status (run before the next)
            with console.status(f"[cyan]Loading Piper voice '{voice}'…[/cyan]"):
                engine = PiperEngine(voice, speed=speed)   # voice already present → no nested status
    except ImportError as e:
        pkg = "coqui-tts (+ torch)" if engine_name == ENGINE_XTTS else "piper-tts"
        console.print(
            f"\n  [red]The {engine_name.upper()} engine isn't installed[/red] "
            f"([dim]{e}[/dim]).\n"
            f"  Install it with:  [bold]pip install {pkg}[/bold]   "
            f"(or run [bold]python install.py[/bold])\n"
        )
        raise SystemExit(1)
    console.print("  [green]✓ TTS engine ready[/green]\n")
    return engine


# ─────────────────────────────── Audio generation ─────────────────────────────

def generate_audio(
    blocks:       list,
    output_dir:   Path,
    break_points: list | None = None,
    source_fmt:   str  = "",
    engine_name:  str  = ENGINE_PIPER,
    voice:        str  = DEFAULT_PIPER_VOICE,
    prefs:        dict | None = None,
):
    """
    Synthesise all parts with a two-level progress display:

        Overall  ░░░░░░░░░░░░░░░░░░░░  45/320  14%  0:01:23  ETA 8:42
        part_01  ░░░░░░░░░░░░░░░░░░░░  45/120  37%  0:01:23  ETA 2:05

    Each part is written to part_NN.wav.  Per-block temp WAVs (_p01b0000.wav)
    allow block-level resume: interrupt at any point and restart to continue.
    """
    from pydub import AudioSegment

    if break_points is None:
        break_points = []
    prefs  = prefs or _default_prefs()
    pauses = prefs_pauses(prefs)
    speed  = float(prefs.get("narration_speed", 1.0))

    wav_bps = _engine_sample_rate(engine_name, voice) * 2   # bytes/sec for size estimate

    all_parts = _group_into_parts(blocks, break_points, source_fmt, prefs)

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

    # ── Estimated output size / duration ──────────────────────────────────────
    est_table = Table(box=box.SIMPLE, show_edge=False, pad_edge=False, padding=(0, 2))
    est_table.add_column("Part", style="cyan", no_wrap=True)
    est_table.add_column("Blocks", justify="right")
    est_table.add_column("Est. duration", justify="right")
    est_table.add_column("Est. size (WAV)", justify="right", style="green")

    total_secs = 0.0
    for name, pblocks in all_parts:
        secs = _estimate_seconds(pblocks)
        total_secs += secs
        est_table.add_row(
            name,
            str(len(pblocks)),
            _fmt_hms(secs),
            _human_size(secs * wav_bps),
        )
    out_fmt    = prefs.get("audio_format", "mp3")
    # FLAC is lossless (~55% of WAV); MP3 is a fixed bitrate.
    joined_bps = MP3_BYTES_PER_SEC if out_fmt == "mp3" else wav_bps * 0.55
    console.print(est_table)
    console.print(
        f"  [green]Estimated total:[/green] {_fmt_hms(total_secs)}  ·  "
        f"~{_human_size(total_secs * wav_bps)} across part WAVs  ·  "
        f"~{_human_size(total_secs * joined_bps)} once joined to {out_fmt.upper()}"
    )
    console.print("  [dim]Estimates are approximate (≈150 wpm); actual sizes shown as each part finishes.[/dim]\n")

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

    # ── Load TTS engine ───────────────────────────────────────────────────────
    if engine_name == ENGINE_PIPER:
        console.print(f"  [dim]Engine: PIPER · voice {voice}[/dim]")
    else:
        console.print(f"  [dim]Engine: XTTS[/dim]")
    engine = _load_engine(engine_name, voice, speed)

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
                    chunks = engine.chunks(block["text"])
                    tmp_files = []

                    for j, chunk in enumerate(chunks):
                        tmp = output_dir / f"_{tag}b{i:04d}_c{j:03d}.wav"
                        engine.synth(chunk, str(tmp))
                        tmp_files.append(tmp)

                    # Merge TTS chunks into one block WAV (pydub handles micro-pauses)
                    block_audio = AudioSegment.silent(0)
                    for ci, cf in enumerate(tmp_files):
                        seg   = AudioSegment.from_wav(str(cf))
                        pause = pauses.get(label, 200) if ci == len(tmp_files) - 1 else 50
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

def join_audio(output_dir: Path, prefs: dict | None = None):
    """
    Concatenate all part WAVs → audiobook.<format>.

    Format and MP3 bitrate come from prefs (audio_format / mp3_bitrate).
    Shows a real-time FFmpeg encoding progress bar: measures the total audio
    duration of all source WAVs, then streams FFmpeg's out_time_us output
    to display X% done + ETA.
    """
    prefs = prefs or _default_prefs()
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

    fmt = prefs.get("audio_format", "mp3")
    if fmt == "flac":
        out_file = output_dir / "audiobook.flac"
        codec    = ["-c:a", "flac"]
    else:
        out_file = output_dir / "audiobook.mp3"
        codec    = ["-c:a", "libmp3lame", "-b:a", prefs.get("mp3_bitrate", "192k")]

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(file_list),
        *codec,
        str(out_file),
    ]

    exit_code, stderr = _ffmpeg_progress(
        cmd,
        total_s=total_s,
        label=f"Encoding {fmt.upper()}  ({len(wav_files)} parts)",
    )

    file_list.unlink(missing_ok=True)

    if exit_code == 0:
        mb = out_file.stat().st_size / 1024 ** 2
        console.print(
            f"\n  [green]✓ Saved:[/green] [bold]{out_file}[/bold]  ({mb:.1f} MB)"
        )
    else:
        console.print(f"  [red]FFmpeg error:[/red]\n{stderr[-800:]}")


# ─────────────────────────────── Main flows ───────────────────────────────────

def _flow_convert():
    _banner()
    console.print(Panel("[bold]Convert Book to Audiobook[/bold]", border_style="blue", padding=(0, 2)))
    console.print()

    prefs = load_prefs()

    path       = _pick_file()
    book_name  = path.stem
    source_fmt = path.suffix.lower().lstrip(".")

    base = _resolve_output_base(prefs)
    output_dir = base / book_name
    if not _safe_mkdir(output_dir):
        Prompt.ask("\n  Press Enter to return to menu")
        return

    console.print(f"\n  [bold]Book:[/bold]   {path.name}")
    console.print(f"  [bold]Format:[/bold] {path.suffix.upper().lstrip('.')}")
    console.print(f"  [bold]Output:[/bold] {output_dir}\n")

    classified_json = output_dir / "classified_text.json"
    config_json     = output_dir / "generation_config.json"
    blocks          = None
    break_points    = []
    engine          = ENGINE_PIPER
    voice           = DEFAULT_PIPER_VOICE

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
                engine       = cfg.get("engine", ENGINE_PIPER)
                voice        = cfg.get("voice", DEFAULT_PIPER_VOICE)
                # Keep audio consistent with already-rendered parts.
                prefs["narration_speed"] = cfg.get("narration_speed", prefs.get("narration_speed", 1.0))
                bp_label     = ", ".join(str(b) for b in break_points) or "none"
                eng_label    = f"{engine.upper()}" + (f" · {voice}" if engine == ENGINE_PIPER else "")
                console.print(
                    f"  [cyan]Loaded {len(blocks)} blocks "
                    f"with break points: [{bp_label}]  ·  engine: {eng_label}[/cyan]\n"
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

        # ── Ask for break points (all formats; unit depends on format + prefs) ─
        total_units, unit_name = _break_unit_info(blocks, source_fmt, prefs)
        if total_units > 1:
            break_points = ask_break_points(total_units, unit_name)

        # ── Choose the voice engine (and voice, for Piper) ─────────────────────
        engine = prefs.get("default_engine") or ask_engine()
        if engine == ENGINE_PIPER:
            voice = prefs.get("default_voice") or ask_piper_voice()

        config_json.write_text(
            json.dumps({
                "break_points":    break_points,
                "source_fmt":      source_fmt,
                "engine":          engine,
                "voice":           voice,
                "narration_speed": float(prefs.get("narration_speed", 1.0)),
            }),
            encoding="utf-8",
        )

    # ── Generate audio ────────────────────────────────────────────────────────
    generate_audio(
        blocks, output_dir,
        break_points=break_points, source_fmt=source_fmt,
        engine_name=engine, voice=voice, prefs=prefs,
    )

    # ── Join (automatically or on request, per prefs.auto_join) ────────────────
    part_wavs = list(output_dir.glob("part_*.wav"))
    if part_wavs:
        fmt = prefs.get("audio_format", "mp3").upper()
        if prefs.get("auto_join", True):
            join_audio(output_dir, prefs)
        elif Confirm.ask(f"\n  [cyan]Join all parts into one {fmt} now?[/cyan]", default=True):
            join_audio(output_dir, prefs)

    Prompt.ask("\n  Press Enter to return to menu")


def _find_audiobook_dirs(prefs: dict) -> list:
    """Folders containing part_*.wav / block_*.wav, searched across likely roots."""
    roots = [Path.cwd(), Path.cwd() / "output"]
    base  = (prefs.get("output_dir") or "").strip()
    if base:
        bp = Path(base).expanduser()
        roots.append(bp if bp.is_absolute() else Path.cwd() / bp)

    found, seen = [], set()
    for root in roots:
        if not root.exists():
            continue
        for d in sorted(root.iterdir()):
            if d in seen or not d.is_dir():
                continue
            if any(d.glob("part_*.wav")) or any(d.glob("block_*.wav")):
                seen.add(d)
                found.append(d)
    return found


def _flow_join():
    _banner()
    console.print(Panel("[bold]Join Audio Parts  →  Audiobook[/bold]", border_style="blue", padding=(0, 2)))
    console.print()

    prefs      = load_prefs()
    candidates = _find_audiobook_dirs(prefs)

    if not candidates:
        console.print("  [yellow]No audiobook folders found (looked in current dir and output/).[/yellow]")
        Prompt.ask("\n  Press Enter to return to menu")
        return

    out_name = f"audiobook.{prefs.get('audio_format', 'mp3')}"
    t = Table(box=box.SIMPLE_HEAD, border_style="dim", show_header=True, padding=(0, 2))
    t.add_column("#",      style="bold cyan", justify="right", width=4)
    t.add_column("Folder", style="white")
    t.add_column("Parts",  style="green", justify="center", width=7)
    t.add_column("Status", style="dim",   width=14)
    for i, d in enumerate(candidates, 1):
        n = len(list(d.glob("part_*.wav"))) or len(list(d.glob("block_*.wav")))
        status = "joined" if (d / out_name).exists() else ""
        t.add_row(str(i), d.name, str(n), status)

    console.print(Panel(t, title="[bold]Audiobook Folders[/bold]", border_style="blue"))

    while True:
        raw = Prompt.ask("  [cyan]Select folder[/cyan]").strip()
        if raw.lower() == "q":
            _confirm_quit()
            continue
        if raw.isdigit() and 1 <= int(raw) <= len(candidates):
            output_dir = candidates[int(raw) - 1]
            break
        console.print(f"  [red]Enter a number between 1 and {len(candidates)}.[/red]")

    join_audio(output_dir, prefs)
    Prompt.ask("\n  Press Enter to return to menu")


def _pref_display(key: str, value) -> str:
    """Human-friendly current-value text for the settings table."""
    if value == "" and key in PREFS_BLANK_PROMPTS:
        return "[dim](ask every run)[/dim]"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _edit_pref(prefs: dict, key: str) -> None:
    """Prompt the user for a new value for one preference, with validation."""
    default, desc, rule = PREFS_SCHEMA[key]
    console.print(f"\n  [bold cyan]{key}[/bold cyan]")
    console.print(f"  [dim]{desc}[/dim]")
    console.print(f"  Current: [bold]{_pref_display(key, prefs[key])}[/bold]\n")

    # Choice settings → numbered menu
    if isinstance(rule, list):
        for i, opt in enumerate(rule, 1):
            label = "(ask every run)" if opt == "" else ("yes" if opt is True else "no" if opt is False else str(opt))
            console.print(f"    [{i}] {label}")
        cur = str(rule.index(prefs[key]) + 1) if prefs[key] in rule else "1"
        while True:
            raw = Prompt.ask("\n  New value (number, Enter to keep)", default="").strip() or cur
            if raw.isdigit() and 1 <= int(raw) <= len(rule):
                prefs[key] = rule[int(raw) - 1]
                return
            console.print(f"  [red]Enter 1–{len(rule)}.[/red]")
        return

    # Free-value settings
    hint = " ('-' to clear)" if key in PREFS_BLANK_PROMPTS else ""
    while True:
        raw = Prompt.ask(f"  New value (Enter to keep){hint}", default="").strip()
        if raw == "":
            return
        newval = "" if raw == "-" else raw
        ok, val = _coerce_pref(key, newval)
        if ok:
            prefs[key] = val
            return
        console.print("  [red]Invalid value for this setting — try again.[/red]")


def _flow_settings():
    keys = list(PREFS_SCHEMA)
    while True:
        _banner()
        console.print(Panel("[bold]Settings[/bold]", border_style="blue", padding=(0, 2)))
        prefs = load_prefs()

        t = Table(box=box.SIMPLE_HEAD, border_style="dim", show_header=True, padding=(0, 2))
        t.add_column("#",       style="bold cyan", justify="right", width=4)
        t.add_column("Setting", style="white", no_wrap=True)
        t.add_column("Value",   style="green")
        t.add_column("What it does", style="dim")
        for i, k in enumerate(keys, 1):
            t.add_row(str(i), k, _pref_display(k, prefs[k]), PREFS_SCHEMA[k][1])
        console.print(t)
        console.print(f"  [dim]Saved at: {PREFS_PATH}[/dim]")
        console.print("  [dim]Enter a number to edit · [bold]r[/bold] reset all · [bold]b[/bold] back · [bold]q[/bold] quit[/dim]")

        try:
            raw = Prompt.ask("\n  [cyan]Choice[/cyan]").strip().lower()
        except (KeyboardInterrupt, EOFError):
            _confirm_quit()
            continue

        if raw in ("b", ""):
            return
        if raw == "q":
            _confirm_quit()
            continue
        if raw == "r":
            if Confirm.ask("  [yellow]Reset all settings to defaults?[/yellow]", default=False):
                save_prefs(_default_prefs())
                console.print("  [green]Settings reset.[/green]")
            continue
        if raw.isdigit() and 1 <= int(raw) <= len(keys):
            _edit_pref(prefs, keys[int(raw) - 1])
            save_prefs(prefs)
            console.print("  [green]Saved.[/green]")
        else:
            console.print(f"  [red]Enter 1–{len(keys)}, r, b, or q.[/red]")


# ─────────────────────────────── Entry point ──────────────────────────────────

def _report_error(exc: Exception) -> None:
    """Show a clean error message and write the full traceback to error.log."""
    import traceback
    log = HERE / "error.log"
    try:
        log.write_text("".join(traceback.format_exception(exc)), encoding="utf-8")
        where = f"  [dim]Full details written to {log}[/dim]"
    except OSError:
        where = ""
    console.print(
        f"\n  [red]Something went wrong:[/red] {type(exc).__name__}: {exc}\n{where}"
    )


def main():
    try:
        while True:
            _banner()
            choice = _menu(
                "MAIN MENU",
                [
                    "Convert Book to Audiobook",
                    "Join Audio Parts  →  Audiobook",
                    "Settings",
                    "Exit",
                ],
            )
            try:
                if choice == 1:
                    _flow_convert()
                elif choice == 2:
                    _flow_join()
                elif choice == 3:
                    _flow_settings()
                else:
                    console.print("\n  [dim]Goodbye![/dim]\n")
                    break
            except (KeyboardInterrupt, EOFError, SystemExit):
                raise
            except Exception as exc:   # one bad run returns to the menu, no crash
                _report_error(exc)
                Prompt.ask("\n  Press Enter to return to menu")
    except (KeyboardInterrupt, EOFError):
        console.print("\n\n  [dim]Goodbye![/dim]\n")
        sys.exit(0)


if __name__ == "__main__":
    main()
