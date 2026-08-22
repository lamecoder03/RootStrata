"""Renders an already-written markdown findings report as a self-contained HTML page.

Charts are inlined, an optional verdict badge is added, and the stylesheet supports print-to-PDF.
Formatting only: it never runs the agent and never touches the audit log, and a fidelity check
asserts that every word of the source markdown appears in the rendered page.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import mimetypes
import os
import re
import sys
from pathlib import Path

# --- palette -------------------------------------------------------------------------------------
# Inherited from generate_report.py so a chart sits on the page in matching colours: same paper
# (#fcfcfb), same ink, same blue accent. The three verdict colours are (ink, tint, border) each.
# Measured: ink on its own tint 5.03-5.65:1, ink on the page 5.71-6.32:1, all above the 4.5 floor.
# The three inks are within 1.11:1 of each other in luminance and so are not separable without
# colour, which is why the badge always spells the verdict out. Ink and tint are set together
# inline, so the badge keeps its light tint in dark mode.
VERDICT_STYLES = {
    "PASS":    ("#1f6b41", "#e7f2eb", "#bcdccb"),
    "PARTIAL": ("#8a5300", "#fbf0dc", "#e8d3a8"),
    "FAIL":    ("#b8352b", "#fbeae8", "#eec4bf"),
}

PROJECT_NAME = "RootStrata"
PROJECT_TAGLINE = "Autonomous Dataset Insight Agent"

# Words that must survive into the page. Link and image destinations are the one exception: they
# move out of the text and into an href/src attribute.
_WORD = re.compile(r"[\w][\w.\-]*", re.UNICODE)
_DESTINATION = re.compile(r"\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_LIST_MARKER = re.compile(r"^\s*(?:[-*+]|\d{1,9}[.)])\s+", re.MULTILINE)


# --- inline markdown ------------------------------------------------------------------------------

def _render_inline(text: str, resolve_image=None) -> str:
    """Render code spans, images, links, bold, italic and hard line breaks.

    Anything carrying a destination is stashed behind a sentinel before escaping runs, so a URL
    cannot be consumed by a later pattern.
    """
    stash: list[str] = []

    def keep(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    # Code first: its content is literal, so nothing inside it may be re-interpreted.
    text = re.sub(r"`([^`]+)`", lambda m: keep(f"<code>{html.escape(m.group(1))}</code>"), text)

    def image(match: re.Match) -> str:
        alt, src = match.group(1), match.group(2)
        resolved = resolve_image(src) if resolve_image else src
        return keep(f'<img src="{html.escape(resolved, quote=True)}" '
                    f'alt="{html.escape(alt, quote=True)}" loading="lazy">')

    text = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", image, text)

    def link(match: re.Match) -> str:
        # Only the tags are stashed; the label stays in the stream so it is still escaped and
        # emphasised like any other text.
        href = html.escape(match.group(2), quote=True)
        return keep(f'<a href="{href}">') + match.group(1) + keep("</a>")

    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", link, text)

    text = html.escape(text)
    text = re.sub(r"[ \t]{2,}\n", "<br>\n", text)                     # markdown's hard line break
    text = _emphasis(text)

    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)


_DELIM_RUN = re.compile(r"[*_]+")


def _emphasis(text: str) -> str:
    """Resolve bold and italic with a delimiter-run stack.

    Each closer is matched to its nearest opener, consuming two asterisks for strong and one for
    em. This handles nesting such as `**Outlier detection on *output_points***`, which nested
    regexes render with crossed tags, and it leaves `ad_spend_usd` alone: an underscore between two
    word characters is neither left- nor right-flanking, so it cannot open emphasis.
    """
    def punctuation(char: str) -> bool:
        return not char.isalnum() and not char.isspace()

    tokens: list[dict] = []
    pos = 0
    for run in _DELIM_RUN.finditer(text):
        if run.start() > pos:
            tokens.append({"text": text[pos:run.start()]})
        before = text[run.start() - 1] if run.start() else " "
        after = text[run.end()] if run.end() < len(text) else " "
        left = (not after.isspace()
                and (not punctuation(after) or before.isspace() or punctuation(before)))
        right = (not before.isspace()
                 and (not punctuation(before) or after.isspace() or punctuation(after)))
        char = run.group(0)[0]
        tokens.append({
            "run": run.group(0), "char": char, "open": "", "close": "",
            # `_` is stricter than `*`: it may not open or close inside a word.
            "can_open": left if char == "*" else (left and (not right or punctuation(before))),
            "can_close": right if char == "*" else (right and (not left or punctuation(after))),
        })
        pos = run.end()
    if pos < len(text):
        tokens.append({"text": text[pos:]})

    for index, closer in enumerate(tokens):
        if "run" not in closer:
            continue
        while closer["can_close"] and closer["run"]:
            back = index - 1
            while back >= 0:
                opener = tokens[back]
                if ("run" in opener and opener["run"] and opener["can_open"]
                        and opener["char"] == closer["char"]):
                    break
                back -= 1
            if back < 0:
                break                       # no opener: the run stays literal, and may open later
            opener = tokens[back]
            width = 2 if len(opener["run"]) >= 2 and len(closer["run"]) >= 2 else 1
            tag = "strong" if width == 2 else "em"
            opener["run"] = opener["run"][:-width]
            closer["run"] = closer["run"][width:]
            # Openers prepend and closers append, so the pair matched first ends up innermost.
            opener["open"] = f"<{tag}>" + opener["open"]
            closer["close"] = closer["close"] + f"</{tag}>"

    return "".join(token["text"] if "text" in token
                   else token["close"] + token["run"] + token["open"] for token in tokens)


# --- block markdown -------------------------------------------------------------------------------

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")
_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_LIST = re.compile(r"^(\s*)(?:([-*+])|(\d{1,9})[.)])\s+(.*)$")
_FENCE = re.compile(r"^\s*(```|~~~)(.*)$")
_TABLE_DELIM = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")

# The title is matched against an already-emitted <p>; the image and caption against the inline body
# of the paragraph being built, which is not wrapped yet. Hence the two shapes.
_FIGURE_TITLE = re.compile(r"^<p><strong>(.+?)</strong></p>$", re.S)
_FIGURE_CAPTION = re.compile(r"^<em>(.+?)</em>$", re.S)
_LONE_IMAGE = re.compile(r"^<img\s[^>]*>$")


def _slug(text: str) -> str:
    plain = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-") or "section"


def _split_row(row: str) -> list[str]:
    row = row.strip()
    row = row[1:] if row.startswith("|") else row
    row = row[:-1] if row.endswith("|") else row
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", row)]


def _table(lines: list[str], start: int, resolve_image) -> tuple[str, int]:
    header = _split_row(lines[start])
    aligns = []
    for spec in _split_row(lines[start + 1]):
        left, right = spec.startswith(":"), spec.endswith(":")
        aligns.append("center" if left and right else "right" if right else "left")

    index = start + 2
    body: list[list[str]] = []
    while index < len(lines) and lines[index].strip().startswith("|"):
        body.append(_split_row(lines[index]))
        index += 1

    def cells(row: list[str], tag: str) -> str:
        out = []
        for position, cell in enumerate(row):
            align = aligns[position] if position < len(aligns) else "left"
            style = f' style="text-align:{align}"' if align != "left" else ""
            out.append(f"<{tag}{style}>{_render_inline(cell, resolve_image)}</{tag}>")
        return "".join(out)

    rows = "\n".join(f"<tr>{cells(row, 'td')}</tr>" for row in body)
    return (f'<div class="table-wrap"><table>\n<thead><tr>{cells(header, "th")}</tr></thead>\n'
            f"<tbody>\n{rows}\n</tbody>\n</table></div>"), index


def _list(lines: list[str], start: int, resolve_image, headings: list) -> tuple[str, int]:
    first = _LIST.match(lines[start])
    base_indent = len(first.group(1))
    ordered = first.group(3) is not None
    items: list[list[str]] = []
    index = start
    content_indent = base_indent + 2

    while index < len(lines):
        line = lines[index]
        match = _LIST.match(line)
        if match and len(match.group(1)) <= base_indent:
            if (match.group(3) is not None) != ordered:
                break                                   # a different kind of list starts here
            content_indent = len(match.group(0)) - len(match.group(4))
            items.append([match.group(4)])
            index += 1
            continue
        if not line.strip():
            # A blank line stays inside the list only if the list actually continues after it.
            look = index + 1
            while look < len(lines) and not lines[look].strip():
                look += 1
            if look >= len(lines) or not items:
                break
            following = _LIST.match(lines[look])
            still_list = (following is not None and len(following.group(1)) <= base_indent
                          and (following.group(3) is not None) == ordered)
            indented = len(lines[look]) - len(lines[look].lstrip()) >= content_indent
            if not (still_list or indented):
                break
            items[-1].append("")
            index += 1
            continue
        if items and len(line) - len(line.lstrip()) >= content_indent:
            items[-1].append(line[content_indent:])     # nested block, dedented one level
            index += 1
            continue
        if items:
            items[-1].append(line.strip())              # lazy continuation of the item's paragraph
            index += 1
            continue
        break

    rendered = []
    for item in items:
        inner = _render_blocks(item, resolve_image, headings)
        single = re.fullmatch(r"<p>(.*)</p>", inner.strip(), flags=re.S)
        rendered.append(f"<li>{single.group(1) if single else inner}</li>")

    if not ordered:
        return "<ul>\n" + "\n".join(rendered) + "\n</ul>", index

    # The numerals are drawn by a CSS counter, so the counter starts where the markdown does.
    # Renumbering the findings would change the content.
    start = int(first.group(3))
    return (f'<ol start="{start}" style="counter-reset:item {start - 1}">\n'
            + "\n".join(rendered) + "\n</ol>"), index


def _render_blocks(lines: list[str], resolve_image, headings: list) -> str:
    out: list[str] = []
    pending_figure: int | None = None
    index = 0

    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue

        fence = _FENCE.match(line)
        if fence:
            index += 1
            body = []
            while index < len(lines) and not lines[index].strip().startswith(fence.group(1)):
                body.append(lines[index])
                index += 1
            index += 1
            language = fence.group(2).strip()
            css = f' class="language-{html.escape(language, quote=True)}"' if language else ""
            out.append(f"<pre><code{css}>{html.escape(chr(10).join(body))}</code></pre>")
            pending_figure = None
            continue

        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            text = _render_inline(heading.group(2), resolve_image)
            anchor = _slug(heading.group(2))
            if level == 2:
                headings.append((anchor, re.sub(r"<[^>]+>", "", text)))
            out.append(f'<h{level} id="{anchor}">{text}</h{level}>')
            index += 1
            pending_figure = None
            continue

        if _RULE.match(line):
            out.append("<hr>")
            index += 1
            pending_figure = None
            continue

        if (line.strip().startswith("|") and index + 1 < len(lines)
                and "|" in lines[index + 1] and _TABLE_DELIM.match(lines[index + 1])):
            chunk, index = _table(lines, index, resolve_image)
            out.append(chunk)
            pending_figure = None
            continue

        if line.lstrip().startswith(">"):
            quoted = []
            while index < len(lines) and lines[index].lstrip().startswith(">"):
                quoted.append(re.sub(r"^\s*>\s?", "", lines[index]))
                index += 1
            out.append("<blockquote>\n"
                       + _render_blocks(quoted, resolve_image, headings) + "\n</blockquote>")
            pending_figure = None
            continue

        if _LIST.match(line):
            chunk, index = _list(lines, index, resolve_image, headings)
            out.append(chunk)
            pending_figure = None
            continue

        paragraph = []
        while index < len(lines) and lines[index].strip():
            current = lines[index]
            if paragraph and (_HEADING.match(current) or _RULE.match(current)
                              or _LIST.match(current) or _FENCE.match(current)
                              or current.strip().startswith("|")):
                break
            paragraph.append(current)
            index += 1
        body = _render_inline("\n".join(paragraph), resolve_image).strip()

        # A chart in these reports is written as a bold title, then the image, then an italic
        # caption. Grouped into one <figure> rather than three loose paragraphs.
        if _LONE_IMAGE.match(body):
            title = ""
            if out and _FIGURE_TITLE.match(out[-1].strip()):
                captured = _FIGURE_TITLE.match(out.pop().strip()).group(1)
                title = f'<figcaption class="fig-title">{captured}</figcaption>'
            out.append(f"<figure>{title}{body}</figure>")
            pending_figure = len(out) - 1
            continue
        if pending_figure is not None and _FIGURE_CAPTION.match(body):
            caption = _FIGURE_CAPTION.match(body).group(1)
            out[pending_figure] = out[pending_figure].replace(
                "</figure>", f'<figcaption class="fig-note">{caption}</figcaption></figure>')
            pending_figure = None
            continue

        out.append(f"<p>{body}</p>")
        pending_figure = None

    return "\n".join(out)


def render_markdown(markdown: str, resolve_image=None) -> tuple[str, list[tuple[str, str]]]:
    """Markdown -> (html body, its h2 headings in order)."""
    headings: list[tuple[str, str]] = []
    body = _render_blocks(markdown.replace("\r\n", "\n").expandtabs(4).split("\n"),
                          resolve_image, headings)
    return body, headings


# --- the page ---------------------------------------------------------------------------------------

STYLESHEET = """
:root {
  --surface:#fcfcfb; --panel:#ffffff; --ink:#111110; --muted:#6b6963;
  --rule:#e4e3dc; --rule-soft:#efeee8; --accent:#2a78d6; --accent-soft:#eef4fd;
  --code-bg:#f4f3ef; --shadow:0 1px 2px rgba(16,16,14,.05), 0 8px 24px -12px rgba(16,16,14,.14);
  --sans:ui-sans-serif,-apple-system,"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif;
  --serif:"Iowan Old Style","Charter","Palatino Linotype",Palatino,Georgia,serif;
  --mono:ui-monospace,"SF Mono","Cascadia Mono","JetBrains Mono",Menlo,Consolas,monospace;
}
@media (prefers-color-scheme: dark) {
  :root {
    --surface:#131312; --panel:#1b1b19; --ink:#eceae4; --muted:#a3a099;
    --rule:#2e2e2b; --rule-soft:#252523; --accent:#7bb0ef; --accent-soft:#1c2733;
    --code-bg:#232320; --shadow:0 1px 2px rgba(0,0,0,.4), 0 8px 24px -12px rgba(0,0,0,.6);
  }
  img { filter: brightness(.92) contrast(1.02); }
}
* { box-sizing:border-box; }
body {
  margin:0; background:var(--surface); color:var(--ink); font-family:var(--sans);
  font-size:16px; line-height:1.65; -webkit-font-smoothing:antialiased;
  text-rendering:optimizeLegibility;
}
.page { max-width:52rem; margin:0 auto; padding:3.5rem 1.5rem 6rem; }

/* --- masthead --- */
.masthead { border-bottom:1px solid var(--rule); padding-bottom:2rem; margin-bottom:2.5rem; }
.eyebrow {
  display:flex; align-items:center; gap:.6rem; font-size:.7rem; font-weight:600;
  letter-spacing:.16em; text-transform:uppercase; color:var(--muted); margin-bottom:1.4rem;
}
.eyebrow .mark {
  width:.55rem; height:.55rem; border-radius:2px; background:var(--accent); flex:none;
}
.eyebrow .tagline::before { content:"/"; margin-right:.6rem; opacity:.45; }
h1 {
  font-family:var(--serif); font-weight:600; font-size:clamp(1.85rem,4.2vw,2.5rem);
  line-height:1.2; letter-spacing:-.01em; margin:0 0 .9rem;
}
.subtitle { color:var(--muted); font-size:.94rem; margin:0; max-width:44rem; }
.subtitle em { font-style:normal; }

/* --- verdict badge --- */
.verdict { display:flex; flex-wrap:wrap; align-items:center; gap:.75rem; margin:1.5rem 0 0; }
.badge {
  display:inline-flex; align-items:center; gap:.5rem; padding:.4rem .9rem .4rem .75rem;
  border-radius:999px; border:1px solid; font-size:.74rem; font-weight:700;
  letter-spacing:.11em; text-transform:uppercase;
}
.badge .dot { width:.5rem; height:.5rem; border-radius:999px; background:currentColor; flex:none; }
.verdict .context { font-size:.82rem; color:var(--muted); }
.verdict .context strong { color:var(--ink); font-weight:600; }

/* --- section nav --- */
.toc { display:flex; flex-wrap:wrap; gap:.4rem; margin:2.5rem 0 3rem; }
.toc a {
  font-size:.78rem; color:var(--muted); text-decoration:none; padding:.3rem .7rem;
  border:1px solid var(--rule); border-radius:999px; transition:.15s;
}
.toc a:hover { color:var(--accent); border-color:var(--accent); background:var(--accent-soft); }

/* --- prose --- */
h2 {
  font-family:var(--serif); font-size:1.35rem; font-weight:600; letter-spacing:-.005em;
  margin:3.25rem 0 1.1rem; padding-top:1.5rem; border-top:1px solid var(--rule-soft);
  scroll-margin-top:1.5rem;
}
/* The markdown puts a `---` before every section heading, and the heading draws its own rule.
   Left alone that is two lines a few pixels apart; the <hr> wins and the heading stands down. */
hr + h2 { border-top:0; padding-top:0; margin-top:0; }
h3 { font-size:1.02rem; font-weight:650; margin:2rem 0 .6rem; }
p { margin:0 0 1.05rem; }
a { color:var(--accent); text-underline-offset:2px; }
strong { font-weight:650; }
hr { border:0; height:1px; background:var(--rule); margin:2.5rem 0; }
code {
  font-family:var(--mono); font-size:.845em; background:var(--code-bg);
  padding:.12em .38em; border-radius:4px; overflow-wrap:anywhere;
}
pre { background:var(--code-bg); padding:1rem; border-radius:8px; overflow-x:auto; }
pre code { background:none; padding:0; }
blockquote {
  margin:1.4rem 0; padding:.2rem 0 .2rem 1.15rem; border-left:3px solid var(--accent);
  color:var(--muted);
}
blockquote p:last-child { margin-bottom:0; }

/* --- lists: numbered findings get a real numeral, not a default marker --- */
ul { padding-left:1.15rem; margin:0 0 1.1rem; }
ul li { margin-bottom:.5rem; }
ul li::marker { color:var(--muted); }
ol { counter-reset:item; list-style:none; padding-left:0; margin:0 0 1.1rem; }
ol > li {
  counter-increment:item; position:relative; padding-left:2.5rem; margin-bottom:1.4rem;
}
ol > li::before {
  content:counter(item); position:absolute; left:0; top:.05rem;
  width:1.65rem; height:1.65rem; border-radius:50%; background:var(--accent-soft);
  color:var(--accent); font-size:.78rem; font-weight:700; font-variant-numeric:tabular-nums;
  display:flex; align-items:center; justify-content:center;
}
ol ul, ol ol, ul ul { margin-top:.55rem; }
ol > li ul li { color:var(--muted); }
ol > li ul li strong, ol > li ul li code { color:var(--ink); }

/* --- tables --- */
.table-wrap {
  overflow-x:auto; margin:1.4rem 0 1.6rem; border:1px solid var(--rule); border-radius:10px;
  background:var(--panel);
}
table { border-collapse:collapse; width:100%; font-size:.83rem; }
th {
  text-align:left; font-weight:600; font-size:.7rem; letter-spacing:.09em; text-transform:uppercase;
  color:var(--muted); padding:.7rem .9rem; border-bottom:1px solid var(--rule);
  white-space:nowrap; background:var(--panel);
}
td { padding:.62rem .9rem; border-bottom:1px solid var(--rule-soft); vertical-align:top; }
tbody tr:last-child td { border-bottom:0; }
tbody tr:hover { background:var(--accent-soft); }
td:first-child { color:var(--muted); font-variant-numeric:tabular-nums; white-space:nowrap; }
td code { background:none; padding:0; color:var(--ink); }

/* --- charts --- */
figure {
  margin:2rem 0; padding:1.15rem; border:1px solid var(--rule); border-radius:12px;
  background:var(--panel); box-shadow:var(--shadow);
}
figure img { display:block; width:100%; height:auto; border-radius:6px; }
.fig-title {
  font-size:.72rem; font-weight:600; letter-spacing:.09em; text-transform:uppercase;
  color:var(--muted); margin-bottom:.85rem;
}
.fig-note {
  margin-top:.85rem; font-size:.82rem; color:var(--muted); line-height:1.5;
  padding-top:.75rem; border-top:1px solid var(--rule-soft);
}

/* --- colophon --- */
.colophon {
  margin-top:4rem; padding-top:1.25rem; border-top:1px solid var(--rule);
  font-size:.76rem; color:var(--muted); line-height:1.6;
}
.colophon code { font-size:.95em; }

@media print {
  :root { --surface:#fff; --panel:#fff; --ink:#000; --muted:#444; --shadow:none; }
  body { font-size:10.5pt; }
  .page { max-width:none; padding:0; }
  .toc, .colophon .hint { display:none; }
  a { color:inherit; text-decoration:none; }
  h2 { break-after:avoid; }
  figure, tr, li { break-inside:avoid; }
  .table-wrap { overflow:visible; }
}
@page { margin:16mm 14mm; }
"""


def _data_uri(path: Path) -> str:
    """Encode a file as a data: URI, so the page is self-contained."""
    kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"data:{kind};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def _image_resolver(source_dir: Path, embed: bool, target_dir: Path | None = None):
    def resolve(src: str) -> str:
        if re.match(r"^[a-z]+:", src):                  # already a URL or a data: URI
            return src
        candidate = (source_dir / src).resolve()
        if not candidate.is_file():
            return src
        if embed:
            return _data_uri(candidate)
        # Linked rather than embedded: the path must be relative to where the page lands, which is
        # not where the markdown lives once --out-dir is in play.
        if target_dir is None:
            return src
        try:
            return Path(os.path.relpath(candidate, target_dir.resolve())).as_posix()
        except ValueError:
            return candidate.as_uri()       # Windows: no relative path exists across drives
    return resolve


def _verdict_block(verdict: dict | str | None) -> str:
    if not verdict:
        return ""
    if isinstance(verdict, str):
        verdict = {"verdict": verdict}
    label = str(verdict.get("verdict", "")).strip().upper()
    if label not in VERDICT_STYLES:
        raise ValueError(f"verdict must be one of {sorted(VERDICT_STYLES)}, got {label!r}")
    ink, tint, edge = VERDICT_STYLES[label]

    context = []
    if verdict.get("round"):
        context.append(f"<strong>{html.escape(str(verdict['round']))}</strong>")
    if verdict.get("note"):
        context.append(html.escape(str(verdict["note"])))
    trailer = f'<span class="context">{" &middot; ".join(context)}</span>' if context else ""

    return (f'<div class="verdict">'
            f'<span class="badge" style="color:{ink};background:{tint};border-color:{edge}">'
            f'<span class="dot"></span>{label}</span>{trailer}</div>')


def build_page(markdown: str, source: Path, verdict=None, embed_images: bool = True,
               target_dir: Path | None = None) -> str:
    """Wrap the rendered markdown in the page shell.

    The H1 and the italic stat line beneath it are moved verbatim into the masthead.
    """
    lines = markdown.replace("\r\n", "\n").split("\n")

    title, cursor = source.stem.replace("_", " "), 0
    while cursor < len(lines) and not lines[cursor].strip():
        cursor += 1
    if cursor < len(lines) and lines[cursor].startswith("# "):
        title = lines[cursor][2:].strip()
        cursor += 1
        while cursor < len(lines) and not lines[cursor].strip():
            cursor += 1

    subtitle_md = ""
    if cursor < len(lines) and re.fullmatch(r"\*[^*].*\*", lines[cursor].strip()):
        subtitle_md = lines[cursor].strip()
        cursor += 1
    while cursor < len(lines) and (not lines[cursor].strip() or _RULE.match(lines[cursor])):
        cursor += 1

    resolve = _image_resolver(source.parent, embed_images, target_dir)
    body, headings = render_markdown("\n".join(lines[cursor:]), resolve)

    subtitle = (f'<p class="subtitle">{_render_inline(subtitle_md)}</p>' if subtitle_md else "")
    nav = "".join(f'<a href="#{anchor}">{html.escape(text)}</a>' for anchor, text in headings)
    nav = f'<nav class="toc">{nav}</nav>' if nav else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<meta name="generator" content="RootStrata format_output.py (presentation layer)">
<style>{STYLESHEET}</style>
</head>
<body>
<main class="page">
<header class="masthead">
  <div class="eyebrow">
    <span class="mark"></span><span>{html.escape(PROJECT_NAME)}</span>
    <span class="tagline">{html.escape(PROJECT_TAGLINE)}</span>
  </div>
  <h1>{_render_inline(title)}</h1>
  {subtitle}
  {_verdict_block(verdict)}
</header>
{nav}
{body}
<footer class="colophon">
  Rendered from <code>{html.escape(source.name)}</code> by <code>format_output.py</code> — a
  presentation layer only. The findings, evidence table and charts are reproduced verbatim from the
  markdown written by <code>generate_report.py</code>; nothing here was regenerated or reworded.
  <span class="hint"><br>Print this page (Ctrl/Cmd&nbsp;+&nbsp;P) to get the PDF.</span>
</footer>
</main>
</body>
</html>
"""


# --- the guarantee ---------------------------------------------------------------------------------

def _strip_tags(page: str) -> str:
    """Return the page's text as a reader sees it, plus alt and title attribute values."""
    page = re.sub(r"<(style|script)\b.*?</\1>", " ", page, flags=re.S | re.I)
    attributes = " ".join(m.group(2) for m in re.finditer(r'\b(alt|title)="([^"]*)"', page))
    return html.unescape(re.sub(r"<[^>]+>", " ", page) + " " + attributes)


def missing_words(markdown: str, page: str) -> list[str]:
    """Return the words of the source markdown that are missing from the rendered page.

    Should always be empty; this is what makes "formatting only" testable.
    """
    source = _DESTINATION.sub("]()", markdown)          # hrefs/srcs move into attributes by design
    source = _LIST_MARKER.sub("", source)               # "1." is drawn by a CSS counter, not text
    rendered = set(_WORD.findall(_strip_tags(page)))
    return sorted({word for word in _WORD.findall(source) if word not in rendered})


# --- driving it ------------------------------------------------------------------------------------

def convert(source: Path, out_path: Path, verdict=None, embed_images: bool = True) -> list[str]:
    markdown = source.read_text(encoding="utf-8")
    page = build_page(markdown, source, verdict, embed_images, out_path.parent)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")
    return missing_words(markdown, page)


def collect(paths: list[Path]) -> list[Path]:
    found: list[Path] = []
    for path in paths:
        if path.is_dir():
            found.extend(sorted(path.rglob("*_report.md")))
        elif path.is_file():
            found.append(path)
        else:
            raise SystemExit(f"no such file or directory: {path}")
    return found


def _verdict_for(source: Path, table: dict, override: str | None):
    """Look up a report's verdict. Keys may be a stem, a filename, or a path suffix.

    The suffix form distinguishes two graded runs of the same dataset, which share a stem.
    """
    if override:
        return override
    posix = source.as_posix()
    for key, value in table.items():
        if "/" in key and posix.endswith(key.lstrip("./")):
            return value
    return (table.get(source.name) or table.get(source.stem)
            or table.get(source.stem.replace("_report", "")))


def _find_verdicts(roots: list[Path], sources: list[Path]) -> Path | None:
    """Find verdicts.json, checking the directories passed on the command line before each
    report's own folder."""
    for folder in [r if r.is_dir() else r.parent for r in roots] + [s.parent for s in sources]:
        candidate = folder / "verdicts.json"
        if candidate.is_file():
            return candidate
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render generated markdown reports as self-contained, styled HTML. "
                    "Reformats only - it never regenerates, rewords or reshapes a finding.")
    parser.add_argument("paths", nargs="*", type=Path, default=[Path("reports/generated")],
                        help="report .md files, or directories to search for *_report.md "
                             "(default: reports/generated)")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="where to write the .html (default: next to each source .md)")
    parser.add_argument("--verdict", default=None, choices=sorted(VERDICT_STYLES),
                        help="badge for the report; only valid when converting a single file")
    parser.add_argument("--verdicts", type=Path, default=None,
                        help="JSON map of report stem -> verdict, e.g. "
                             '{"marketing_weekly_report": {"verdict": "PARTIAL", "round": "Day 5"}}'
                             " (default: verdicts.json beside the reports, if present)")
    parser.add_argument("--no-embed", action="store_true",
                        help="link charts instead of inlining them as base64")
    parser.add_argument("--strict", action="store_true",
                        help="exit non-zero if any source word is missing from the rendered page")
    args = parser.parse_args(argv)

    sources = collect([Path(p) for p in args.paths])
    if not sources:
        print("no *_report.md files found", file=sys.stderr)
        return 1
    if args.verdict and len(sources) > 1:
        print("--verdict applies to one report; use --verdicts for a batch", file=sys.stderr)
        return 2

    table: dict = {}
    verdicts_path = args.verdicts or _find_verdicts([Path(p) for p in args.paths], sources)
    if verdicts_path and verdicts_path.is_file():
        table = json.loads(verdicts_path.read_text(encoding="utf-8"))
        print(f"verdicts from {verdicts_path}")

    failures = 0
    for source in sources:
        target = ((args.out_dir / f"{source.stem}.html") if args.out_dir
                  else source.with_suffix(".html"))
        dropped = convert(source, target, _verdict_for(source, table, args.verdict),
                          embed_images=not args.no_embed)
        size = target.stat().st_size / 1024
        print(f"wrote {target}  ({size:.0f} KB)")
        if dropped:
            failures += 1
            print(f"      CONTENT CHECK FAILED - {len(dropped)} word(s) missing from the page: "
                  f"{', '.join(dropped[:8])}", file=sys.stderr)
        else:
            print("      content check: every word of the markdown is present in the page")

    return 1 if (failures and args.strict) else 0


if __name__ == "__main__":
    raise SystemExit(main())
