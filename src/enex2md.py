#!/usr/bin/env python3
"""Convert Evernote export files (.enex) into a folder of Markdown notes.

Every note becomes one Markdown file named after its title, with YAML front
matter (title, created, updated, tags, source, author, location), and the
file's creation and modification times set to the note's (on macOS; other
systems keep only the modification time). Attachments are saved
once each, in an attachments/ folder beside the notes, and linked with relative
paths: pictures and PDFs are shown in the note, as Obsidian can, and other
files are a box with their link, as Evernote shows them (an Obsidian
callout); links from one note to another become relative links to its file. The
output is plain Markdown, so it works as-is in Obsidian, in a Git repository or
in any Markdown editor.

Usage:
  enex2md.py NOTEBOOK.enex [MORE.enex | FOLDER ...] [-o OUTPUT_FOLDER]

By default each .enex file's notes go to a new folder named after the file,
next to it (Evernote exports one notebook per file). With -o they go to that
folder instead, one subfolder per file when there are several; --flat puts
every note together.

How the conversion works, and why:
- Notes are read one at a time, so an export of several gigabytes doesn't have
  to fit in memory.
- A note's body is ENML, Evernote's XHTML dialect. It is parsed leniently with
  html.parser, since web clips are often not well-formed XML and use HTML
  entities, such as &nbsp;, that an XML parser rejects.
- Evernote writes one <div> per line and an empty <div><br/></div> per blank
  line, so a <div> becomes a line break, not a paragraph; otherwise every note
  would come out double-spaced. A line break is a bare newline, which
  Obsidian shows as typed; most other previews (VS Code, GitHub) join such
  lines, and would need HARD_BREAK set to Markdown's backslash break.
- A blank line typed in Evernote becomes a line holding only &nbsp;: a bare
  blank line in Markdown only separates paragraphs, which a preview shows as
  a gap smaller than a line, or not at all next to a list or a divider.
- Links between notes carry Evernote IDs that the export doesn't map to notes,
  so they are matched by their text to a note's title once every note is
  written; a link that matches no note keeps its evernote:// address.
- Text colour carries meaning (such as red for urgent), and Markdown has no
  syntax for it, so coloured text becomes an inline <span style="color: ...">,
  which Obsidian shows in colour. Black and grey are the default text colours,
  so they are dropped. Evernote highlights become ==highlights==, and
  underlines, which Markdown has no syntax for either, <u>underlines</u>.
- Indented lines lose their indent, since Markdown can't indent a line of
  text; a blank line after them marks where they end instead. Older Evernote
  indented with a <blockquote> drawn without a bar: it's an indent, not a quote.
- Code pasted from an editor isn't an Evernote code block but lines in a
  monospaced font, with every token coloured; a run of lines whose text is all
  monospaced becomes one fenced code block, keeping its indentation and losing
  the syntax colours, and a monospaced word in a sentence becomes `code`.
  Old notes indented such code with Evernote's indent, which becomes spaces.
- A code block keeps the language Evernote recorded; with none, the language
  is guessed from patterns in the code, the languages the note's title names
  and the language of the note's other code blocks, and left unset when unsure.
- Code is kept as typed: nothing is reformatted.
- A picture always has exactly one blank line under it, however many were
  typed, so pictures are spaced evenly. A divider gets only the bare blank
  lines Markdown needs around it, whatever was typed: Obsidian already draws
  it with a blank line's margin above and below, next to a picture too.
- Tags are made valid Obsidian tags: no spaces or punctuation but - _ and /,
  so "Work - Ideas" becomes "Work-Ideas".
- Only the Python standard library is used, so there is nothing to install.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import collections
import hashlib
import json
import mimetypes
import os
import re
import sys
import textwrap
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import quote, unquote_to_bytes

ATTACHMENTS_DIR = "attachments"
MAX_NAME_LENGTH = 100

# Placeholders a rendered block holds until its content is complete; resolve()
# turns each run of them into line breaks, paragraph breaks and blank lines:
# LINE ends a line (a <div>), PARA a block (a list, a divider), BREAK is a <br>;
# INDENT_START and INDENT_END enclose indented lines, which a blank line ends.
LINE, PARA, BREAK, INDENT_START, INDENT_END = "\x01", "\x02", "\x03", "\x04", "\x05"
MARKERS = (LINE, PARA, BREAK, INDENT_START, INDENT_END)
MARKER_RUN = re.compile(r"[ \t]*[\x01-\x05](?:[ \t]*[\x01-\x05])*[ \t]*")
CODE_MARKER_RUN = re.compile(r"[\x01-\x05]+")
INDENT_STYLE = re.compile(r"(?:padding|margin)-left:[1-9]")
CODE_INDENT = "    "
# Stands for a code block's language until every block of the note is known
LANGUAGE_SLOT = re.compile(r"\x07(\d+)\x07")
# Stands for a link to another note until every note's file is known.
NOTE_LINK = re.compile(r"\x06(\d+)\x06")
# A line that starts a block of its own; a hard line break before it, or at
# the end of a block, would show as a literal backslash.
BLOCK_START = re.compile(r"(?:[-*+] |\d+[.)] |#|>|\||```|~~~|---)")
LIST_ITEM = re.compile(r"[ \t]*(?:[-*+]|\d+[.)]) ")
IMAGE_LINE_END = re.compile(r"!\[[^\]]*\]\([^()\s]*\)(?:</span>)*$")
ADJACENT_IMAGES = re.compile(r"(!\[[^\]]*\]\([^()\s]*\))[ \t]*(?=!\[)")
DIVIDER = "---"
DIVIDER_START = re.compile(r"---(?:[\x01-\x05]|$)")
HARD_BREAK = "\n"
BLANK_LINE = "&nbsp;"
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

VOID_TAGS = {"br", "hr", "img", "en-media", "en-todo", "input", "meta", "link",
             "col", "area", "wbr", "source"}
SKIPPED_TAGS = {"head", "title", "style", "script", "noscript", "svg", "iframe",
                "object", "button", "select", "input", "meta", "link"}
BLOCK_TAGS = {"div", "en-note", "html", "body", "section", "article", "header",
              "footer", "main", "nav", "aside", "center", "address", "figure",
              "figcaption", "dl", "dt", "dd", "form", "fieldset", "details",
              "summary", "li", "tr", "td", "th", "thead", "tbody", "tfoot",
              "caption"}
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

UNSAFE_NAME = re.compile(r'[\\/:*?"<>|#^\[\]\x00-\x1f]')
MARKDOWN_SPECIAL = re.compile(r"([\\`*\[\]<]|(?<!\w)_|_(?!\w))")
MONOSPACE_FONT = re.compile(r"consolas|courier|monaco|menlo|monospace|lucida console|"
                            r"source code|fira code|sf mono|andale mono")
CODE_TAGS = {"code", "kbd", "samp", "tt"}
TOUCHING_CODE = re.compile(r"(?<!`)`[^`\n]+`(?:`[^`\n]+`)+(?!`)")
# Evernote's names for code block languages that Obsidian names differently
EVERNOTE_LANGUAGES = {"cs": "csharp",
                      "js": "javascript", "ts": "typescript", "sh": "bash"}
# Patterns that point to one language, each worth a point, or two when given
# as (pattern, 2) because only that language has it; guess_language names one
# only with two points
STRONG = 2
SHELL_COMMAND = (r"^\s*(\$\s+)?([A-Z_][A-Z0-9_]*=\S*\s+)*"  # an environment variable may come first
                 r"(sudo|npm|npx|yarn|pnpm|dotnet|git|docker|kubectl|brew|pip3?|curl|"
                 r"cd|ls|mkdir|export|chmod|apt-get|az|aws|claude|codex|code|node|python3?)\s")
LANGUAGE_PATTERNS = {language: [(re.compile(entry[0], re.M), entry[1]) if isinstance(entry, tuple)
                                else (re.compile(entry, re.M), 1) for entry in patterns]
                     for language, patterns in {
    "csharp": [(r"\b(public|internal)\s+(sealed\s+|abstract\s+|static\s+|partial\s+)*"
                r"(class|interface|record|enum|struct)\s+\w+", STRONG),
               r"\b(private|protected)\s+readonly\s+\w+",
               r"\b(public|private|protected|internal)\s+(static\s+|async\s+|override\s+|virtual\s+|readonly\s+|sealed\s+|abstract\s+)*"
               r"(class|interface|void|Task|int|string|bool|[A-Z]\w*(<[\w<>, ]+>)?)\s+\w+",
               r"^\s*using\s+[A-Z][\w.]*;", r"^\s*namespace\s+[A-Z][\w.]*",
               r"\{\s*get;\s*(private\s+)?set;\s*\}", r"^\s*\[[A-Z]\w*(\(.*\))?\]\s*$",
               r"\bnew\s+[A-Z]\w*(<[\w<>, ]+>)?\s*\(", r"\b(string|int|bool|double|decimal|long)\s+\w+\s*=",
               r"\bawait\s+\w+.*Async\(", r"\bConsole\.WriteLine\b"],
    "javascript": [r"\bfunction\s*\w*\s*\([^)]*\)\s*\{", r"\b(const|let)\s+\w+\s*=",
                   r"\b(var|let|const)\s+\w+\s*=\s*function\b|\bthis\.\w+\s*=\s*function\b|\.prototype\b",
                   r"\bconsole\.log\(", r"\$scope\b|\bangular\.module\b|\$\(['\"]",
                   r"\bdocument\.|\bwindow\.", r"\brequire\(['\"]", r"===|!==",
                   r"\bmodule\.exports\b|\bexport\s+(default\s+)?(function|const|class)\b"],
    "typescript": [r"\binterface\s+\w+\s*\{", r"\w+\s*:\s*(string|number|boolean|any|void)\b",
                   r"^\s*import\s+.*\s+from\s+['\"]", r"@(Component|Injectable|NgModule)\("],
    "css": [r"^\s*[.#]?[\w-]+([\s>+~.#:][\w-]+)*\s*\{\s*$", r"^\s*[\w-]+\s*:\s*[^;{}]+;\s*$",
            r"@media\b|!important\b", r"\b\d+(px|em|rem|%)\b"],
    "sql": [r"\bSELECT\b[\s\S]+\b(?i:from)\b", r"\b(INSERT\s+INTO|DELETE\s+FROM|CREATE\s+TABLE|ALTER\s+TABLE)\b",
            r"\bUPDATE\s+\w+\s+SET\b", r"\b(?i:where|group\s+by|order\s+by|inner\s+join|left\s+join|facet)\b"],
    "bash": [r"^\s*\$\s+\w", SHELL_COMMAND, (r"^#!.*\b(ba|z)?sh\b", STRONG)],
    "python": [r"^\s*def\s+\w+\(.*\):\s*$", r"^\s*(from\s+\w+\s+)?import\s+\w+\s*$",
               r"\bself\.\w+", r"^\s*if\s+__name__\s*==", r"\bprint\("],
    "xml": [], "html": [], "json": [], "yaml": [], "powershell": [r"^\s*\$\w+\s*=", r"\b(Get|Set|New|Remove|Invoke)-[A-Z]\w+"],
}.items()}
YAML_LINE = re.compile(r"\s*(-\s+)?[\w.\-\"']+:(\s+\S.*)?$|\s*-\s+\S")
# Words in a note's title that name a language; each is a hint, worth a point
TITLE_LANGUAGES = {language: re.compile(pattern, re.I) for language, pattern in {
    "csharp": r"C#|\.net\b|\bnet core\b|asp\.?net|\blinq\b|entity framework|\bwpf\b|\bwcf\b|"
              r"\bcastle\b|\bwindsor\b|\bmvc\b|\bxunit\b|\bnunit\b|\bmoq\b",
    "javascript": r"javascript|\bjs\b|jquery|angular|\bnode\b|react|\bes6\b",
    "typescript": r"typescript",
    "css": r"\bcss3?\b|\bless\b|\bsass\b|\bscss\b",
    "html": r"\bhtml5?\b",
    "xml": r"\bxslt?\b|\bxml\b|\bxsd\b|\bxpath\b",
    "sql": r"\bsql\b|\bnrql\b",
    "bash": r"\bbash\b|\bshell\b|\bnpm\b|\bnpx\b|\bdocker\b|\bterminal\b",
    "python": r"\bpython\b",
    "powershell": r"powershell",
}.items()}
URL_SPECIAL = re.compile(r"[\s()<>]")
PATH_SPECIAL = re.compile(r"[\s()<>%]")
TAG_SPECIAL = re.compile(r"[^\w/]*[^\w/-][^\w/]*|-{2,}")
COLOR_STYLE = re.compile(r"(?<![-\w])color:([^;]+)")
SAFE_COLOR = re.compile(r"[#\w(),.%]+")
RGB_COLOR = re.compile(r"rgba?\((\d+),(\d+),(\d+)(?:,[\d.]+)?\)")
NEUTRAL_COLORS = {"black", "white", "gray", "grey", "inherit", "initial", "unset",
                  "currentcolor", "transparent"}
SEGMENTS = re.compile(r"([\x01-\x05]+)")
LIST_PREFIX = re.compile(r"(?:[-*+]|\d+\.) (?:\[[ x]\] )?")


@dataclass
class Resource:
    """A file attached to a note, as the export holds it."""
    data: bytes
    mime: str
    file_name: str
    hash: str


@dataclass
class Attachment:
    """An attachment saved to disk: its path relative to the notes, and type."""
    path: str
    mime: str
    name: str


@dataclass
class Note:
    """One note of the export, with its ENML body still unconverted."""
    title: str
    content: str
    created: datetime | None
    updated: datetime | None
    tags: list[str]
    source: str | None
    author: str | None
    location: str | None
    resources: list[Resource] = field(default_factory=list)


class Exporter:
    """Converts .enex files into Markdown notes under one output folder."""

    def __init__(self):
        self.folders: dict[str, NoteFolder] = {}
        self.notes_by_title: dict[str, str] = {}
        self.note_links: list[tuple[str, str, str]] = []
        self.files_with_links: set[str] = set()
        self.file_times: dict[str, list[float]] = {}
        self.notes = 0
        self.linked = 0
        self.warnings: list[str] = []

    def export(self, plan: list[tuple[str, str]]) -> None:
        """Converts each .enex file of the plan into its output folder."""
        for path, folder_path in plan:
            folder = self.folders.setdefault(
                folder_path, NoteFolder(folder_path))
            self.export_file(path, folder)
        self.link_notes()
        for path, stamps in self.file_times.items():
            # macOS moves a file's creation time back to a modification time set
            # before it, so setting the note's created time first makes it the
            # file's creation time too; apps such as Obsidian sort notes by it
            for stamp in stamps:
                os.utime(path, (stamp, stamp))

    def attachment_count(self) -> int:
        return sum(len(folder.attachment_names) for folder in self.folders.values())

    def export_file(self, path: str, folder: NoteFolder) -> None:
        print(f"Reading {path}", file=sys.stderr)
        try:
            events = ET.iterparse(path, events=("start", "end"))
            _, root = next(events)
            for event, element in events:
                if event == "end" and element.tag == "note":
                    self.export_note(read_note(element), folder)
                    root.clear()
        except ET.ParseError as error:
            raise SystemExit(
                f"error: {path} is not a valid Evernote export: {error}")

    def export_note(self, note: Note, folder: NoteFolder) -> None:
        path = folder.note_path(note.title)
        self.notes_by_title.setdefault(note.title.casefold(), path)
        attachments = {res.hash: folder.save_attachment(
            res) for res in note.resources}
        renderer = NoteRenderer(attachments, folder,
                                self.add_note_link, note.title)
        try:
            body = renderer.render(note.content)
        except Exception as error:  # a converter bug must not lose the note
            self.warnings.append(
                f"{note.title}: kept as HTML, conversion failed ({error})")
            body = note.content
        if renderer.missing:
            self.warnings.append(
                f"{note.title}: {renderer.missing} attachment(s) missing from the export")
        text = front_matter(note) + "\n" + (body + "\n" if body else "")
        os.makedirs(folder.path, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as file:
            file.write(text)
        if NOTE_LINK.search(text):
            self.files_with_links.add(path)
        stamps = [stamp.timestamp()
                  for stamp in (note.created, note.updated) if stamp]
        if stamps:
            self.file_times[path] = stamps
        self.notes += 1

    def add_note_link(self, label: str, title: str, href: str) -> str:
        """Returns a placeholder for a link to another note, by its title."""
        self.note_links.append((label, title, href))
        return f"\x06{len(self.note_links) - 1}\x06"

    def link_notes(self) -> None:
        for path in self.files_with_links:
            with open(path, encoding="utf-8") as file:
                text = file.read()

            def replace(match: re.Match) -> str:
                label, title, href = self.note_links[int(match.group(1))]
                target = self.notes_by_title.get(title.casefold())
                if target is None:
                    return f"[{label}]({link_target(href)})"
                self.linked += 1
                relative = os.path.relpath(target, os.path.dirname(path))
                return f"[{label}]({local_link(relative)})"

            with open(path, "w", encoding="utf-8", newline="\n") as file:
                file.write(NOTE_LINK.sub(replace, text))


class NoteFolder:
    """One output folder: the file names of its notes and its saved attachments."""

    def __init__(self, path: str):
        self.path = path
        self.note_names: set[str] = set()
        self.attachments: dict[str, Attachment] = {}
        self.attachment_names: set[str] = set()

    def note_path(self, title: str) -> str:
        return os.path.join(self.path, unique_name(safe_name(title), ".md", self.note_names))

    def save_attachment(self, resource: Resource) -> Attachment:
        """Saves a resource once per folder, however many notes hold it."""
        if resource.hash in self.attachments:
            return self.attachments[resource.hash]
        stem, ext = os.path.splitext(resource.file_name)
        stem = safe_name(stem, default=resource.hash[:12])
        ext = re.sub(r"[^\w.]", "", ext)[:10]
        if len(ext) < 2:
            ext = mimetypes.guess_extension(resource.mime) or ".bin"
        name = unique_name(stem, ext, self.attachment_names)
        directory = os.path.join(self.path, ATTACHMENTS_DIR)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, name), "wb") as file:
            file.write(resource.data)
        attachment = Attachment(
            f"{ATTACHMENTS_DIR}/{name}", resource.mime, name)
        self.attachments[resource.hash] = attachment
        return attachment


def safe_name(text: str, default: str = "Untitled") -> str:
    """Turns a title into a file name that every OS and Obsidian accept."""
    name = re.sub(r"\s+", " ", UNSAFE_NAME.sub(" ", text)).strip().lstrip(".")
    name = name[:MAX_NAME_LENGTH].rstrip(" .")
    return name or default


def unique_name(stem: str, ext: str, taken: set[str]) -> str:
    """Adds " (2)", " (3)"... until the name is free; case-insensitive, as on macOS."""
    candidate, number = stem + ext, 2
    while candidate.casefold() in taken:
        candidate, number = f"{stem} ({number}){ext}", number + 1
    taken.add(candidate.casefold())
    return candidate


def read_note(element: ET.Element) -> Note:
    attributes = element.find("note-attributes")

    def attribute(name: str) -> str | None:
        value = attributes.findtext(name) if attributes is not None else None
        return value.strip() if value and value.strip() else None

    latitude, longitude = attribute("latitude"), attribute("longitude")
    return Note(
        title=(element.findtext("title") or "").strip() or "Untitled",
        content=element.findtext("content") or "",
        created=parse_time(element.findtext("created")),
        updated=parse_time(element.findtext("updated")),
        tags=[tag.text.strip() for tag in element.findall("tag")
              if tag.text and tag.text.strip()],
        source=attribute("source-url"),
        author=attribute("author"),
        location=f"{latitude}, {longitude}" if latitude and longitude else None,
        resources=[res for res in map(
            read_resource, element.findall("resource")) if res],
    )


def read_resource(element: ET.Element) -> Resource | None:
    try:
        data = base64.b64decode(element.findtext("data") or "")
    except (binascii.Error, ValueError):
        return None
    if not data:
        return None
    attributes = element.find("resource-attributes")
    file_name = attributes.findtext(
        "file-name") if attributes is not None else None
    mime = (element.findtext("mime") or "").strip(
    ) or "application/octet-stream"
    return Resource(data, mime, (file_name or "").strip(), hashlib.md5(data).hexdigest())


def parse_time(value: str | None) -> datetime | None:
    """Reads Evernote's UTC timestamps, such as 20240115T093000Z."""
    try:
        return datetime.strptime(value.strip(), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except (AttributeError, ValueError):
        return None


def front_matter(note: Note) -> str:
    lines = ["---", f"title: {yaml_string(note.title)}"]
    for key, stamp in (("created", note.created), ("updated", note.updated)):
        if stamp:
            # local time with no offset, the date-time format Obsidian recognizes
            lines.append(
                f"{key}: {stamp.astimezone().strftime('%Y-%m-%dT%H:%M:%S')}")
    tags = [tag for tag in map(obsidian_tag, note.tags) if tag]
    if tags:
        lines.append("tags:")
        lines += [f"  - {yaml_string(tag)}" for tag in dict.fromkeys(tags)]
    for key, value in (("source", note.source), ("author", note.author),
                       ("location", note.location)):
        if value:
            lines.append(f"{key}: {yaml_string(value)}")
    lines.append("---")
    return "\n".join(lines)


def yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)  # a JSON string is valid YAML


def text_color(element: Element, style: str) -> str | None:
    """Returns the text colour an element sets, unless it's a default one (black, grey)."""
    match = COLOR_STYLE.search(style)
    if match:
        value = match.group(1)
    elif element.tag == "font":
        value = (element.attrs.get("color") or "").lower().replace(" ", "")
    else:
        return None
    value = value.replace("!important", "")
    if not SAFE_COLOR.fullmatch(value) or value in NEUTRAL_COLORS:
        return None
    rgb = RGB_COLOR.fullmatch(value)
    if rgb:
        channels = rgb.groups()
    elif re.fullmatch(r"#[0-9a-f]{6}", value):
        channels = (value[1:3], value[3:5], value[5:7])
    elif re.fullmatch(r"#[0-9a-f]{3}", value):
        channels = tuple(value[1:])
    else:
        channels = ("named",)
    return None if len(set(channels)) == 1 and channels != ("named",) else value


def obsidian_tag(tag: str) -> str:
    return TAG_SPECIAL.sub("-", tag.strip().lstrip("#")).strip("-")


class NoteRenderer:
    """Renders one note's ENML body as Markdown."""

    def __init__(self, attachments: dict[str, Attachment], folder: NoteFolder, add_note_link,
                 title: str = ""):
        self.attachments = attachments
        self.hints = title_languages(title)
        # per code block: the language Evernote recorded, or the scores of its guess
        self.languages: list[str | dict[str, int]] = []
        self.folder = folder
        self.add_note_link = add_note_link
        self.used: set[str] = set()
        self.missing = 0
        self.code = False
        self.list_depth = 0
        self.cell_depth = 0
        self.marks: set[str] = set()
        self.color: str | None = None

    def render(self, enml: str) -> str:
        builder = TreeBuilder()
        builder.feed(enml)
        builder.close()
        body = resolve(self.block_children(builder.root))
        unused = [att for key, att in self.attachments.items()
                  if key not in self.used]
        for attachment in unused:
            if body:
                body += f"\n{BLANK_LINE}\n" if IMAGE_LINE_END.search(
                    body) else "\n\n"
            body += resolve(self.embed(attachment))
        body = join_touching_code(self.fill_languages(body))
        return "\n".join(line.rstrip() for line in body.split("\n"))

    def fence(self, text: str, language: str | None) -> str:
        """Fences a code block; with no language recorded (None), its language is decided later."""
        # code inside an indent starts at the margin, keeping its own indentation
        text = textwrap.dedent(text.strip("\n").rstrip())
        if not text.strip():
            return ""
        if language is None:
            self.languages.append(language_scores(text, self.hints))
            language = f"\x07{len(self.languages) - 1}\x07"
        else:
            self.languages.append(language)
        return fenced(text, language)

    def fill_languages(self, body: str) -> str:
        """Names each guessed block's language, taking the note's usual language when the
        block's own code is too short to tell but fits it."""
        named = [entry if isinstance(entry, str) else confident_language(entry)
                 for entry in self.languages]
        usual = collections.Counter(
            language for language in named if language).most_common(1)

        def language(match: re.Match) -> str:
            index = int(match.group(1))
            scores = self.languages[index]
            if named[index] or not usual or isinstance(scores, str):
                return named[index]
            best = usual[0][0]
            # the usual language may be one Evernote recorded that isn't guessed, such as ruby
            score = scores.get(best, 0)
            return best if score >= 1 and score == max(scores.values()) else ""

        return LANGUAGE_SLOT.sub(language, body)

    def node(self, node: Element | str) -> str:
        if isinstance(node, str):
            return self.text(node)
        tag = node.tag
        style = (node.attrs.get("style") or "").lower().replace(" ", "")
        if tag in SKIPPED_TAGS:
            return ""
        if tag == "br":
            return BREAK
        if self.code:
            inner = self.children(node)
            if is_indent(node) or (tag in BLOCK_TAGS and INDENT_STYLE.search(style)):
                # old notes indented code with Evernote's indent, not spaces
                inner = CODE_MARKER_RUN.sub(code_newlines, inner).strip("\n")
                inner = "\n".join(CODE_INDENT + line if line.strip() else line
                                  for line in inner.split("\n"))
            return LINE + inner + LINE if tag in BLOCK_TAGS or tag in ("p", "blockquote") else inner
        return self.with_color(node, style, lambda own: self.element(node, tag, style, own))

    def with_color(self, element: Element, style: str, render) -> str:
        """Renders an element with its text colour as the current one, if it sets one."""
        color = text_color(element, style)
        if not color or color == self.color:
            return render(False)
        outer, self.color = self.color, color
        try:
            return render(True)
        finally:
            self.color = outer

    def element(self, node: Element, tag: str, style: str, own_color: bool) -> str:
        if tag == "en-media":
            return self.media(node)
        if tag == "en-todo":
            box = "[x] " if node.attrs.get("checked") == "true" else "[ ] "
            return box if self.list_depth else "- " + box
        if tag == "en-crypt":
            return "*(encrypted content not exported)*"
        if tag == "img":
            return self.image(node)
        if tag == "pre" or "-en-codeblock:true" in style:
            return self.code_block(node, tag == "pre", recorded_language(style))
        if tag in HEADING_TAGS:
            return self.heading(node, int(tag[1]))
        if tag in ("ul", "ol"):
            return self.list(node, tag == "ol", style)
        if tag == "table":
            return self.table(node)
        if tag == "blockquote" and not is_indent(node):
            return self.blockquote(node)
        if tag == "p":
            return PARA + self.block_children(node) + PARA
        if tag in BLOCK_TAGS or tag == "blockquote":
            if INDENT_STYLE.search(style) or tag == "blockquote":
                return INDENT_START + LINE + self.block_children(node) + LINE + INDENT_END
            return LINE + self.block_children(node) + LINE
        if tag == "hr":
            return PARA + DIVIDER + PARA
        if tag == "a":
            inline = self.link(node)
        elif tag in CODE_TAGS or is_monospace(node):
            inline = self.inline_code(node)
        else:
            marks = [mark for applies, mark in (
                (tag in ("b", "strong") or re.search(
                    r"font-weight:(bold|[6-9]00)", style), "**"),
                (tag in ("i", "em", "cite") or "font-style:italic" in style, "*"),
                (tag in ("s", "strike", "del") or "line-through" in style, "~~"),
                ("--en-highlight:" in style or "-evernote-highlight:true" in style, "=="),
                (tag in ("u", "ins") or "underline" in style, "<u>"),
            ) if applies]
            inline = self.wrap(node, marks)
        # a block colours its own lines in block_children; an inline element here
        return self.colored(inline) if own_color else inline

    def children(self, element: Element) -> str:
        return "".join(map(self.node, self.merge_monospace(element.children)))

    def merge_monospace(self, kids: list[Element | str]) -> list[Element | str]:
        """Joins consecutive monospaced pieces, one per coloured token, into one piece of code."""
        if self.code:
            return kids
        merged: list[Element | str] = []
        run: list[Element | str] = []

        def flush():
            trailing = []
            while run and isinstance(run[-1], str):
                trailing.insert(0, run.pop())
            if len(run) > 1:
                code = Element("code", [])
                code.children = run[:]
                merged.append(code)
            else:
                merged.extend(run)
            merged.extend(trailing)
            run.clear()

        for kid in kids:
            if isinstance(kid, str):
                if run and not kid.strip():
                    run.append(kid)
                    continue
            elif is_inline_code(kid):
                run.append(kid)
                continue
            flush()
            merged.append(kid)
        flush()
        return merged

    def block_children(self, element: Element) -> str:
        """Renders a block's content, dropping a <br> that ends it, as a browser does."""
        kids = element.children
        end = len(kids)
        while end and isinstance(kids[end - 1], str) and not kids[end - 1].strip():
            end -= 1
        last = kids[end - 1] if end else None
        if isinstance(last, Element) and last.tag == "br" and any(
                not isinstance(kid, str) or kid.strip() for kid in kids[:end - 1]):
            kids = kids[:end - 1]
        return self.colored(self.blocks(kids))

    def blocks(self, kids: list[Element | str]) -> str:
        """Renders a block's children, turning each run of monospaced lines into one code block."""
        parts: list[str] = []
        run: list[Element] = []
        for kid in self.merge_monospace(kids):
            if run and isinstance(kid, str) and not kid.strip():
                continue  # the whitespace between code lines
            if not self.code and isinstance(kid, Element) and is_code_line(kid):
                run.append(kid)
                continue
            parts.append(self.code_lines(run))
            run = []
            parts.append(self.node(kid))
        parts.append(self.code_lines(run))
        return "".join(parts)

    def code_lines(self, lines: list[Element]) -> str:
        """Renders monospaced lines pasted from an editor as one code block."""
        if not any(code_text(line) for line in lines):
            # empty lines alone are just blank lines
            return "".join(map(self.node, lines))
        # an Evernote code block among the lines keeps the language it recorded
        styles = [(line.attrs.get("style") or "").lower().replace(" ", "")
                  for line in lines]
        recorded = [recorded_language(
            style) for style in styles if "-en-codeblock:true" in style]
        language = next(
            (value for value in recorded if value is not None), None)
        self.code = True
        try:
            text = CODE_MARKER_RUN.sub(
                code_newlines, LINE.join(map(self.node, lines)))
        finally:
            self.code = False
        return self.fence(text, language)

    def colored(self, text: str) -> str:
        """Shows each line of text in the current colour, keeping list markers outside."""
        if not self.color or self.code:
            return text
        opening = f'<span style="color: {self.color}">'
        parts = SEGMENTS.split(text)
        for index in range(0, len(parts), 2):  # odd parts are line markers
            part = parts[index]
            core = part.strip()
            prefix_match = LIST_PREFIX.match(core)
            prefix = prefix_match.group(0) if prefix_match else ""
            body = core[len(prefix):]
            # a finished block (a list, a table) can't sit in a span; its lines
            # were coloured as it rendered
            if not body or "\n" in body or (body.startswith(opening) and body.endswith("</span>")):
                continue
            lead = part[:len(part) - len(part.lstrip())]
            trail = part[len(part.rstrip()):]
            parts[index] = f"{lead}{prefix}{opening}{body}</span>{trail}"
        return "".join(parts)

    def text(self, value: str) -> str:
        value = CONTROL_CHARS.sub("", value).replace("\xa0", " ")
        if self.code:
            return value.replace("\r", "").replace("\n", "")
        return MARKDOWN_SPECIAL.sub(r"\\\1", re.sub(r"\s+", " ", value))

    def wrap(self, element: Element, marks: list[str]) -> str:
        """Renders an element's content inside the marks, such as ** or <u>, not yet open."""
        marks = [mark for mark in marks if mark not in self.marks]
        self.marks.update(marks)
        try:
            inner = self.children(element)
        finally:
            self.marks.difference_update(marks)
        core = inner.strip()
        if not marks or not core or any(marker in inner for marker in MARKERS):
            return inner
        lead = inner[:len(inner) - len(inner.lstrip())]
        trail = inner[len(inner.rstrip()):]
        closing = "".join("</" + mark[1:] if mark.startswith("<") else mark
                          for mark in reversed(marks))
        return f"{lead}{''.join(marks)}{core}{closing}{trail}"

    def link(self, element: Element) -> str:
        href = (element.attrs.get("href") or "").strip()
        label = self.children(element)
        core = label.strip()
        if (not href or not core or href.startswith("#")
                or href.lower().startswith("javascript:")
                or any(marker in label for marker in MARKERS)):
            return label
        lead = label[:len(label) - len(label.lstrip())]
        trail = label[len(label.rstrip()):]
        if href.lower().startswith("evernote:"):
            title = re.sub(r"\s+", " ", raw_text(element)).strip()
            return lead + self.add_note_link(core, title, href) + trail
        return f"{lead}[{core}]({link_target(href)}){trail}"

    def inline_code(self, element: Element) -> str:
        text = re.sub(r"[\r\n]+", " ", raw_text(element).replace("\xa0", " "))
        if self.code or not text.strip():
            return text
        ticks = "`" * (longest_run(text, "`") + 1)
        pad = " " if text.startswith("`") or text.endswith("`") else ""
        return f"{ticks}{pad}{text}{pad}{ticks}"

    def code_block(self, element: Element, is_pre: bool, language: str | None) -> str:
        if is_pre:
            text = raw_text(element).replace("\xa0", " ").replace("\r", "")
        else:
            self.code = True
            try:
                text = CODE_MARKER_RUN.sub(
                    code_newlines, self.block_children(element))
            finally:
                self.code = False
        return self.fence(text, language)

    def heading(self, element: Element, level: int) -> str:
        text = re.sub(r"\\?\n", " ", resolve(self.block_children(element)))
        return PARA + "#" * level + " " + text + PARA if text else ""

    def blockquote(self, element: Element) -> str:
        body = resolve(self.block_children(element))
        if not body:
            return ""
        return PARA + "\n".join("> " + line if line else ">" for line in body.split("\n")) + PARA

    def list(self, element: Element, ordered: bool, style: str) -> str:
        todo = "--en-todo:true" in style
        start = element.attrs.get("start") or ""
        number = int(start) if start.isdigit() else 1
        items: list[str] = []
        widths: list[int] = []
        self.list_depth += 1
        try:
            for child in element.children:
                if isinstance(child, str) and not child.strip():
                    continue
                if isinstance(child, Element) and child.tag in ("ul", "ol"):
                    # Evernote nests a sublist directly in its parent list
                    nested = resolve(self.node(child))
                    if nested and items:
                        items[-1] += "\n" + indent(nested, widths[-1])
                    elif nested:
                        items.append(nested)
                        widths.append(0)
                    continue
                is_item = isinstance(child, Element) and child.tag == "li"
                if is_item:
                    child_style = (child.attrs.get("style")
                                   or "").replace(" ", "").lower()
                    body = resolve(self.with_color(
                        child, child_style, lambda own: self.block_children(child)))
                else:
                    body = resolve(self.node(child))
                bullet = f"{number}." if ordered else "-"
                marker = bullet
                if todo and is_item:
                    marker += " [x]" if "--en-checked:true" in child_style else " [ ]"
                elif not body:
                    continue
                items.append(f"{marker} " + indent(body,
                             len(bullet) + 1, first=False))
                widths.append(len(bullet) + 1)
                number += 1
        finally:
            self.list_depth -= 1
        if not items:
            return ""
        # a picture ending an item gets its blank line inside the item; after
        # the last item, the list's own end gives it
        for index in range(len(items) - 1):
            if IMAGE_LINE_END.search(items[index]):
                items[index] += "\n" + " " * widths[index] + BLANK_LINE
        separator = LINE if self.list_depth else PARA
        return separator + "\n".join(items) + separator

    def table(self, element: Element) -> str:
        rows = [[cell for cell in row.children
                 if isinstance(cell, Element) and cell.tag in ("td", "th")]
                for row in table_rows(element)]
        rows = [row for row in rows if row]
        if not rows:
            return ""
        width = max(map(len, rows))
        if width == 1:
            # a one-column table is a layout box: its cells read best as paragraphs
            return "".join(PARA + self.block_children(row[0]) + PARA for row in rows)
        cells = [[self.cell(cell) for cell in row] + [""]
                 * (width - len(row)) for row in rows]
        lines = [table_row(cells[0]), table_row(["---"] * width)]
        lines += [table_row(row) for row in cells[1:]]
        return PARA + "\n".join(lines) + PARA

    def cell(self, element: Element) -> str:
        self.cell_depth += 1
        try:
            text = resolve(self.block_children(element)).replace("|", "\\|")
        finally:
            self.cell_depth -= 1
        return re.sub(r"\\?\n+", "<br>", text)

    def media(self, element: Element) -> str:
        key = (element.attrs.get("hash") or "").lower()
        attachment = self.attachments.get(key)
        if attachment is None:
            self.missing += 1
            return ""
        self.used.add(key)
        return self.embed(attachment)

    def image(self, element: Element) -> str:
        src = (element.attrs.get("src") or "").strip()
        if src.startswith("data:"):
            attachment = self.save_data_uri(src)
            return self.embed(attachment) if attachment else ""
        if not src:
            return ""
        alt = MARKDOWN_SPECIAL.sub(r"\\\1", re.sub(
            r"\s+", " ", element.attrs.get("alt") or "").strip())
        return f"![{alt}]({link_target(src)})"

    def save_data_uri(self, src: str) -> Attachment | None:
        match = re.match(r"data:([\w/+.-]*)((?:;[^,]*)?),(.*)", src, re.S)
        if not match:
            return None
        try:
            if ";base64" in match.group(2):
                data = base64.b64decode(match.group(3))
            else:
                data = unquote_to_bytes(match.group(3))
        except (binascii.Error, ValueError):
            return None
        if not data:
            return None
        mime = match.group(1) or "application/octet-stream"
        return self.folder.save_attachment(Resource(data, mime, "", hashlib.md5(data).hexdigest()))

    def embed(self, attachment: Attachment) -> str:
        """Shows a picture or a PDF in the note, as Obsidian can; any other file is a block
        with its link, as Evernote shows it, or a bare link in a table cell."""
        target = local_link(attachment.path)
        if (attachment.mime.startswith("image/") or attachment.mime == "application/pdf"
                or attachment.name.lower().endswith(".pdf")):
            return f"![]({target})"
        name = MARKDOWN_SPECIAL.sub(r"\\\1", attachment.name)
        link = f"[{name}]({target})"
        if self.cell_depth:
            return link
        # a callout, which Obsidian draws as a box; other editors show a quote
        return PARA + f"> [!attachment] {link}" + PARA


class Element:
    """An HTML element of a note's body."""
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: str, attrs: list[tuple[str, str | None]]):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children: list[Element | str] = []


class TreeBuilder(HTMLParser):
    """Builds an Element tree from HTML that may not be well formed."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Element("root", [])
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        element = Element(tag, attrs)
        self.stack[-1].children.append(element)
        if tag not in VOID_TAGS:
            self.stack.append(element)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(Element(tag, attrs))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def resolve(text: str) -> str:
    """Turns the line and paragraph placeholders of a finished block into newlines."""
    text = ADJACENT_IMAGES.sub(
        r"\1" + LINE, text)  # each picture on its own line
    return MARKER_RUN.sub(newlines, text).strip()


def newlines(match: re.Match) -> str:
    run, text = match.group(0), match.string
    if match.start() == 0 or match.end() == len(text):
        return "\n"  # the block's edge, stripped by resolve()
    block_gap = PARA in run
    # the <br> of each empty <div> is a blank line; between lines, the first
    # newline only ends the line
    blank = run.count(BREAK) if block_gap else newline_count(run) - 1
    if INDENT_END in run and INDENT_START not in run:
        blank = max(blank, 1)  # the indented lines end here
    before_block = BLOCK_START.match(text, match.end())
    line_start = max(text.rfind(char, 0, match.start())
                     for char in ("\n", *MARKERS)) + 1
    if text[line_start:match.start()] == DIVIDER or DIVIDER_START.match(text, match.end()):
        # Obsidian draws a divider with a blank line's margin around it, so the
        # bare blank line Markdown needs there is all the space it gets
        return "\n\n"
    if IMAGE_LINE_END.search(text, line_start, match.start()):
        blank = 1  # always one blank line under a picture, however many were typed
    # a line after a list item, such as a to-do line, would join the item
    after_item = LIST_ITEM.match(text, line_start)
    if not blank:
        if block_gap or (after_item and not before_block):
            return "\n\n"
        return "\n" if before_block else HARD_BREAK
    if before_block:
        return "\n\n" + (BLANK_LINE + "\n\n") * blank
    if block_gap or after_item:
        return "\n\n" + (BLANK_LINE + HARD_BREAK) * blank
    return HARD_BREAK + (BLANK_LINE + HARD_BREAK) * blank


def code_newlines(match: re.Match) -> str:
    return "\n" * newline_count(match.group(0))


def newline_count(run: str) -> int:
    block = 2 if PARA in run else 1 if LINE in run else 0
    return max(block, run.count(BREAK) + (1 if block else 0))


def join_touching_code(text: str) -> str:
    """Joins pieces of inline code that touch, as pieces from different nesting levels can:
    `a``b` would read as one broken piece."""
    return TOUCHING_CODE.sub(lambda match: "`" + match.group(0)[1:-1].replace("``", "") + "`", text)


def fenced(text: str, language: str) -> str:
    """Wraps code in a fence longer than any run of backticks it holds, naming its language."""
    fence = "`" * max(3, longest_run(text, "`") + 1)
    return PARA + f"{fence}{language}\n{text}\n{fence}" + PARA


def recorded_language(style: str) -> str | None:
    """Returns the language of an Evernote code block, "" for plain text, None if it has none."""
    match = re.search(r"--en-syntaxlanguage:([\w#+-]+)", style)
    if not match:
        return None
    language = match.group(1)
    return "" if language in ("plaintext", "text", "plain") else EVERNOTE_LANGUAGES.get(language, language)


def guess_language(code: str, hints: tuple[str, ...] = ()) -> str:
    """Names the language of a piece of code, or "" when unsure; hints are languages its note names."""
    return confident_language(language_scores(code, hints))


def language_scores(code: str, hints: tuple[str, ...] = ()) -> dict[str, int]:
    """Scores each language by the patterns of it the code has, and a point if its note names it.

    A tag that opens the code makes it markup outright, since CSS or script
    inside a page is still the page; valid JSON is JSON.
    """
    scores = dict.fromkeys(LANGUAGE_PATTERNS, 0)
    stripped = code.strip()
    if stripped[:1] in "{[":
        try:
            json.loads(stripped)
            return {**scores, "json": 10}
        except ValueError:
            pass
    if stripped.startswith("<") and re.search(r"</[\w:.-]+>|/>", code):
        markup = "xml" if re.search(
            r"<\?xml|<xsl:|xmlns(:\w+)?=\"http://www\.w3\.org/1999/XSL", code) else "html"
        return {**scores, markup: 10}
    lines = [line for line in stripped.split(
        "\n") if line.strip() and not line.lstrip().startswith("#")]
    if len(lines) >= 2 and not re.search(r"[;{}]", code) and all(map(YAML_LINE.match, lines)):
        # keys and list items only, such as a docker-compose file
        return {**scores, "yaml": 10}
    for language, patterns in LANGUAGE_PATTERNS.items():
        scores[language] = sum(
            weight for pattern, weight in patterns if pattern.search(code))
    if lines and all(re.match(SHELL_COMMAND, line) for line in lines):
        # nothing but commands, such as one "brew install"
        scores["bash"] += STRONG
    for language in hints:
        scores[language] += 1
    return scores


def confident_language(scores: dict[str, int]) -> str:
    """Names the best-scoring language when it has two points and a clear lead, since a
    wrong language colours code worse than none; otherwise ""."""
    ranked = sorted(scores.items(), key=lambda item: -item[1])
    (best, top), (_, second) = ranked[0], ranked[1]
    return best if top >= 2 and top > second else ""


def title_languages(title: str) -> tuple[str, ...]:
    """Returns the languages a note's title names, such as css for "Gradients CSS3"."""
    return tuple(language for language, pattern in TITLE_LANGUAGES.items() if pattern.search(title))


def is_monospace(element: Element) -> bool:
    """Tells whether an element sets a monospaced font, as code pasted from an editor has."""
    style = (element.attrs.get("style") or "").lower()
    match = re.search(r"font-family:([^;]+)", style)
    family = match.group(1) if match else (
        element.attrs.get("face") or "").lower()
    first = family.split(",")[0].strip(" '\"")
    return bool(first) and bool(MONOSPACE_FONT.search(first))


def is_indent(element: Element) -> bool:
    """Tells whether a blockquote is older Evernote's indent, drawn with no quote bar."""
    style = (element.attrs.get("style") or "").lower().replace(" ", "")
    return element.tag == "blockquote" and "border:none" in style


def is_code_line(element: Element) -> bool:
    """Tells whether a block holds only monospaced text: a line of pasted code, or an empty one."""
    if element.tag not in BLOCK_TAGS and element.tag != "p" and not is_indent(element):
        return False
    if element.tag in ("li", "td", "th", "tr", "thead", "tbody", "tfoot", "dl", "en-note"):
        return False
    return monospace_only(element, False) == "mono"


def is_inline_code(element: Element) -> bool:
    """Tells whether an inline element holds only monospaced text, such as one coloured token."""
    if element.tag in BLOCK_TAGS or element.tag in ("p", "blockquote", "br", "pre", "a"):
        return False
    return monospace_only(element, False) == "mono" and bool(code_text(element))


def monospace_only(element: Element, mono: bool) -> str | None:
    """Returns "mono" when all of an element's text is monospaced and some font says so,
    "empty" when it holds no text and no monospaced font, None when some text isn't monospaced."""
    mono = mono or is_monospace(element) or element.tag in CODE_TAGS
    found = "mono" if mono else "empty"
    for child in element.children:
        if isinstance(child, str):
            if child.strip() and not mono:
                return None
        elif child.tag in ("en-media", "img", "table", "ul", "ol", "hr", "en-todo"):
            return None
        elif child.tag not in SKIPPED_TAGS:
            result = monospace_only(child, mono)
            if result is None:
                return None
            if result == "mono":
                found = "mono"
    return found


def code_text(element: Element) -> str:
    return raw_text(element).replace("\xa0", " ").strip()


def raw_text(element: Element) -> str:
    parts = []
    for child in element.children:
        if isinstance(child, str):
            parts.append(CONTROL_CHARS.sub("", child))
        elif child.tag == "br":
            parts.append("\n")
        elif child.tag not in SKIPPED_TAGS:
            parts.append(raw_text(child))
    return "".join(parts)


def indent(text: str, width: int, first: bool = True) -> str:
    lines = text.split("\n")
    pad = " " * width
    indented = [pad + line if line else "" for line in lines[1:]]
    return "\n".join([(pad if first and lines[0] else "") + lines[0]] + indented)


def table_rows(table: Element):
    for child in table.children:
        if isinstance(child, Element):
            if child.tag == "tr":
                yield child
            elif child.tag in ("thead", "tbody", "tfoot"):
                yield from (row for row in child.children
                            if isinstance(row, Element) and row.tag == "tr")


def table_row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def longest_run(text: str, char: str) -> int:
    return max((len(run) for run in re.findall(re.escape(char) + "+", text)), default=0)


def link_target(url: str) -> str:
    return URL_SPECIAL.sub(lambda match: quote(match.group(0)), url)


def local_link(path: str) -> str:
    return PATH_SPECIAL.sub(lambda match: quote(match.group(0)), path.replace(os.sep, "/"))


def find_enex_files(inputs: list[str], parser: argparse.ArgumentParser) -> list[str]:
    files = []
    for path in inputs:
        if os.path.isdir(path):
            files += sorted(os.path.join(path, name) for name in os.listdir(path)
                            if name.lower().endswith(".enex"))
        elif os.path.isfile(path):
            files.append(path)
        else:
            parser.error(f"{path} does not exist")
    if not files:
        parser.error("no .enex files found")
    return files


def output_plan(files: list[str], output: str | None, flat: bool) -> list[tuple[str, str]]:
    """Pairs each .enex file with the folder its notes go to."""
    def stem(path: str) -> str:
        return safe_name(os.path.splitext(os.path.basename(path))[0])

    if output is None:
        return [(path, os.path.join(os.path.dirname(os.path.abspath(path)), stem(path)))
                for path in files]
    if len(files) == 1 or flat:
        return [(path, output) for path in files]
    return [(path, os.path.join(output, stem(path))) for path in files]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Convert Evernote exports (.enex) into Markdown notes, with "
                    "front matter, attachments and links between notes, ready "
                    "for Obsidian or a Git repository.")
    parser.add_argument("inputs", nargs="+", metavar="ENEX",
                        help=".enex files, or folders holding them")
    parser.add_argument("-o", "--output",
                        help="folder to write the notes to; it must be new or empty "
                             "(default: a folder named after each .enex file, next to it)")
    parser.add_argument("--flat", action="store_true",
                        help="with -o and several input files, put every note in the "
                             "output folder instead of one subfolder per file")
    args = parser.parse_args(argv)
    if args.flat and not args.output:
        parser.error("--flat needs -o")
    plan = output_plan(find_enex_files(
        args.inputs, parser), args.output, args.flat)
    for folder in dict.fromkeys(folder for _, folder in plan):
        if os.path.isdir(folder) and any(
                not name.startswith(".") for name in os.listdir(folder)):
            parser.error(
                f"{folder} is not empty: move it away or choose another folder with -o")
    sys.setrecursionlimit(10000)  # web clips can nest elements deeply

    exporter = Exporter()
    exporter.export(plan)
    for warning in exporter.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    folders = ", ".join(dict.fromkeys(
        args.output or folder for _, folder in plan))
    print(f"Wrote {exporter.notes} notes and {exporter.attachment_count()} attachments "
          f"to {folders}", file=sys.stderr)
    if exporter.note_links:
        print(f"Linked {exporter.linked} of {len(exporter.note_links)} links between notes "
              "(the rest point at notes that aren't in the export)", file=sys.stderr)


if __name__ == "__main__":
    main()
