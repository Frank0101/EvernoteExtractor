#!/usr/bin/env python3
"""Convert Evernote export files (.enex) into a folder of Markdown notes.

Every note becomes one Markdown file named after its title, as typed but for
/ and : and a starting dot, which become look-alikes (a Mac can't hold the
first two in a file name, and the dot would hide the file), with YAML front
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
  line, and shows a small gap between lines typed with Enter, but none in a
  line that wraps. So each line becomes a paragraph of its own, which Obsidian
  shows with such a gap. A line break within a line (Shift+Enter), and the
  lines of a list item, quote or table cell, are a bare newline, which
  Obsidian shows as typed; most other previews (VS Code, GitHub) join such
  lines, and would need HARD_BREAK set to Markdown's backslash break.
- A blank line typed in Evernote, which marks a new section, becomes a line
  holding only &nbsp;, as the last line of the paragraph above it: a bare
  blank line in Markdown only separates paragraphs, so the new section starts
  after a line's space and a paragraph's, a gap bigger than between lines.
  After a line of text, whatever follows, and under a picture, it becomes two
  such lines, since Obsidian's gap between paragraphs is nearly a line already.
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
  indented with a <blockquote> drawn without a bar, or around code: it's an
  indent, not a quote.
- Code pasted from an editor isn't an Evernote code block but lines in a
  monospaced font, with every token coloured; a run of lines whose text is all
  monospaced becomes one fenced code block, keeping its indentation and losing
  the syntax colours, and a monospaced word in a sentence becomes `code`.
  Old notes indented such code with Evernote's indent, which becomes spaces.
- A code block keeps the language Evernote recorded; with none, the language
  is guessed from patterns in the code, the languages the note's title and
  tags name, and the language of the note's other code blocks, and left unset
  when unsure.
- Code is kept as typed: nothing is reformatted.
- A blank line typed under a picture becomes two lines holding &nbsp;, however
  many were typed, so pictures are spaced evenly; with none typed there is
  none. A PDF, which Obsidian shows in a viewer of its own, is spaced as text. A divider gets only the bare blank
  lines Markdown needs around it, whatever was typed: Obsidian already draws
  it with a blank line's margin above and below, next to a picture too.
- Tags are made valid Obsidian tags: no spaces or punctuation but - _ and /,
  so "Work - Ideas" becomes "Work-Ideas", and "C#" becomes "CSharp".
- Only the Python standard library is used, so there is nothing to install.
"""

from __future__ import annotations

import argparse
import base64
import collections
import contextlib
import hashlib
import html
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

# Output files
ATTACHMENTS_DIR = "attachments"
MAX_NAME_LENGTH = 100
# A note's file name keeps its title; a Mac can't hold / or : in one, so they
# become look-alikes, as does a dot that starts it, which would hide the file
NOTE_NAME_LOOKALIKES = {"/": "\u2215", ":": "\ua789"}
HIDING_DOT = "\u2024"
# a Mac takes names of up to 255 bytes, ".md" and " (2)" included
MAX_NAME_BYTES = 240
UNSAFE_NAME = re.compile(r'[\\/:*?"<>|#^\[\]\x00-\x1f]')
TAG_SPECIAL = re.compile(r"[^\w/]*[^\w/-][^\w/]*|-{2,}")
URL_SPECIAL = re.compile(r"[\s()<>]")
PATH_SPECIAL = re.compile(r"[\s()<>%#?\"]")

# Placeholders a rendered block holds until its content is complete; resolve()
# turns each run of them into line breaks, paragraph breaks and blank lines:
# LINE ends a line (a <div>), PARA a block (a list, a divider), BREAK is a <br>;
# INDENT_START and INDENT_END enclose indented lines, which a blank line ends.
LINE, PARA, BREAK, INDENT_START, INDENT_END = "\x01", "\x02", "\x03", "\x04", "\x05"
MARKERS = (LINE, PARA, BREAK, INDENT_START, INDENT_END)
MARKER_RUN = re.compile(r"[ \t]*[\x01-\x05](?:[ \t]*[\x01-\x05])*[ \t]*")
BARE_MARKER_RUN = re.compile(r"([\x01-\x05]+)")
# Stands for a code block's language until every block of the note is known
LANGUAGE_SLOT = re.compile(r"\x07(\d+)\x07")
# a fenced block whose language is yet to be named, at the margin or in a list item
FENCED_BLOCK = re.compile(
    r"^([ \t]*(?:[-*+] |\d+\. )?)(`{3,})(\x07\d+\x07)\n(.*?)\n(?=[ \t]*\2$)", re.M | re.S)
BRACE_LANGUAGES = {"csharp", "javascript", "typescript", "css", "json", "java"}
MARKUP_LANGUAGES = {"xml", "html"}
BRACKETS = {"{": "}", "(": ")", "[": "]"}
MARKUP_TAG = re.compile(r"<(/?)\s*([A-Za-z][\w:.-]*)(?:\s[^<>]*?)?(/?)>")
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
             "source", "track", "wbr", "!doctype"}
# Stands for a link to another note until every note's file is known
NOTE_LINK = re.compile(r"\x06(\d+)\x06")

# Markdown
# A line that starts a block of its own; a hard line break before it, or at
# the end of a block, would show as a literal backslash.
BLOCK_START = re.compile(r"(?:[-*+] |\d+[.)] |#|>|\||```|~~~|---)")
LIST_ITEM = re.compile(r"[ \t]*(?:[-*+]|\d+[.)]) ")
# A line of a block that a next line would join: a list item, a quote, a table
# row, or an indented line of one of them
BLOCK_LINE = re.compile(r"[ \t]|(?:[-*+]|\d+[.)]) |>|\|")
CODE_FENCE = re.compile(r"`{3,}|~{3,}")
CODE_FENCE_LINE = re.compile(r"^`{3,}", re.M)
NESTED_ITEM = re.compile(r"\s*([-*+]|\d+\.) ")
TRAILING_BLANK_LINES = re.compile(r"(\n[ \t]*(&nbsp;)?)+$")
LIST_PREFIX = re.compile(r"(?:[-*+]|\d+\.) (?:\[[ x]\] )?")
IMAGE_LINE_END = re.compile(r"!\[[^\]]*\]\([^()\s]*\)(?:</span>)*$")
PDF_LINE_END = re.compile(r"!\[[^\]]*\]\([^()\s]*\.pdf\)(?:</span>)*$", re.I)
ADJACENT_IMAGES = re.compile(r"(!\[[^\]]*\]\([^()\s]*\))[ \t]*(?=!\[)")
DIVIDER = "---"
DIVIDER_START = re.compile(r"---(?:[\x01-\x05]|$)")
HARD_BREAK = "\n"
BLANK_LINE = "&nbsp;"
MARKDOWN_SPECIAL = re.compile(r"([\\`*\[\]<]|(?<!\w)_|_(?!\w))")
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
# Text that would start a list, a heading or a quote if a line began with it,
# such as "1) " typed by hand, holds LINE_START_MARK where a backslash would
# keep it text; once the note is complete, the mark becomes that backslash
# where it is at the start of a line, in a quote too, and is dropped elsewhere
LINE_START_MARK = "\x10"
LOOKS_LIKE_BLOCK = re.compile(r"^( *(?:\d+(?=[.)] )|(?=[-+] )|(?=[#>])))")
MARKED_LINE_START = re.compile(r"^((?:[ \t]*>)*[ \t]*\d*)\x10", re.M)
TOUCHING_CODE = re.compile(r"(?<!`)`[^`\n]+`(?:`[^`\n]+`)+(?!`)")
CODE_INDENT = "    "
# A formatting mark, such as ** for bold, is held as a token until the note is
# complete, so that pieces with the same mark that touch can be joined: *a**b*
# would read as bold. An opening token is the mark between MARK_OPEN, a closing
# one between MARK_CLOSE.
MARK_OPEN, MARK_CLOSE = "\x0e", "\x0f"
HIGHLIGHT_OPEN, HIGHLIGHT_CLOSE = MARK_OPEN + \
    "==" + MARK_OPEN, MARK_CLOSE + "==" + MARK_CLOSE
# a colour span right inside one of the same colour, as joining highlights may leave
DOUBLE_SPAN = re.compile(
    r"(<span[^>]*>)\1((?:(?!</?span\b)[\s\S])*?)</span></span>")
# Evernote's highlight colour as a plain background, as some notes store a highlight
HIGHLIGHT_YELLOW = re.compile(
    r"background(-color)?:(rgb\(255,250,165\)|#fffaa5)")
LINK_LINE = re.compile(r"\s*(?:\[[^\]\n]*\]\([^)\s]*\)|\x06\d+\x06)\s*")
LINE_END = re.compile(r"[\n\x01-\x05]|$")
MARK_TOKEN = re.compile(r"([\x0e\x0f])([^\x0e\x0f]+)\1")
# a space a mark held, written outside it, as Markdown can't end a mark on a space; it
# still joins the pieces on either side, which a space outside the mark keeps apart
MARKED_SPACE = "\x1f"
TOUCHING_MARKS = re.compile(r"\x0f([^\x0e\x0f]+)\x0f(\x1f*)\x0e\1\x0e")
# a colour span holding a marked piece in full, as a red word in a highlighted sentence
# an underline holding a coloured piece in full: a browser draws the line in the
# underline's own colour, so the colour goes outside it, as Evernote shows it
# text in a span, which may hold spans of its own, as a highlight's colour
SPAN_TEXT = r"(?:(?!</?span\b)[\s\S]|<span[^>]*>(?:(?!</?span\b)[\s\S])*?</span>)*?"
UNDERLINED_SPAN = re.compile(
    r"\x0e<u>\x0e(<span[^>]*>)(" + SPAN_TEXT + r")</span>\x0f<u>\x0f")
SPAN_MARKED = re.compile(
    r"(<span[^>]*>)\x0e([^\x0e\x0f]+)\x0e(" + SPAN_TEXT + r")\x0f\2\x0f</span>")

# HTML
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
CODE_TAGS = {"code", "kbd", "samp", "tt"}
INDENT_STYLE = re.compile(r"(?:padding|margin)-left:[1-9]")
MONOSPACE_FONT = re.compile(r"consolas|courier|monaco|menlo|monospace|lucida console|"
                            r"source code|fira code|sf mono|andale mono")

# Text colours
COLOR_STYLE = re.compile(r"(?<![-\w])color:([^;]+)")
SAFE_COLOR = re.compile(r"[#\w(),.%]+")
RGB_COLOR = re.compile(r"rgba?\((\d+),(\d+),(\d+)(?:,[\d.]+)?\)")
NEUTRAL_COLORS = {"black", "white", "gray", "grey", "inherit", "initial", "unset",
                  "currentcolor", "transparent"}
# How far apart a colour's red, green and blue can be for it to count as a grey
GREY_SPREAD = 24

# Code languages
# Evernote's names for code block languages that Obsidian names differently
EVERNOTE_LANGUAGES = {"cs": "csharp",
                      "js": "javascript", "ts": "typescript", "sh": "bash"}
# Patterns that point to one language, each worth a point, or two when given
# as (pattern, 2) because only that language has it; a language is named only
# with two points
STRONG = 2
SHELL_COMMAND = (r"^\s*(\$\s+)?([A-Z_][A-Z0-9_]*=\S*\s+)*"  # an environment variable may come first
                 r"(sudo|npm|npx|yarn|pnpm|dotnet|git|docker|kubectl|brew|pip3?|curl|"
                 r"cd|ls|mkdir|export|chmod|apt-get|az|aws|claude|codex|code|node|python3?|"
                 r"sails|nuget|ng|mvn|java|make|cat|echo|rm|cp|mv|touch|grep|"
                 # or a program run by its path, not a // comment
                 r"[\w.-]*/(?!/)[\w./-]+)\s")
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
    # a selector, not a keyword opening a block, as else {
    "css": [r"^(?!\s*(else|try|finally|do)\s*\{)\s*[.#]?[\w-]+([\s>+~.#:][\w-]+)*\s*\{\s*$", r"^\s*[\w-]+\s*:\s*[^;{}]+;\s*$",
            r"@media\b|!important\b", r"\b\d+(px|em|rem|%)\b"],
    "sql": [r"\bSELECT\b[\s\S]+\b(?i:from)\b", r"\b(INSERT\s+INTO|DELETE\s+FROM|CREATE\s+TABLE|ALTER\s+TABLE)\b",
            # after a dot, a method of the same name, as LINQ's .Where(
            r"\bUPDATE\s+\w+\s+SET\b", r"(?<!\.)\b(?i:where|group\s+by|order\s+by|inner\s+join|left\s+join|facet)\b"],
    "bash": [r"^\s*\$\s+\w", SHELL_COMMAND, (r"^#!.*\b(ba|z)?sh\b", STRONG)],
    "python": [r"^\s*def\s+\w+\(.*\):\s*$", r"^\s*(from\s+\w+\s+)?import\s+\w+\s*$",
               r"\bself\.\w+", r"^\s*if\s+__name__\s*==", r"\bprint\("],
    "xml": [], "html": [], "json": [], "yaml": [], "powershell": [r"^\s*\$\w+\s*=", r"\b(Get|Set|New|Remove|Invoke)-[A-Z]\w+"],
}.items()}
YAML_LINE = re.compile(r"\s*(-\s+)?[\w.\-\"']+:(\s+\S.*)?$|\s*-\s+\S")
# A tag (or the end of one), a comment, a declaration or ASP.NET's <% %> that opens markup
MARKUP_START = re.compile(r"</?(?:%|!|\?|[A-Za-z])")
ASPNET_BLOCK = re.compile(r"<%.*?%>", re.S)
XML_ONLY = re.compile(
    r"<\?xml|<xsl:|<asp:|xmlns(:\w+)?=\"http://www\.w3\.org/1999/XSL")
HTML_TAG = re.compile(r"<!DOCTYPE html|</?(html|head|body|div|span|p|a|ul|ol|li|table|tr|td|th|form|input|"
                      r"button|script|style|link|meta|img|br|h[1-6]|label|select|option|textarea|"
                      r"iframe|nav|header|footer|section)[\s>/]", re.I)
HTML_ATTRIBUTE = re.compile(r"[\w:.-]+=(\"[^\"]*\"|'[^']*')")
# A terminal session: a prompt such as "Mac:Git me$ " before the first command
SHELL_PROMPT = re.compile(r"[\w.@~:/-]+(?: [\w.@-]+)?\$ +\S")
JSON_MEMBER = re.compile(r"\"[^\"\n]+\"\s*:")
MARKDOWN_BADGE = re.compile(r"\[!\[[^\]]*\]\([^)]*\)\]\(")
# Words in a note's title that name a language; each is a hint, worth a point
TITLE_LANGUAGES = {language: re.compile(pattern, re.I) for language, pattern in {
    "csharp": r"C#|\.net\b|\bnet core\b|asp\.?net|\baspx\b|\blinq\b|entity framework|\bwpf\b|\bwcf\b|"
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


@dataclass
class Resource:
    """A file attached to a note, as the export holds it; its hash is the one
    Evernote gives it, which the note's <en-media> tags refer to."""
    data: bytes
    mime: str
    file_name: str = ""
    hash: str = field(init=False)

    def __post_init__(self):
        self.hash = hashlib.md5(self.data).hexdigest()


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
        attachments = {res.hash: folder.save_attachment(res)
                       for res in note.resources}
        renderer = NoteRenderer(attachments, folder,
                                self.add_note_link, note.title, note.tags)
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
        """Turns each link placeholder into a link to the note with its title,
        once every note's file is known."""
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

    def attachment_count(self) -> int:
        return sum(len(folder.attachments) for folder in self.folders.values())


class NoteFolder:
    """One output folder: the file names of its notes and its saved attachments."""

    def __init__(self, path: str):
        self.path = path
        self.note_names: set[str] = set()
        self.attachments: dict[str, Attachment] = {}
        self.attachment_names: set[str] = set()

    def note_path(self, title: str) -> str:
        return os.path.join(self.path, unique_name(note_name(title), ".md", self.note_names))

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


def note_name(title: str) -> str:
    """Turns a note's title into its file name, as typed but for what a Mac can't hold."""
    name = re.sub(r"\s+", " ", CONTROL_CHARS.sub(" ", title)).strip()
    name = "".join(NOTE_NAME_LOOKALIKES.get(char, char) for char in name)
    if name.startswith("."):
        name = HIDING_DOT + name[1:]
    name = name.encode()[:MAX_NAME_BYTES].decode(errors="ignore").rstrip()
    return name or "Untitled"


def safe_name(text: str, default: str = "Untitled") -> str:
    """Turns a name into one for an attachment or a folder that every OS and Obsidian accept."""
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
    def attribute(name: str) -> str | None:
        return (element.findtext(f"note-attributes/{name}") or "").strip() or None

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
    except ValueError:
        return None
    if not data:
        return None
    mime = (element.findtext("mime") or "").strip()
    file_name = element.findtext("resource-attributes/file-name") or ""
    return Resource(data, mime or "application/octet-stream", file_name.strip())


def parse_time(value: str | None) -> datetime | None:
    """Reads Evernote's UTC timestamps, such as 20240115T093000Z."""
    try:
        stamp = datetime.strptime((value or "").strip(), "%Y%m%dT%H%M%SZ")
    except ValueError:
        return None
    return stamp.replace(tzinfo=timezone.utc)


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


def obsidian_tag(tag: str) -> str:
    """Makes a tag valid in Obsidian; a # after a letter is spelled out, so C# is CSharp."""
    tag = re.sub(r"(?<=\w)#", "Sharp", tag.strip().lstrip("#"))
    return TAG_SPECIAL.sub("-", tag).strip("-")


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


class NoteRenderer:
    """Renders one note's ENML body as Markdown."""

    def __init__(self, attachments: dict[str, Attachment], folder: NoteFolder, add_note_link,
                 title: str = "", tags: list[str] | None = None):
        self.attachments = attachments
        self.folder = folder
        self.add_note_link = add_note_link
        # the languages the note's title and tags name, which hint at its code's
        self.hints = title_languages(" ".join([title, *(tags or [])]))
        # per code block: the language Evernote recorded, or the scores of its guess
        self.languages: list[str | dict[str, int]] = []
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
        body = resolve(self.block_children(builder.root), paragraphs=True)
        unused = [att for key, att in self.attachments.items()
                  if key not in self.used]
        for attachment in unused:
            if body:
                body += "\n\n"
            body += resolve(self.embed(attachment))
        body = join_touching_code(write_marks(
            escape_line_starts(self.fill_languages(body))))
        return "\n".join(line.rstrip() for line in body.split("\n"))

    # Blocks and their children

    def block_children(self, element: Element) -> str:
        """Renders a block's content, dropping a <br> that ends it or comes right before a
        block, as a browser does: the line ends there anyway."""
        children = unwrapped(element.children)
        kids = [kid for index, kid in enumerate(children)
                if not (isinstance(kid, Element) and kid.tag == "br"
                        and starts_line(next_kid(children, index + 1)))]
        line_start = 0
        for index, kid in enumerate(kids):
            if starts_line(kid):
                line_start = index + 1  # the next line starts after this block
                continue
            # a <br> ending a line inside formatting, as <span>a<br/></span>; on a
            # line of its own, it is a blank line
            ends_line = next_kid(kids, index + 1)
            if (isinstance(kid, Element) and kid.tag != "br"
                    and (ends_line is None or starts_line(ends_line))
                    and has_content(kids[line_start:index + 1])):
                drop_trailing_break(kid)
        end = len(kids)
        while end:
            kid = kids[end - 1]
            if not isinstance(kid, str) or kid.strip():
                break
            end -= 1
        last = kids[end - 1] if end else None
        if isinstance(last, Element) and last.tag == "br" and any(
                not isinstance(kid, str) or kid.strip() for kid in kids[:end - 1]):
            kids = kids[:end - 1]
        return self.colored(self.blocks(loose_code_lined(kids)))

    def blocks(self, kids: list[Element | str]) -> str:
        """Renders a block's children, turning each run of monospaced lines into one code block.

        Before Evernote had code blocks, code was pasted as lines in a monospaced
        font: pieces of it with only blank lines between them are one piece, unless
        their font or language differs, and a divider between them parts them, with
        no need to draw it, as each piece is a block of its own now.
        """
        items = self.merge_monospace(kids)
        parts: list[str] = []
        index = 0
        while index < len(items):
            if self.code or not starts_code(items[index]):
                parts.append(self.node(items[index]))
                index += 1
                continue
            run, index = code_run(items, index)
            while True:
                gap, after = blank_lines(items, index)
                if not gap or after == len(items) or not starts_code(items[after]):
                    break
                following, end = code_run(items, after)
                if any(map(is_divider_line, gap)):
                    # blank lines typed around the divider go with it
                    while run and not code_text(run[-1]):
                        run.pop()
                    parts.append(self.code_pieces(run))
                    run = following[next(index for index, line in enumerate(
                        following) if code_text(line)):]
                elif self.one_piece(run, following):
                    run = run + gap + following
                else:
                    # two blocks, with a bare blank line between them
                    parts.append(self.code_pieces(run))
                    run = following
                index = end
            parts.append(self.code_pieces(run))
        return "".join(parts)

    def code_pieces(self, run: list[Element]) -> str:
        """Renders a run of code lines, parting it at its empty lines where the pieces on
        either side differ in font, or are markup and script, as an HTML page and the
        script after it; a Razor view's C# and HTML, or C# and its LINQ query, are one."""
        filled = [index for index, line in enumerate(run) if code_text(line)]
        if not filled:
            return self.code_lines(run)
        piece = run[:filled[0] + 1]
        parts: list[str] = []
        for previous, index in zip(filled, filled[1:]):
            gap, following = run[previous + 1:index], [run[index]]
            if gap and self.page_and_script(piece, self.rest_of_piece(run, index, filled)):
                # code blocks one after the other have a bare blank line between them
                parts.append(self.code_lines(piece))
                piece = following
            else:
                piece = piece + gap + following
        return "".join(parts) + self.code_lines(piece + run[filled[-1] + 1:])

    def page_and_script(self, piece: list[Element], following: list[Element]) -> bool:
        """Tells whether two pieces of code differ in font, or are markup and the script or
        style that goes with it, one after the other."""
        if code_fonts(piece) != code_fonts(following):
            return True
        kinds = {confident_language(language_scores(self.code_of(lines), self.hints), self.hints)
                 for lines in (piece, following)}
        return len(kinds) == 2 and bool(kinds & MARKUP_LANGUAGES) and bool(kinds & {"javascript", "typescript", "css"})

    @staticmethod
    def rest_of_piece(run: list[Element], index: int, filled: list[int]) -> list[Element]:
        """Returns the lines of code from index to the next empty line."""
        end = next((previous + 1 for previous, line in zip(filled, filled[1:])
                    if previous >= index and line > previous + 1), filled[-1] + 1)
        return run[index:end]

    def one_piece(self, run: list[Element], following: list[Element]) -> bool:
        """Tells whether two runs of code lines are in the same font and, as far as can be
        told, the same language."""
        if code_fonts(run) != code_fonts(following):
            return False
        languages = [confident_language(language_scores(self.code_of(lines), self.hints), self.hints)
                     for lines in (run, following)]
        return not all(languages) or languages[0] == languages[1]

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

    def node(self, node: Element | str) -> str:
        if isinstance(node, str):
            return self.text(node)
        tag, style = node.tag, style_of(node)
        if tag in SKIPPED_TAGS:
            return ""
        if tag == "br":
            return BREAK
        if self.code:
            inner = self.children(node)
            if is_indent(node) or (tag in BLOCK_TAGS and INDENT_STYLE.search(style)):
                # old notes indented code with Evernote's indent, not spaces
                inner = BARE_MARKER_RUN.sub(code_newlines, inner).strip("\n")
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
            return self.bullet_list(node, tag == "ol", style)
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
                ("--en-highlight:" in style or "-evernote-highlight:true" in style
                 or HIGHLIGHT_YELLOW.search(style), "=="),
                (tag in ("u", "ins") or "underline" in style, "<u>"),
            ) if applies]
            inline = self.wrap(node, marks)
        # a block colours its own lines in block_children; an inline element here
        return self.colored(inline) if own_color else inline

    # Text and inline elements

    def text(self, value: str) -> str:
        value = CONTROL_CHARS.sub("", value).replace("\xa0", " ")
        if self.code:
            return value.replace("\r", "").replace("\n", "")
        return LOOKS_LIKE_BLOCK.sub(r"\1" + LINE_START_MARK, escape(re.sub(r"\s+", " ", value)), count=1)

    def colored(self, text: str) -> str:
        """Shows each line of text in the current colour, keeping list markers outside."""
        if not self.color or self.code:
            return text
        opening = f'<span style="color: {self.color}">'
        parts = BARE_MARKER_RUN.split(text)
        for index in range(0, len(parts), 2):  # odd parts are line markers
            lead, core, trail = split_space(parts[index])
            prefix_match = LIST_PREFIX.match(core)
            prefix = prefix_match.group(0) if prefix_match else ""
            body = core[len(prefix):]
            # a finished block (a list, a table) can't sit in a span; its lines
            # were coloured as it rendered
            if not body or "\n" in body or (body.startswith(opening) and body.endswith("</span>")):
                continue
            # Obsidian draws a highlight in the normal text colour, so the colour goes
            # inside it: around all of a highlighted piece, inside each highlight of one
            # highlighted in part
            inner = body[len(HIGHLIGHT_OPEN):-len(HIGHLIGHT_CLOSE)]
            if body.startswith(HIGHLIGHT_OPEN) and body.endswith(HIGHLIGHT_CLOSE) and HIGHLIGHT_OPEN not in inner:
                parts[index] = f"{lead}{prefix}{HIGHLIGHT_OPEN}{opening}{inner}</span>{HIGHLIGHT_CLOSE}{trail}"
                continue
            body = body.replace(HIGHLIGHT_OPEN, HIGHLIGHT_OPEN + opening).replace(
                HIGHLIGHT_CLOSE, "</span>" + HIGHLIGHT_CLOSE)
            parts[index] = f"{lead}{prefix}{opening}{body}</span>{trail}"
        return "".join(parts)

    def wrap(self, element: Element, marks: list[str]) -> str:
        """Renders an element's content inside the marks, such as ** or <u>, not yet open."""
        marks = [mark for mark in marks if mark not in self.marks]
        self.marks.update(marks)
        try:
            inner = self.children(element)
        finally:
            self.marks.difference_update(marks)
        lead, core, trail = split_space(inner)
        if not marks or not core or any(marker in inner for marker in MARKERS):
            return inner
        opening = "".join(MARK_OPEN + mark + MARK_OPEN for mark in marks)
        closing = "".join(MARK_CLOSE + mark + MARK_CLOSE
                          for mark in reversed(marks))
        return f"{lead.replace(' ', MARKED_SPACE)}{opening}{core}{closing}{trail.replace(' ', MARKED_SPACE)}"

    def link(self, element: Element) -> str:
        href = (element.attrs.get("href") or "").strip()
        label = self.children(element)
        lead, core, trail = split_space(label)
        if (not href or not core or href.startswith("#")
                or href.lower().startswith("javascript:")
                or any(marker in label for marker in MARKERS)):
            return label
        if href.lower().startswith("evernote:"):
            title = re.sub(r"\s+", " ", raw_text(element)).strip()
            # the link is written once every note is, so its label's marks are now
            label = write_marks(core.replace(LINE_START_MARK, ""))
            return lead + self.add_note_link(label, title, href) + trail
        return f"{lead}[{core}]({link_target(href)}){trail}"

    def inline_code(self, element: Element) -> str:
        text = re.sub(r"[\r\n]+", " ", raw_text(element).replace("\xa0", " "))
        if not text.strip():
            # spaces or a line break, in a monospaced font
            return self.children(element)
        ticks = "`" * (longest_run(text, "`") + 1)
        pad = " " if text.startswith("`") or text.endswith("`") else ""
        return f"{ticks}{pad}{text}{pad}{ticks}"

    # Attachments and pictures

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
        alt = escape(
            re.sub(r"\s+", " ", element.attrs.get("alt") or "").strip())
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
        except ValueError:
            return None
        if not data:
            return None
        mime = match.group(1) or "application/octet-stream"
        return self.folder.save_attachment(Resource(data, mime))

    def embed(self, attachment: Attachment) -> str:
        """Shows a picture or a PDF in the note, as Obsidian can; any other file is a block
        with its link, as Evernote shows it, or a bare link in a table cell."""
        target = local_link(attachment.path)
        if (attachment.mime.startswith("image/") or attachment.mime == "application/pdf"
                or attachment.name.lower().endswith(".pdf")):
            return f"![]({target})"
        link = f"[{escape(attachment.name)}]({target})"
        if self.cell_depth:
            return link
        # a callout, which Obsidian draws as a box; other editors show a quote
        return PARA + f"> [!attachment] {link}" + PARA

    # Code

    @contextlib.contextmanager
    def in_code(self):
        """Renders text as typed, for a code block."""
        self.code = True
        try:
            yield
        finally:
            self.code = False

    def code_block(self, element: Element, is_pre: bool, language: str | None) -> str:
        if is_pre:
            text = raw_text(element).replace("\xa0", " ").replace("\r", "")
        else:
            with self.in_code():
                text = BARE_MARKER_RUN.sub(
                    code_newlines, self.block_children(element))
        return self.fence(text, language)

    def code_lines(self, lines: list[Element]) -> str:
        """Renders monospaced lines pasted from an editor as one code block."""
        filled = [index for index, line in enumerate(lines) if code_text(line)]
        if not filled:
            # empty lines alone are just blank lines
            return "".join(map(self.node, lines))
        # empty lines before or after the code are blank lines typed around it,
        # in its font
        before, after = lines[:filled[0]], lines[filled[-1] + 1:]
        lines = lines[filled[0]:filled[-1] + 1]
        # an Evernote code block among the lines keeps the language it recorded
        recorded = [recorded_language(style) for style in map(style_of, lines)
                    if "-en-codeblock:true" in style]
        language = next(
            (value for value in recorded if value is not None), None)
        text = self.code_of(lines)
        return "".join(map(self.node, before)) + self.fence(text, language) + "".join(map(self.node, after))

    def code_of(self, lines: list[Element]) -> str:
        """Returns the text of lines of code, as typed."""
        lines = [line for line in lines if line.tag != "pre" or raw_text(line)]
        parts: list[str] = []
        with self.in_code():
            for index, line in enumerate(lines):
                # an empty line of a <pre> is a blank line, which a bare line break would lose
                text = (pre_text(line)
                        or BREAK) if line.tag == "pre" else self.node(line)
                # a <pre> holding only ..., code left out between pieces, stands apart
                omitted = line.tag == "pre" and text.strip() in ("...", "…")
                if omitted and parts:
                    parts.append(BREAK)
                parts.append(text)
                if omitted and index < len(lines) - 1:
                    parts.append(BREAK)
            return BARE_MARKER_RUN.sub(code_newlines, LINE.join(parts))

    def fence(self, text: str, language: str | None) -> str:
        """Fences a code block; with no language recorded (None), its language is decided later."""
        # code inside an indent starts at the margin, keeping its own indentation
        text = textwrap.dedent(text.strip("\n").rstrip())
        if not text.strip():
            return ""
        self.languages.append(language_scores(
            text, self.hints) if language is None else language)
        return fenced(text, f"\x07{len(self.languages) - 1}\x07")

    def fill_languages(self, body: str) -> str:
        """Names each guessed block's language, taking the note's usual language when the
        block's own code is too short to tell but fits it, and indents its lines where
        their nesting shows they've lost their indent."""
        named = [entry if isinstance(entry, str) else confident_language(entry, self.hints)
                 for entry in self.languages]
        usual = collections.Counter(
            language for language in named if language).most_common(1)

        def language(index: int) -> str:
            scores = self.languages[index]
            if named[index] or not usual or isinstance(scores, str):
                return named[index]
            best = usual[0][0]
            # the usual language may be one Evernote recorded that isn't guessed, such as ruby
            score = scores.get(best, 0)
            return best if score >= 1 and score == max(scores.values()) else ""

        def block(match: re.Match) -> str:
            start, fence, slot, text = match.groups()
            name = language(int(slot.strip("\x07")))
            # the lines of a block in a list item are indented as deep as its fence
            margin = " " * len(start)
            lines = [line[len(margin):] for line in text.split("\n")]
            if all(line.startswith(margin) or not line.strip() for line in text.split("\n")):
                text = "\n".join(
                    margin + line if line else line for line in aligned_comments(reindented(lines, name), name))
            return f"{start}{fence}{name}\n{text}\n"

        return LANGUAGE_SLOT.sub(lambda match: language(int(match.group(1))), FENCED_BLOCK.sub(block, body))

    # Headings, lists and tables

    def heading(self, element: Element, level: int) -> str:
        text = re.sub(r"\\?\n", " ", resolve(self.block_children(element)))
        return PARA + "#" * level + " " + text + PARA if text else ""

    def bullet_list(self, element: Element, ordered: bool, style: str) -> str:
        todo = "--en-todo:true" in style
        start = element.attrs.get("start") or ""
        number = int(start) if start.isdigit() else 1
        items: list[str] = []
        widths: list[int] = []
        # whether an item is a section of its own: it holds code, or spans several lines
        sections: list[bool] = []
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
                child_style = ""
                if isinstance(child, Element) and is_item:
                    item = lines_of_code(child) or child
                    child_style = style_of(item)
                    body = resolve(self.with_color(
                        item, child_style, lambda own: self.block_children(item)))
                else:
                    body = resolve(self.node(child))
                bullet = f"{number}." if ordered else "-"
                marker = bullet
                if todo and is_item:
                    marker += " [x]" if "--en-checked:true" in child_style else " [ ]"
                elif not body:
                    continue
                if CODE_FENCE_LINE.search(body):
                    body = spaced_code(body)
                sections.append(bool(CODE_FENCE_LINE.search(body))
                                or len(item_lines(body)) > 1)
                items.append(f"{marker} " + indent(body,
                             len(bullet) + 1, first=False))
                widths.append(len(bullet) + 1)
                number += 1
        finally:
            self.list_depth -= 1
        if not items:
            return ""
        # when an item is a section of its own, every item of the list is, for the list
        # to read evenly: empty lines below each, before the next, two below text, as
        # after a paragraph, one below code, which ends a block
        if any(sections):
            for index in range(len(items) - 1):
                items[index] = TRAILING_BLANK_LINES.sub("", items[index])
                count = 1 if CODE_FENCE_LINE.match(
                    items[index].rsplit("\n", 1)[-1].lstrip()) else 2
                items[index] += ("\n" + " " * widths[index] + "&nbsp;") * count
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

    def blockquote(self, element: Element) -> str:
        body = resolve(self.block_children(element))
        if not body:
            return ""
        return PARA + "\n".join("> " + line if line else ">" for line in body.split("\n")) + PARA


# Reading the HTML tree


def style_of(element: Element, spaces: bool = False) -> str:
    """Returns an element's inline style in lower case, without spaces unless asked, ready
    to match; quotes some notes store encoded, as in font-family: &apos;Courier New&apos;,
    are decoded."""
    style = html.unescape(element.attrs.get("style") or "").lower()
    return style if spaces else style.replace(" ", "")


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
        channels = [int(channel) for channel in rgb.groups()]
    elif re.fullmatch(r"#[0-9a-f]{6}", value):
        channels = [int(value[i:i + 2], 16) for i in (1, 3, 5)]
    elif re.fullmatch(r"#[0-9a-f]{3}", value):
        channels = [int(digit * 2, 16) for digit in value[1:]]
    else:
        return value  # a named colour
    # a grey, maybe slightly tinted, as text pasted from a web page often has
    return None if max(channels) - min(channels) <= GREY_SPREAD else value


def starts_code(item: Element | str) -> bool:
    return isinstance(item, Element) and is_code_line(item)


def code_run(items: list[Element | str], index: int) -> tuple[list[Element], int]:
    """Returns the lines of code from index on, and where they end."""
    run: list[Element] = []
    while index < len(items):
        item = items[index]
        if isinstance(item, Element) and is_code_line(item):
            run.append(item)
        elif not isinstance(item, str) or item.strip():
            break  # whitespace between the lines is skipped
        index += 1
    return run, index


def blank_lines(items: list[Element | str], index: int) -> tuple[list[Element], int]:
    """Returns the empty lines and dividers from index on, and where they end."""
    gap: list[Element] = []
    while index < len(items):
        item = items[index]
        if isinstance(item, Element) and (is_divider_line(item) or (
                (item.tag in BLOCK_TAGS or item.tag in ("p", "pre")) and not has_content(item.children))):
            gap.append(item)
        elif not isinstance(item, str) or item.strip():
            break
        index += 1
    return gap, index


def is_divider_line(element: Element) -> bool:
    """Tells whether an element is a divider, or a line holding nothing but one."""
    return element.tag == "hr" or (not has_content(element.children) and any(
        isinstance(kid, Element) and is_divider_line(kid) for kid in element.children))


def code_fonts(lines: list[Element]) -> set[str]:
    """Returns the monospaced fonts the text of lines of code is set in."""
    fonts: set[str] = set()

    def walk(element: Element, font: str) -> None:
        if is_monospace(element) or element.tag in CODE_TAGS:
            style = (element.attrs.get("style") or "").lower()
            match = re.search(r"font-family:([^;]+)", style)
            family = match.group(1) if match else (
                element.attrs.get("face") or element.tag)
            font = family.split(",")[0].strip(" '\"").lower()
        for kid in element.children:
            if isinstance(kid, str):
                if kid.strip() and font:
                    fonts.add(font)
            else:
                walk(kid, font)

    for line in lines:
        walk(line, "")
    return fonts


def loose_code_lined(kids: list[Element | str]) -> list[Element | str]:
    """Puts code left loose between lines, as a } after indented lines, on a line of its
    own, as a browser draws it, so that it joins the code around it."""
    if not any(starts_line(kid) for kid in kids):
        return kids
    result: list[Element | str] = []
    run: list[Element | str] = []
    for kid in kids + [Element("div", [])]:
        if not starts_line(kid):
            run.append(kid)
            continue
        line = Element("div", [])
        line.children = run
        result += [line] if is_code_line(line) and code_text(line) else run
        result.append(kid)
        run = []
    return result[:-1]


def lines_of_code(item: Element) -> Element | None:
    """Returns a list item written as lines with line breaks as one with a block per line,
    when a line is all code, so that the code becomes a code block in the item."""
    if any(starts_line(kid) for kid in item.children):
        return None
    lines = split_at_breaks(item.children)
    if not has_content(lines[-1]):
        lines.pop()  # a line break ending the item draws nothing
    blocks = []
    for line in lines:
        block = Element("div", [])
        block.children = line if has_content(line) else [Element("br", [])]
        blocks.append(block)
    if not any(is_code_line(block) and code_text(block) for block in blocks):
        return None
    lined = Element(item.tag, list(item.attrs.items()))
    lined.children = list(blocks)
    return lined


def split_at_breaks(kids: list[Element | str]) -> list[list[Element | str]]:
    """Splits inline content into its lines at each <br>, however deep in formatting,
    each line keeping the formatting around its part."""
    lines: list[list[Element | str]] = [[]]
    for kid in kids:
        if isinstance(kid, str):
            lines[-1].append(kid)
        elif kid.tag == "br":
            lines.append([])
        else:
            for index, part in enumerate(split_at_breaks(kid.children)):
                if index:
                    lines.append([])
                piece = Element(kid.tag, list(kid.attrs.items()))
                piece.children = part
                lines[-1].append(piece)
    return lines


def item_lines(body: str) -> list[str]:
    """Returns the lines of text a list item's own text spans, up to a list nested in it;
    a picture or an attached file, spaced by rules of its own, isn't one."""
    lines = []
    for line in body.split("\n"):
        if NESTED_ITEM.match(line):
            break
        if line.strip() and line.strip() != BLANK_LINE and not line.lstrip().startswith(("![", "> [!")):
            lines.append(line)
    return lines


def spaced_code(body: str) -> str:
    """Sets one empty line between the code blocks of a list item and its text, in place
    of the blank lines around them, and none at its end, where the list sets it."""
    segments: list[tuple[bool, list[str]]] = []
    text: list[str] = []
    lines = body.split("\n")
    index = 0
    while index < len(lines):
        fence = CODE_FENCE_LINE.match(lines[index])
        if not fence:
            text.append(lines[index])
            index += 1
            continue
        end = index + 1
        while end < len(lines) and lines[end] != fence.group(0):
            end += 1
        segments += [(False, text), (True, lines[index:end + 1])]
        text, index = [], end + 1
    segments.append((False, text))
    result: list[str] = []
    for number, (code, part) in enumerate(segments):
        if not code:
            blank = ("", "&nbsp;")
            while number and part and part[0].strip() in blank:
                part = part[1:]
            while part and part[-1].strip() in blank:
                part = part[:-1]
            if not part:
                continue
            part = (["&nbsp;"] if number else []) + part + \
                (["&nbsp;"] if number < len(segments) - 1 else [])
        result += part
    return "\n".join(result)


def unwrapped(kids: list[Element | str]) -> list[Element | str]:
    """Replaces each plain line that only wraps other lines with what it wraps, so that the
    lines inside are neighbours of the ones around it, as a browser shows them."""
    result: list[Element | str] = []
    for kid in kids:
        if not (isinstance(kid, Element) and wraps_lines(kid)):
            result.append(kid)
            continue
        # text and formatting the wrapper holds between its lines are lines of their own
        line: list[Element | str] = []
        for inner in unwrapped(kid.children) + [Element("div", [])]:
            if not starts_line(inner):
                line.append(inner)
                continue
            if any(not isinstance(piece, str) or piece.strip() for piece in line):
                wrapped = Element("div", [])
                wrapped.children = line
                result.append(wrapped)
            line = []
            result.append(inner)
        result.pop()
    return result


def wraps_lines(element: Element) -> bool:
    """Tells whether an element is a plain <div> holding lines of its own, with no colour,
    indent, font or other formatting that its lines would lose."""
    if element.tag != "div" or not any(starts_line(kid) for kid in element.children):
        return False
    style = style_of(element)
    return not (text_color(element, style) or INDENT_STYLE.search(style) or "font" in style
                or "-en-codeblock" in style or "highlight" in style or "decoration" in style)


def next_kid(kids: list[Element | str], start: int) -> Element | str | None:
    """Returns the first of the kids from start on that isn't blank text."""
    return next((kid for kid in kids[start:] if not isinstance(kid, str) or kid.strip()), None)


def drop_trailing_break(element: Element) -> None:
    """Removes the <br> an inline element ends with, however deep in its formatting."""
    for index in range(len(element.children) - 1, -1, -1):
        kid = element.children[index]
        if isinstance(kid, str):
            if kid.strip():
                return
        elif kid.tag == "br":
            del element.children[index]
            return
        else:
            drop_trailing_break(kid)
            return


def has_content(kids: list[Element | str]) -> bool:
    """Tells whether a line's pieces hold text or a picture, not only line breaks."""
    return any(kid.strip() if isinstance(kid, str)
               else kid.tag in ("en-media", "img") or has_content(kid.children) for kid in kids)


def starts_line(kid: Element | str | None) -> bool:
    """Tells whether a block's child is a block, which a browser starts on a line of its own."""
    return isinstance(kid, Element) and (
        kid.tag in BLOCK_TAGS or kid.tag in HEADING_TAGS
        or kid.tag in ("p", "blockquote", "ul", "ol", "table", "hr", "pre"))


def is_monospace(element: Element) -> bool:
    """Tells whether an element sets a monospaced font, as code pasted from an editor has."""
    match = re.search(r"font-family:([^;]+)", style_of(element, spaces=True))
    family = match.group(1) if match else (
        element.attrs.get("face") or "").lower()
    first = family.split(",")[0].strip(" '\"")
    return bool(first) and bool(MONOSPACE_FONT.search(first))


def is_indent(element: Element) -> bool:
    """Tells whether a blockquote is older Evernote's indent: drawn with no quote bar, or
    around code, which it indented with one."""
    return element.tag == "blockquote" and ("border:none" in style_of(element) or holds_code(element))


def holds_code(element: Element) -> bool:
    """Tells whether an element holds code: text in a monospaced font, or a code block."""
    return any(isinstance(kid, Element) and (
        is_monospace(kid) or kid.tag in CODE_TAGS or "-en-codeblock" in style_of(kid) or holds_code(kid))
        for kid in element.children)


def is_code_line(element: Element) -> bool:
    """Tells whether a block holds only monospaced text: a line of pasted code, or an empty one."""
    if element.tag == "pre":
        # code as typed; an empty one shows nothing
        return bool(raw_text(element))
    if element.tag not in BLOCK_TAGS and element.tag != "p" and not is_indent(element):
        return False
    if element.tag in ("li", "td", "th", "tr", "thead", "tbody", "tfoot", "dl", "en-note"):
        return False
    return monospace_only(element, False) == "mono" or glued_code(element)


def glued_code(element: Element) -> bool:
    """Tells whether a line is code with words in another font glued to it, as css in
    cssSource, typed in later in front of a name: text with code in it has spaces around
    the code."""
    pieces = [(text, mono)
              for text, mono in font_pieces(element, False) if text]
    if not any(mono and text.strip() for text, mono in pieces):
        return False
    for index, (text, mono) in enumerate(pieces):
        if mono or not text.strip():
            continue
        if not re.fullmatch(r"\s*\w+\s*", text):
            return False
        before = pieces[index - 1] if index else ("", False)
        after = pieces[index + 1] if index + 1 < len(pieces) else ("", False)
        glued_before = not text[0].isspace(
        ) and before[1] and re.search(r"\w$", before[0])
        glued_after = not text[-1].isspace() and after[1] and re.match(r"\w", after[0])
        if not (glued_before or glued_after):
            return False
    return True


def font_pieces(element: Element, mono: bool) -> list[tuple[str, bool]]:
    """Returns an element's text, piece by piece, each telling whether it's monospaced;
    a picture, list or other block that can't be code is a piece of text."""
    mono = mono or is_monospace(element) or element.tag in CODE_TAGS
    pieces: list[tuple[str, bool]] = []
    for child in element.children:
        if isinstance(child, str):
            pieces.append((child, mono))
        elif child.tag in ("en-media", "img", "table", "ul", "ol", "hr", "en-todo"):
            pieces.append(("\0", False))
        elif child.tag not in SKIPPED_TAGS:
            # a line of its own isn't glued to the text around it
            edge = [("\n", False)] if starts_line(child) else []
            pieces += edge + font_pieces(child, mono) + edge
    return pieces


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


def pre_text(element: Element) -> str:
    """Returns the text of a <pre> as typed, without the line break it may end with, which
    a browser doesn't show."""
    text = raw_text(element).replace("\xa0", " ").replace("\r", "")
    return text[:-1] if text.endswith("\n") else text


def table_rows(table: Element):
    for child in table.children:
        if isinstance(child, Element):
            if child.tag == "tr":
                yield child
            elif child.tag in ("thead", "tbody", "tfoot"):
                yield from (row for row in child.children
                            if isinstance(row, Element) and row.tag == "tr")


# Writing Markdown

def resolve(text: str, paragraphs: bool = False) -> str:
    """Turns the line and paragraph placeholders of a finished block into newlines; with
    paragraphs, each line of the block is a paragraph of its own."""
    text = ADJACENT_IMAGES.sub(
        r"\1" + LINE, text)  # each picture on its own line
    return MARKER_RUN.sub(lambda match: newlines(match, paragraphs), text).strip()


def newlines(match: re.Match, paragraphs: bool = False) -> str:
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
    # a PDF, which Obsidian shows in a viewer of its own, is spaced as text is
    after_pdf = PDF_LINE_END.search(text, line_start, match.start())
    after_image = not after_pdf and IMAGE_LINE_END.search(
        text, line_start, match.start())
    if after_image and blank:
        blank = 1  # one blank line under a picture, however many were typed
    # a line after a list item, such as a to-do line, would join the item
    after_item = LIST_ITEM.match(text, line_start)
    if not blank and paragraphs and is_link_line(text[line_start:match.start()]) and is_link_line(
            text[match.end():LINE_END.search(text, match.end()).end() - 1]):  # type: ignore[union-attr]
        return HARD_BREAK  # a list of links sits tight, as a list does
    if not blank:
        # each line is a paragraph, and so is a picture in a list item, which
        # Evernote draws on a line of its own with the gap of one
        if block_gap or (after_item and not before_block) or (LINE in run and not before_block and (
                paragraphs or text.startswith("![", match.end()))):
            return "\n\n"
        return "\n" if before_block else HARD_BREAK
    # the line above, without the spaces a line of Evernote's text may start
    # with, which the newlines replace; a line of a list already written keeps them
    line = text[line_start:match.start()]
    if line_start and text[line_start - 1] in MARKERS:
        line = line.lstrip(" \t")
    after_block = BLOCK_LINE.match(line)
    if before_block and after_image:
        # under a picture before a block, an extra blank line in the picture's
        # section gives the gap after a list
        return (HARD_BREAK + BLANK_LINE) * (blank + 1) + "\n\n"
    if paragraphs and not after_pdf and not after_block and not CODE_FENCE.match(line):
        # after a line of text or a picture, whatever follows, each typed blank
        # line is two, for the new section to stand out, as Obsidian's gap
        # between paragraphs is nearly a line already
        blank *= 2
    if before_block:
        if not after_block:
            # after a line of text the blank lines are its next lines, so they
            # show as typed, not with a paragraph's spacing around each
            return (HARD_BREAK + BLANK_LINE) * blank + "\n\n"
        return "\n\n" + (BLANK_LINE + "\n\n") * blank
    if block_gap or after_item:
        return "\n\n" + (BLANK_LINE + HARD_BREAK) * blank
    if paragraphs:
        # the blank lines end the paragraph above, so a new section starts
        # after their space and a paragraph's
        return (HARD_BREAK + BLANK_LINE) * blank + "\n\n"
    return HARD_BREAK + (BLANK_LINE + HARD_BREAK) * blank


def is_link_line(line: str) -> bool:
    """Tells whether a line holds nothing but a link, however formatted."""
    return bool(LINK_LINE.fullmatch(MARK_TOKEN.sub("", line).replace(LINE_START_MARK, "")))


def code_newlines(match: re.Match) -> str:
    return "\n" * newline_count(match.group(0))


def newline_count(run: str) -> int:
    block = 2 if PARA in run else 1 if LINE in run else 0
    return max(block, run.count(BREAK) + (1 if block else 0))


def split_space(text: str) -> tuple[str, str, str]:
    """Splits text into its leading space, its content and its trailing space."""
    return text[:len(text) - len(text.lstrip())], text.strip(), text[len(text.rstrip()):]


def escape(text: str) -> str:
    """Escapes the characters Markdown would read as formatting."""
    return MARKDOWN_SPECIAL.sub(r"\\\1", text)


def indent(text: str, width: int, first: bool = True) -> str:
    lines = text.split("\n")
    pad = " " * width
    indented = [pad + line if line else "" for line in lines[1:]]
    return "\n".join([(pad if first and lines[0] else "") + lines[0]] + indented)


def table_row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def fenced(text: str, language: str) -> str:
    """Wraps code in a fence longer than any run of backticks it holds, naming its language."""
    fence = "`" * max(3, longest_run(text, "`") + 1)
    return PARA + f"{fence}{language}\n{text}\n{fence}" + PARA


def reindented(lines: list[str], language: str) -> list[str]:
    """Indents lines of code one level deeper than the line that opens their block, wherever
    they aren't, in code that has lost its indent: inside braces in a language that uses
    them, or inside a tag in markup. Every other line keeps its indent, as typed, and so
    does code that hasn't lost it, whatever its style."""
    nesting = BRACE_NESTING if language in BRACE_LANGUAGES else TAG_NESTING if language in MARKUP_LANGUAGES else None
    if not nesting or not lost_indent(lines, language, nesting):
        return lines
    widths = {indent_width(line) for line in lines if line.strip()}
    unit = 2 if min(widths - {0}, default=4) == 2 else 4
    # the open blocks: the name that closes each, its line's indent, and how far that moved
    stack: list[tuple[str, int, int]] = []
    state = {"comment": False}
    result = []
    for line in lines:
        if not line.strip():
            result.append(line)
            continue
        own = indent_width(line)
        tokens = nesting(line.strip(), language, state)
        closing = 0  # the blocks the line's start closes
        while closing < len(tokens) and tokens[closing][0] == "lead":
            closing += 1
        opener = None
        for _, name in tokens[:closing]:
            opener = close_block(stack, name) or opener
        if opener and not line.strip().startswith("#"):
            width = opener[1]
        else:
            parent = stack[-1] if stack else None
            moved = parent[2] if parent else 0
            width = max(own + moved, 0)
            if parent and width <= parent[1] and not at_parent_depth(line, parent):
                width = parent[1] + unit
            elif parent and width < parent[1] and line.lstrip().startswith("{"):
                width = parent[1]
        if width != own:
            line = " " * width + line.lstrip()
        for kind, name in tokens[closing:]:
            if kind == "open":
                stack.append((name, width, width - own))
            else:
                close_block(stack, name)
        result.append(line)
    return result


def aligned_comments(lines: list[str], language: str) -> list[str]:
    """Lines up the lines of a comment in markup that spans several, and its closing -->,
    with the <!-- that opens it, keeping how its lines are indented among themselves."""
    if language not in MARKUP_LANGUAGES:
        return lines
    result = list(lines)
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.lstrip().startswith("<!--") or "-->" in line:
            index += 1
            continue
        end = next((later for later in range(index + 1, len(lines))
                   if "-->" in lines[later]), None)
        if end is None:
            break
        margin = line[:len(line) - len(line.lstrip())]
        inside = lines[index + 1:end + 1]
        # the text's own indent, wherever a --> on a line of its own sits
        text = [piece for piece in inside if piece.strip()
                and piece.strip() != "-->"]
        depth = min(map(indent_width, text), default=0)
        result[index + 1:end + 1] = [
            margin + "-->" if piece.strip() == "-->"
            else margin + " " * (indent_width(piece) - depth) + piece.lstrip() if piece.strip() else piece
            for piece in inside]
        index = end + 1
    return result


def lost_indent(lines: list[str], language: str, nesting) -> bool:
    """Tells whether at least half the lines inside blocks are no deeper than the line that
    opens theirs: some styles leave a few at its depth, as <body> under <html>, or { under
    a call it passes a function to, but not most."""
    stack: list[tuple[str, int, int]] = []
    state = {"comment": False}
    nested = shallow = 0
    for line in lines:
        if not line.strip():
            continue
        own = indent_width(line)
        tokens = nesting(line.strip(), language, state)
        closing = [name for kind, name in tokens if kind == "lead"]
        for name in closing:
            close_block(stack, name)
        if stack and not closing and not at_parent_depth(line, stack[-1]):
            nested += 1
            shallow += own <= stack[-1][1]
        for kind, name in tokens[len(closing):]:
            if kind == "open":
                stack.append((name, own, 0))
            else:
                close_block(stack, name)
    return nested > 0 and shallow * 2 >= nested


def at_parent_depth(line: str, parent: tuple[str, int, int]) -> bool:
    """Tells whether a line may sit as deep as the line opening its block: a preprocessor
    line, as #region, a brace opening the body of a function passed to a call, or <head>
    and <body> in <html>."""
    line = line.strip()
    return line.startswith("#") or (line.startswith("{") and parent[0] == ")") or parent[0] == "html"


def close_block(stack: list[tuple[str, int, int]], name: str) -> tuple[str, int, int] | None:
    """Closes the innermost open block a name closes, with the ones left open inside it."""
    for index in range(len(stack) - 1, -1, -1):
        if stack[index][0] == name:
            opener = stack[index]
            del stack[index:]
            return opener
    return None


def indent_width(line: str) -> int:
    indent = line[:len(line) - len(line.lstrip())]
    return len(indent.expandtabs(4))


def brace_nesting(line: str, language: str, state: dict[str, bool]) -> list[tuple[str, str]]:
    """Returns the brackets a line of code opens and closes, outside strings and comments."""
    tokens: list[tuple[str, str]] = []
    index = 0
    while index < len(line):
        if state["comment"]:
            end = line.find("*/", index)
            if end < 0:
                break
            state["comment"], index = False, end + 2
            continue
        char = line[index]
        if line.startswith("/*", index):
            state["comment"], index = True, index + 2
            continue
        if line.startswith("//", index) and language != "css":
            break
        if char in "\"'`":
            end = index + 1
            while end < len(line) and line[end] != char:
                end += 2 if line[end] == "\\" else 1
            # a quote with no closing one on its line, as after a typographic one, is
            # just a character
            index = end + 1 if end < len(line) else index + 1
            continue
        if char in "{([":
            tokens.append(("open", BRACKETS[char]))
        elif char in "})]":
            # a closing bracket the line starts with closes before the line starts
            leading = not line[:index].strip(" \t})]")
            tokens.append(("lead" if leading else "close", char))
        index += 1
    return tokens


def tag_nesting(line: str, language: str, state: dict[str, bool]) -> list[tuple[str, str]]:
    """Returns the tags a line of markup opens and closes, outside comments."""
    if state["comment"]:
        end = line.find("-->")
        if end < 0:
            return []
        state["comment"], line = False, line[end + 3:]
    if "<!--" in line and "-->" not in line[line.rfind("<!--"):]:
        state["comment"] = True
    line = re.sub(r"<!--.*?(-->|$)", "", line)
    tokens = []
    for match in MARKUP_TAG.finditer(line):
        slash, name, end = match.groups()
        if end or name.lower() in VOID_TAGS:
            continue
        # a closing tag the line starts with closes before the line starts
        leading = slash and not MARKUP_TAG.sub(
            "", line[:match.start()]).strip()
        tokens.append(
            ("lead" if leading else "close" if slash else "open", name.lower()))
    if language == "html":
        # Razor's @{ … } and the C# inside it, among a page's tags
        text = MARKUP_TAG.sub(lambda match: " " * len(match.group(0)), line)
        braces = brace_nesting(text, language, {"comment": False})
        leading = [token for token in braces if token[0] == "lead"]
        tokens = leading + tokens + braces[len(leading):]
    return tokens


BRACE_NESTING, TAG_NESTING = brace_nesting, tag_nesting


def longest_run(text: str, char: str) -> int:
    return max((len(run) for run in re.findall(re.escape(char) + "+", text)), default=0)


def join_touching_code(text: str) -> str:
    """Joins pieces of inline code that touch, as pieces from different nesting levels can:
    `a``b` would read as one broken piece."""
    return TOUCHING_CODE.sub(lambda match: "`" + match.group(0)[1:-1].replace("``", "") + "`", text)


def escape_line_starts(text: str) -> str:
    """Keeps text that starts a line but looks like a list, heading or quote as text,
    with a backslash: 1\\) as typed is not a numbered list."""
    return MARKED_LINE_START.sub(r"\1\\", text).replace(LINE_START_MARK, "")


def write_marks(text: str) -> str:
    """Writes the formatting marks' tokens as Markdown, first joining pieces with the
    same mark that touch, such as two highlighted spans side by side, or a highlighted
    sentence and a word in it in another colour."""
    text = UNDERLINED_SPAN.sub("\\1\x0e<u>\x0e\\2\x0f<u>\x0f</span>", text)
    joined = 1
    while joined:  # once the innermost marks join, the ones around them touch
        text = SPAN_MARKED.sub(
            lambda match: mark_around_span(match, text), text)
        text, joined = TOUCHING_MARKS.subn(r"\2", text)
    text = DOUBLE_SPAN.sub(r"\1\2</span>", text)
    return MARK_TOKEN.sub(lambda match: closing_mark(match.group(2))
                          if match.group(1) == MARK_CLOSE else match.group(2), text).replace(MARKED_SPACE, " ")


def mark_around_span(match: re.Match, text: str) -> str:
    """Moves the mark a colour span holds in full outside it, when the same mark touches
    the span, so that the pieces join; leaves it inside otherwise."""
    span, mark, inner = match.groups()
    if mark == "<u>":
        # an underline stays inside the colour, to be drawn in it
        return match.group(0)
    token = MARK_OPEN + mark + MARK_OPEN, MARK_CLOSE + mark + MARK_CLOSE
    before = text[:match.start()].rstrip(MARKED_SPACE)
    after = text[match.end():].lstrip(MARKED_SPACE)
    if not (before.endswith(token[1]) or after.startswith(token[0])):
        return match.group(0)
    return f"{token[0]}{span}{inner}</span>{token[1]}"


def closing_mark(mark: str) -> str:
    return "</" + mark[1:] if mark.startswith("<") else mark


def link_target(url: str) -> str:
    return URL_SPECIAL.sub(lambda match: quote(match.group(0)), url)


def local_link(path: str) -> str:
    return PATH_SPECIAL.sub(lambda match: quote(match.group(0)), path.replace(os.sep, "/"))


# Code languages

def recorded_language(style: str) -> str | None:
    """Returns the language of an Evernote code block, "" for plain text, None if it has none."""
    match = re.search(r"--en-syntaxlanguage:([\w#+-]+)", style)
    if not match:
        return None
    language = match.group(1)
    return "" if language in ("plaintext", "text", "plain") else EVERNOTE_LANGUAGES.get(language, language)


def title_languages(title: str) -> tuple[str, ...]:
    """Returns the languages a note's title or tags name, such as css for "Gradients CSS3"."""
    return tuple(language for language, pattern in TITLE_LANGUAGES.items() if pattern.search(title))


def language_scores(code: str, hints: tuple[str, ...] = ()) -> dict[str, int]:
    """Scores each language by the patterns of it the code has, and a point if its note names it.

    Some code is a kind outright: markup, as CSS or script inside a page is
    still the page; JSON, even a fragment of an object; a terminal session;
    YAML; HTML attributes; a Markdown badge.
    """
    scores = dict.fromkeys(LANGUAGE_PATTERNS, 0)
    kind = outright_language(code)
    if kind == "html" and "xml" in hints and HTML_ATTRIBUTE.match(code.strip()):
        kind = "xml"  # attributes in a note about XML, as XSLT's select="…"
    if kind:
        return {**scores, kind: 10}
    lines = [line for line in code.strip().split("\n")
             if line.strip() and not line.lstrip().startswith("#")]
    for language, patterns in LANGUAGE_PATTERNS.items():
        scores[language] = sum(
            weight for pattern, weight in patterns if pattern.search(code))
    if lines and all(re.match(SHELL_COMMAND, line) for line in lines):
        # nothing but commands, such as one "brew install"
        scores["bash"] += STRONG
    for language in hints:
        # a note about HTML may hold code that only handles it, as C# decoding "<p>"
        if language in MARKUP_LANGUAGES and not has_markup(code):
            continue
        scores[language] += 1
    return scores


def has_markup(code: str) -> bool:
    """Tells whether code holds tags outside its quoted strings, or a tag's attributes,
    as XSLT's select="…" on its own."""
    bare = re.sub(r'"[^"\n]*"|\'[^\'\n]*\'', "", code)
    return bool(re.search(r"<[A-Za-z!?/]", bare) or re.search(r"(^|\s)[\w:.-]+=(?=\s|/?>|$)", bare, re.M))


def outright_language(code: str) -> str:
    """Names the language of code that shows it outright, or "" when patterns must tell."""
    stripped = code.strip()
    lines = [line for line in stripped.split("\n")
             if line.strip() and not line.lstrip().startswith("#") and line.strip() != "..."]
    # a fragment of an object is JSON too
    for text in (stripped, "{" + stripped + "}"):
        if text[:1] in "{[" and (text == stripped or JSON_MEMBER.match(stripped)):
            try:
                json.loads(text)
                return "json"
            except ValueError:
                pass
    if JSON_MEMBER.match(stripped) and stripped.endswith(("}", "]")):
        return "json"
    if MARKUP_START.match(stripped) or ASPNET_BLOCK.search(code):
        # ASP.NET's own markup is XML; a page with HTML's tags is HTML, EJS too
        return "xml" if XML_ONLY.search(code) or not HTML_TAG.search(code) else "html"
    if lines and all(HTML_ATTRIBUTE.fullmatch(line.strip()) for line in lines):
        return "html"  # attributes of a tag, as Angular's ng-class
    if SHELL_PROMPT.match(stripped):
        return "bash"
    yaml_text = re.sub(r"\$\{\{.*?\}\}", "", code)  # GitHub's expressions
    if len(lines) >= 2 and not re.search(r"[;{}]", yaml_text) and all(map(YAML_LINE.match, lines)):
        return "yaml"  # keys and list items only, such as a docker-compose file
    if MARKDOWN_BADGE.match(stripped):
        return "markdown"
    return ""


def confident_language(scores: dict[str, int], hints: tuple[str, ...] = ()) -> str:
    """Names the best-scoring language when it has two points and a clear lead, since a
    wrong language colours code worse than none; otherwise ""; and, when the code
    shows no other language, the one language the note's title and tags name."""
    ranked = sorted(scores.items(), key=lambda item: -item[1])
    (best, top), (_, second) = ranked[0], ranked[1]
    if top >= 2 and top > second:
        return best
    # a hint the code doesn't bear out, as HTML for code with no markup, scores nothing
    hints = tuple(language for language in hints if scores.get(language))
    shown = {language for language,
             score in scores.items() if score and language not in hints}
    return hints[0] if len(hints) == 1 and not shown else ""


# Command line

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


if __name__ == "__main__":
    main()
