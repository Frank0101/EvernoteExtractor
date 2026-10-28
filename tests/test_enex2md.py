#!/usr/bin/env python3
"""Tests for src/enex2md.py: run with `python3 tests/test_enex2md.py` (add -v to list each case).

Most tests build a small .enex file, run the script on it as a user would and
read the Markdown it writes, so they check the output itself. Cases that
check the same thing are tables run as subtests. All data is made up.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))), "src"))
import enex2md  # noqa: E402

RED = "rgb(252, 18, 51)"
RED_SPAN = '<span style="color: rgb(252,18,51)">'
YELLOW_SPAN = '<span style="color: rgb(229,158,37)">'
INDENT = 'style="padding-left:40px;"'
OLD_INDENT = 'style="margin: 0px 0px 0px 40px; border: none; padding: 0px;"'


def resource(data: bytes, mime: str, file_name: str | None = None) -> dict:
    return {"data": data, "mime": mime, "file_name": file_name}


def media(res: dict) -> str:
    """The <en-media> tag that shows a resource in a note's body."""
    return f'<en-media hash="{hashlib.md5(res["data"]).hexdigest()}" type="{res["mime"]}"/>'


PNG = resource(b"fake png one", "image/png", "one.png")
PNG2 = resource(b"fake png two", "image/png", "two.png")
PDF = resource(b"%PDF-1.4 fake", "application/pdf", "Report.pdf")
XLSX = resource(b"fake sheet", "application/vnd.ms-excel", "data.xlsx")
RESOURCES = (PNG, PNG2, PDF, XLSX)


def note_xml(title: str, content: str, created: str | None = "20240115T093000Z",
             updated: str | None = "20240116T101500Z", tags: tuple[str, ...] = (),
             attributes: dict[str, str] | None = None, resources: tuple[dict, ...] = ()) -> str:
    parts = [f"<note><title>{title}</title>",
             f'<content><![CDATA[<?xml version="1.0" encoding="UTF-8"?><en-note>{content}</en-note>]]></content>']
    if created:
        parts.append(f"<created>{created}</created>")
    if updated:
        parts.append(f"<updated>{updated}</updated>")
    parts += [f"<tag>{tag}</tag>" for tag in tags]
    if attributes:
        parts.append("<note-attributes>" + "".join(
            f"<{key}>{value}</{key}>" for key, value in attributes.items()) + "</note-attributes>")
    for res in resources:
        name = f"<resource-attributes><file-name>{res['file_name']}</file-name></resource-attributes>" \
            if res["file_name"] else ""
        parts.append(f'<resource><data encoding="base64">{base64.b64encode(res["data"]).decode()}</data>'
                     f"<mime>{res['mime']}</mime>{name}</resource>")
    return "".join(parts) + "</note>"


def write_enex(path: str, *notes: str) -> str:
    with open(path, "w", encoding="utf-8") as file:
        file.write('<?xml version="1.0" encoding="UTF-8"?>\n<en-export>' +
                   "".join(notes) + "</en-export>")
    return path


def run(*args: str) -> None:
    """Runs the script's command line, hiding its progress messages."""
    with contextlib.redirect_stderr(io.StringIO()):
        enex2md.main(list(args))


def read_folder(folder: str) -> dict[str, str]:
    """Returns every note in an output folder by its path relative to the folder."""
    notes = {}
    for root, _, files in os.walk(folder):
        for name in files:
            if name.endswith(".md") and os.path.basename(root) != enex2md.ATTACHMENTS_DIR:
                path = os.path.join(root, name)
                with open(path, encoding="utf-8") as file:
                    notes[os.path.relpath(path, folder)] = file.read()
    return notes


def body_of(text: str) -> str:
    """Returns a note's text after its front matter."""
    return text.split("\n---\n", 1)[1].rstrip("\n")


class ExportTest(unittest.TestCase):
    """Runs exports in a temporary folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    def export(self, *notes: str, name: str = "Notebook") -> dict[str, str]:
        enex = write_enex(os.path.join(self.dir, f"{name}.enex"), *notes)
        run(enex)
        return read_folder(os.path.join(self.dir, name))

    def body(self, content: str, title: str = "Note", resources: tuple[dict, ...] = ()) -> str:
        notes = self.export(note_xml(title, content, resources=resources))
        return body_of(notes[f"{title}.md"])

    def check_bodies(self, cases: list[tuple[str, str, str]]):
        """Checks a table of (case, note content, expected body); a note holds the resources its content shows."""
        for case, content, expected in cases:
            with self.subTest(case):
                self.setUp()
                shown = tuple(
                    res for res in RESOURCES if media(res) in content)
                self.assertEqual(self.body(content, resources=shown), expected)


class TestLinesAndBlankLines(ExportTest):
    """One <div> per line; a typed blank line becomes a visible &nbsp; line."""

    def test_lines_and_blank_lines(self):
        self.check_bodies([
            ("lines follow each other", "<div>a</div><div>b</div>", "a\nb"),
            ("typed blank line", "<div>a</div><div><br/></div><div>b</div>", "a\n&nbsp;\nb"),
            ("blank line in nested spans", "<div>a</div><div><span><span><br/></span></span></div><div>b</div>",
             "a\n&nbsp;\nb"),
            ("two line breaks inside a line",
             "<div>a<br/><br/>b</div>", "a\n&nbsp;\nb"),
            ("a <br> ending a line draws nothing",
             "<div>a<br/></div><div>b</div>", "a\nb"),
            ("no backslash line breaks",
             "<div>a</div><div>b</div><div>c</div>", "a\nb\nc"),
            ("blank line after a list",
             "<ul><li>x</li></ul><div><br/></div><div>b</div>", "- x\n\n&nbsp;\nb"),
            ("text after a list starts a paragraph",
             "<ul><li>x</li></ul><div>b</div>", "- x\n\nb"),
            ("text before a list", "<div>a</div><ul><li>x</li></ul>", "a\n\n- x"),
            ("text after a to-do starts a paragraph",
             '<div><en-todo checked="true"/>a</div><div><en-todo/>b</div><div>c</div>', "- [x] a\n- [ ] b\n\nc"),
            ("paragraphs", "<p>a</p><p>b</p>", "a\n\nb"),
            ("blank lines at the start and end are dropped",
             "<div><br/></div><div>a</div><div><br/></div>", "a"),
        ])


class TestDividers(ExportTest):
    """A divider gets exactly one plain blank line above and below, whatever was typed."""

    def test_dividers(self):
        self.check_bodies([
            ("no blank lines typed", "<div>a</div><hr/><div>b</div>", "a\n\n---\n\nb"),
            ("several blank lines typed",
             "<div>a</div><div><br/></div><div><br/></div><hr/><div><br/></div><div><br/></div><div>b</div>",
             "a\n\n---\n\nb"),
            ("after a list", "<ul><li>x</li></ul><div><br/></div><hr/><div>b</div>",
             "- x\n\n---\n\nb"),
            ("two dividers", "<div>a</div><hr/><div><br/></div><hr/><div>b</div>",
             "a\n\n---\n\n---\n\nb"),
            ("after a picture", f"{media(PNG)}<div><br/></div><hr/><div>b</div>",
             "![](attachments/one.png)\n\n---\n\nb"),
        ])


class TestPictures(ExportTest):
    """A picture gets exactly one blank line under it; PDFs show inline, other files are links."""

    def test_blank_line_under_pictures(self):
        image, image2 = "![](attachments/one.png)", "![](attachments/two.png)"
        self.check_bodies([
            ("no blank line typed",
             f"<div>a</div>{media(PNG)}<div>b</div>", f"a\n{image}\n&nbsp;\nb"),
            ("two blank lines typed", f"<div>{media(PNG)}</div><div><br/></div><div><br/></div><div>b</div>",
             f"{image}\n&nbsp;\nb"),
            ("side by side pictures each get a line", f"{media(PNG)}{media(PNG2)}<div>b</div>",
             f"{image}\n&nbsp;\n{image2}\n&nbsp;\nb"),
            ("before a list",
             f"{media(PNG)}<ul><li>x</li></ul>", f"{image}\n\n&nbsp;\n\n- x"),
            ("inside a list item", f"<ul><li><div>a</div>{media(PNG)}</li><li>b</li></ul>",
             f"- a\n  {image}\n  &nbsp;\n- b"),
            ("last in the note", f"<div>a</div>{media(PNG)}", f"a\n{image}"),
        ])

    def test_attachment_embeds(self):
        self.check_bodies([
            ("picture", media(PNG), "![](attachments/one.png)"),
            ("PDF shown inline", media(PDF), "![](attachments/Report.pdf)"),
            ("other file is a link", media(XLSX),
             "[data.xlsx](attachments/data.xlsx)"),
        ])

    def test_attachment_not_shown_in_the_text_is_added_at_the_end(self):
        self.assertEqual(self.body("<div>a</div>", resources=(XLSX,)),
                         "a\n\n[data.xlsx](attachments/data.xlsx)")


class TestIndentation(ExportTest):
    """Indented lines lose their indent and get a blank line after them; old indents aren't quotes."""

    def test_indentation(self):
        self.check_bodies([
            ("indented lines end with a blank line",
             f"<div>Task</div><div {INDENT}>x</div><div {INDENT}>y</div><div>Action</div>",
             "Task\nx\ny\n&nbsp;\nAction"),
            ("a typed blank line isn't doubled",
             f"<div>Task</div><div {INDENT}>x</div><div><br/></div><div>Next</div>", "Task\nx\n&nbsp;\nNext"),
            ("deeper indent stays in the same run",
             f'<div {INDENT}>x</div><div style="padding-left:80px;">y</div><div>z</div>', "x\ny\n&nbsp;\nz"),
            ("old Evernote indent isn't a quote", f"<blockquote {OLD_INDENT}><div>x</div></blockquote><div>y</div>",
             "x\n&nbsp;\ny"),
            ("a real quote", "<blockquote><div>q1</div><div>q2</div></blockquote>", "> q1\n> q2"),
        ])


class TestFormatting(ExportTest):
    """Bold, italic, strike, underline, highlights, headings, lists and escaping."""

    def test_formatting(self):
        self.check_bodies([
            ("marks", "<b>b</b> <i>i</i> <s>s</s> <u>u</u>",
             "**b** *i* ~~s~~ <u>u</u>"),
            ("styles", '<span style="font-weight: bold;">b</span> <span style="font-style: italic;">i</span> '
                       '<span style="text-decoration: underline;">u</span>', "**b** *i* <u>u</u>"),
            ("highlight", '<span style="--en-highlight:yellow;background-color: #ffef9e;">h</span>', "==h=="),
            ("old highlight", '<span style="background-color:rgb(255, 250, 165);-evernote-highlight:true;">h</span>',
             "==h=="),
            ("bold, highlight and colour together",
             f'<span style="color:{RED};font-weight:bold;--en-highlight:yellow;">x</span>', f"{RED_SPAN}**==x==**</span>"),
            ("nested bold isn't doubled", "<b>a <b>b</b></b>", "**a b**"),
            ("heading", "<h2>Title <i>two</i></h2>", "## Title *two*"),
            ("escaping", "<div>5 * 3 [x] &lt;tag&gt; a_b _c_ \\</div>",
             "5 \\* 3 \\[x\\] \\<tag> a_b \\_c\\_ \\\\"),
            ("link", '<a href="https://example.com/a (b)">site</a>',
             "[site](https://example.com/a%20%28b%29)"),
            ("encrypted text", '<en-crypt hint="x">AAAA</en-crypt>',
             "*(encrypted content not exported)*"),
        ])

    def test_lists(self):
        self.check_bodies([
            ("nested list", "<ul><li>a<ul><li>b</li></ul></li><li>c</li></ul>",
             "- a\n  - b\n- c"),
            ("sublist directly in its list",
             "<ul><li>a</li><ul><li>b</li></ul><li>c</li></ul>", "- a\n  - b\n- c"),
            ("numbered list", '<ol start="3"><li>a</li><li>b</li></ol>', "3. a\n4. b"),
            ("old checklist", '<div><en-todo checked="true"/>done</div><div><en-todo checked="false"/>open</div>',
             "- [x] done\n- [ ] open"),
            ("new checklist", '<ul style="--en-todo:true;"><li style="--en-checked:true;">done</li>'
                              '<li style="--en-checked:false;">open</li></ul>', "- [x] done\n- [ ] open"),
            ("table", "<table><tr><td>a</td><td>b|c</td></tr><tr><td>1</td><td><div>x</div><div>y</div></td></tr></table>",
             "| a | b\\|c |\n| --- | --- |\n| 1 | x<br>y |"),
            ("one-column table is a box",
             "<table><tr><td><div>inside</div></td></tr></table>", "inside"),
        ])


class TestColours(ExportTest):
    """Text colour becomes a span; black and grey are dropped."""

    def test_colours(self):
        self.check_bodies([
            ("red text",
             f'<span style="color:{RED};">r</span>', f"{RED_SPAN}r</span>"),
            ("yellow text", '<span style="color:rgb(229, 158, 37);">y</span>',
             f"{YELLOW_SPAN}y</span>"),
            ("font colour", '<font color="#FF0000">r</font>',
             '<span style="color: #ff0000">r</span>'),
            ("black is dropped", '<span style="color:rgb(0, 0, 0);">k</span>', "k"),
            ("grey is dropped", '<span style="color:#333333;">g</span>', "g"),
            ("named black is dropped", '<span style="color:black;">k</span>', "k"),
            ("background colour isn't text colour",
             '<span style="background-color:#ff0000;">b</span>', "b"),
            ("bold inside colour",
             f'<span style="color:{RED};">a <b>b</b></span>', f"{RED_SPAN}a **b**</span>"),
            ("coloured lines each get a span", f'<div style="color:{RED};">one<br/>two</div>',
             f"{RED_SPAN}one</span>\n{RED_SPAN}two</span>"),
            ("coloured list items", f'<ul><li><span style="color:{RED};">x</span></li><li style="color:{RED};">y</li></ul>',
             f"- {RED_SPAN}x</span>\n- {RED_SPAN}y</span>"),
            ("to-do box stays outside the colour", f'<div style="color:{RED};"><en-todo/>task</div>',
             f"- [ ] {RED_SPAN}task</span>"),
            ("colour inside colour",
             f'<div style="color:{RED};">a <span style="color:rgb(229, 158, 37);">b</span></div>',
             f"{RED_SPAN}a {YELLOW_SPAN}b</span></span>"),
        ])


class TestCode(ExportTest):
    """Evernote code blocks, <pre> and code pasted from an editor become fenced code."""

    def test_code(self):
        consolas = 'face="Consolas"'
        self.check_bodies([
            ("Evernote code block with its language",
             '<div style="--en-codeblock:true; --en-syntaxLanguage:cs;"><div>int x = 1;</div>'
             "<div>&nbsp;&nbsp;&nbsp; y();</div></div>", "```csharp\nint x = 1;\n    y();\n```"),
            ("plain text code block has no language",
             '<div style="--en-codeblock:true; --en-syntaxLanguage:plaintext;"><div>public class A { }</div></div>',
             "```\npublic class A { }\n```"),
            ("pre", "<pre>raw   text\n  indented</pre>",
             "```\nraw   text\n  indented\n```"),
            ("pasted lines in a monospaced font",
             f'<div>Before</div><div><font {consolas}><font color="#0000FF">int</font> x = 1;</font></div>'
             f"<div><font {consolas}><br/></font></div><div><font {consolas}>&nbsp;&nbsp;&nbsp; y();</font></div>"
             "<div>After</div>", "Before\n\n```\nint x = 1;\n\n    y();\n```\n\nAfter"),
            ("Courier lines", "<div><span style=\"font-family: 'Courier New';\">a</span></div>"
                              "<div><span style=\"font-family: 'Courier New';\">&nbsp; b</span></div>", "```\na\n  b\n```"),
            ("one monospaced block with line breaks",
             '<div style="font-family: Consolas;">f(<br/>\n    x)</div>', "```\nf(\n    x)\n```"),
            ("old indent inside code becomes spaces",
             f"<div><font {consolas}>class A</font></div><div><font {consolas}>{{</font></div>"
             f"<blockquote {OLD_INDENT}><div><font {consolas}>int x;</font></div></blockquote>"
             f"<div><font {consolas}>}}</font></div>", "```\nclass A\n{\n    int x;\n}\n```"),
            ("code all inside an indent starts at the margin",
             f"<blockquote {OLD_INDENT}><div><font {consolas}>a</font></div><div><font {consolas}>b</font></div></blockquote>",
             "```\na\nb\n```"),
            ("backticks in code get a longer fence",
             "<pre>a ``` b</pre>", "````\na ``` b\n````"),
            ("monospaced word in a sentence", '<div>Call <span style="font-family: Consolas;">foo()</span> now</div>',
             "Call `foo()` now"),
            ("coloured tokens join into one piece",
             f'<ul><li><font color="#2B91AF" {consolas}>Action</font><font {consolas}>&lt;</font>'
             f'<font color="#0000FF" {consolas}>int</font><font {consolas}>&gt; cb;</font></li></ul>',
             "- `Action<int> cb;`"),
            ("pieces at different nesting levels join",
             f'<div>x <span><font {consolas}>a</font></span><font {consolas}>b</font> y</div>', "x `ab` y"),
            ("code tag", "<div>use <code>x</code></div>", "use `x`"),
        ])

    def test_touching_pieces_of_code_join(self):
        for text, expected in [("`a``b`", "`ab`"), ("`a``b``c` and `d`", "`abc` and `d`"),
                               ("`a` `b`", "`a` `b`"), ("```\ncode\n```", "```\ncode\n```")]:
            with self.subTest(text):
                self.assertEqual(enex2md.join_touching_code(text), expected)


class TestLanguageGuess(unittest.TestCase):
    """guess_language names a language only when the code clearly shows it."""

    def test_guess_language(self):
        cases = [
            ("C# class",
             "public class Repository\n{\n    private readonly List<string> _names;\n}", "csharp"),
            ("C# using", "using System.Text;\nnamespace Acme.Tools", "csharp"),
            ("JavaScript",
             "function load(x) {\n    console.log(x);\n}", "javascript"),
            ("JavaScript object style",
             "function Car(model) {\n    this.model = model;\n}\nCar.prototype.drive = function () {\n};", "javascript"),
            ("JavaScript and C# look alike: no guess", "var c = new Car(1);", ""),
            ("Angular",
             'angular.module("app").controller("main", function ($scope) {\n});', "javascript"),
            ("HTML page with CSS inside", "<!DOCTYPE html>\n<html>\n<style>\n.a {\n  color: red;\n}\n</style>\n</html>",
             "html"),
            ("XSLT", '<xsl:template match="/">\n  <xsl:value-of select="."/>\n</xsl:template>', "xml"),
            ("CSS", ".box {\n    margin: 10px;\n    color: red;\n}", "css"),
            ("SQL", "SELECT name FROM users\nWHERE id = 1", "sql"),
            ("query with lowercase from",
             "SELECT count(*) from Transaction where appName = 'x'", "sql"),
            ("one shell command", "brew install something", "bash"),
            ("command after an environment variable",
             "NODE_PATH=. npm start", "bash"),
            ("shebang", "#!/usr/bin/env bash\necho hi", "bash"),
            ("JSON", '{\n  "a": 1,\n  "b": [true]\n}', "json"),
            ("YAML", "services:\n  db:\n    image: postgres\n    ports:\n      - 5432:5432", "yaml"),
            ("too little to tell", "x = y + 1", ""),
            ("plain text", "Hello there, this is a note.", ""),
        ]
        for case, code, expected in cases:
            with self.subTest(case):
                self.assertEqual(enex2md.guess_language(code), expected)

    def test_title_hints(self):
        for title, expected in [("Gradients CSS3", ("css",)), ("Angular JS - Directives", ("javascript",)),
                                ("Net Core - Options", ("csharp",)), ("XSLT basics", ("xml",)), ("Holidays", ())]:
            with self.subTest(title):
                self.assertEqual(enex2md.title_languages(title), expected)


class TestLanguageContext(ExportTest):
    """A note's title and its other code blocks help name a block too short to tell."""

    def test_context(self):
        weak_css = "<pre>body {\n}</pre>"
        weak_js = "<pre>if (a === b) go();</pre>"
        strong_js = "<pre>function f(x) {\n    console.log(x);\n}</pre>"
        for case, title, content, expected in [
            ("no hint, no language", "Notes", weak_css, ["```"]),
            ("the title names the language",
             "CSS3 tricks", weak_css, ["```css"]),
            ("alone, too short to tell", "Notes", weak_js, ["```"]),
            ("the note's other code blocks are JavaScript", "Notes", strong_js + weak_js,
             ["```javascript", "```javascript"]),
            ("the other block's recorded language isn't one guessed", "Notes",
             '<div style="-en-codeblock:true;--en-syntaxLanguage:ruby"><div>puts 1</div></div>'
             "<div>text</div><pre>foo bar</pre>", ["```ruby", "```"]),
        ]:
            with self.subTest(case):
                self.setUp()
                fences = [line for line in self.body(
                    content, title=title).split("\n") if line.startswith("```")]
                # every other fence opens a block
                self.assertEqual(fences[::2], expected)


class TestFrontMatterAndTags(ExportTest):
    """Front matter holds the note's details; tags become valid Obsidian tags."""

    def test_front_matter(self):
        notes = self.export(note_xml(
            'Plan: "Q1"', "<div>x</div>", tags=("Management - Excel", "work stuff", "#ideas", "a--b", "Management - Excel"),
            attributes={"source-url": "https://example.com/clip", "author": "Someone", "latitude": "45.1",
                        "longitude": "9.2"}))
        def local(stamp): return datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%dT%H:%M:%S")
        self.assertEqual(notes["Plan Q1.md"].split("\n---\n")[0], "\n".join([
            "---", 'title: "Plan: \\"Q1\\""', f"created: {local('20240115T093000Z')}",
            f"updated: {local('20240116T101500Z')}", "tags:", '  - "Management-Excel"', '  - "work-stuff"',
            '  - "ideas"', '  - "a-b"', 'source: "https://example.com/clip"', 'author: "Someone"',
            'location: "45.1, 9.2"']))

    def test_missing_fields_are_left_out(self):
        notes = self.export(
            note_xml("Bare", "<div>x</div>", created=None, updated=None))
        self.assertEqual(notes["Bare.md"], '---\ntitle: "Bare"\n---\nx\n')

    def test_tags(self):
        for tag, expected in [("Management - Excel", "Management-Excel"), ("work stuff", "work-stuff"),
                              ("#ideas", "ideas"), ("a--b", "a-b"), ("Dev/AI", "Dev/AI"), ("C++", "C")]:
            with self.subTest(tag):
                self.assertEqual(enex2md.obsidian_tag(tag), expected)


class TestFilesAndDates(ExportTest):
    """File names, duplicates, dates and attachments."""

    def test_file_names(self):
        for title, expected in [("a/b: c?", "a b c"), ("  .hidden", "hidden"), ("x#y^[z]|", "x y z"), ("", "Untitled")]:
            with self.subTest(title):
                self.assertEqual(enex2md.safe_name(title), expected)

    def test_duplicate_titles(self):
        notes = self.export(note_xml("Note", "<div>1</div>"),
                            note_xml("note", "<div>2</div>"))
        self.assertEqual(sorted(notes), ["Note.md", "note (2).md"])

    def test_file_dates_are_the_notes(self):
        self.export(note_xml("Dated", "<div>x</div>",
                    created="20200101T080000Z", updated="20230505T120000Z"))
        info = os.stat(os.path.join(self.dir, "Notebook", "Dated.md"))
        self.assertEqual(info.st_mtime, datetime(
            2023, 5, 5, 12, tzinfo=timezone.utc).timestamp())
        if sys.platform == "darwin":  # only macOS lets a script set the creation time
            self.assertEqual(info.st_birthtime, datetime(
                2020, 1, 1, 8, tzinfo=timezone.utc).timestamp())

    def test_attachments(self):
        same = resource(b"shared bytes", "image/png", "shared.png")
        clash = resource(b"other bytes", "image/png", "shared.png")
        unnamed = resource(b"no name", "image/png")
        self.export(note_xml("A", media(same) + media(unnamed), resources=(same, unnamed)),
                    note_xml("B", media(same) + media(clash), resources=(same, clash)))
        files = sorted(os.listdir(os.path.join(
            self.dir, "Notebook", "attachments")))
        self.assertEqual(files, [hashlib.md5(b"no name").hexdigest()[
                         :12] + ".png", "shared (2).png", "shared.png"])

    def test_picture_inside_the_note(self):
        png = base64.b64encode(b"inline image").decode()
        body = self.body(
            f'<div><img src="data:image/png;base64,{png}"/></div>')
        self.assertEqual(
            body, f"![](attachments/{hashlib.md5(b'inline image').hexdigest()[:12]}.png)")


class TestNoteLinks(ExportTest):
    """Links between notes become relative links to the note's file."""

    def test_links(self):
        notes = self.export(
            note_xml("Source", '<div><a href="evernote:///view/1/s1/abc/abc/">target note</a> and '
                               '<a href="evernote:///view/1/s1/zzz/zzz/">Missing</a></div>'),
            note_xml("Target Note", "<div>x</div>"))
        self.assertEqual(body_of(notes["Source.md"]),
                         "[target note](Target%20Note.md) and [Missing](evernote:///view/1/s1/zzz/zzz/)")

    def test_link_into_another_notebook(self):
        write_enex(os.path.join(self.dir, "A.enex"),
                   note_xml("From", '<div><a href="evernote:///view/1/s1/a/a/">To</a></div>'))
        write_enex(os.path.join(self.dir, "B.enex"),
                   note_xml("To", "<div>x</div>"))
        run(os.path.join(self.dir, "A.enex"), os.path.join(
            self.dir, "B.enex"), "-o", os.path.join(self.dir, "out"))
        self.assertEqual(body_of(read_folder(os.path.join(self.dir, "out"))[os.path.join("A", "From.md")]),
                         "[To](../B/To.md)")


class TestCommandLine(ExportTest):
    """Where the notes go, and what the script refuses to do."""

    def test_output_folder_next_to_each_file(self):
        write_enex(os.path.join(self.dir, "My Notebook.enex"),
                   note_xml("N", "<div>x</div>"))
        os.mkdir(os.path.join(self.dir, "sub"))
        write_enex(os.path.join(self.dir, "sub", "Other.enex"),
                   note_xml("M", "<div>y</div>"))
        run(os.path.join(self.dir, "My Notebook.enex"),
            os.path.join(self.dir, "sub", "Other.enex"))
        self.assertEqual(
            list(read_folder(os.path.join(self.dir, "My Notebook"))), ["N.md"])
        self.assertEqual(
            list(read_folder(os.path.join(self.dir, "sub", "Other"))), ["M.md"])

    def test_output_folder_option(self):
        for case, flat, expected in [("one subfolder per file", False, [os.path.join("A", "N.md"), os.path.join("B", "M.md")]),
                                     ("flat", True, ["M.md", "N.md"])]:
            with self.subTest(case):
                self.setUp()
                a = write_enex(os.path.join(self.dir, "A.enex"),
                               note_xml("N", "<div>x</div>"))
                b = write_enex(os.path.join(self.dir, "B.enex"),
                               note_xml("M", "<div>y</div>"))
                out = os.path.join(self.dir, "out")
                run(a, b, "-o", out, *(["--flat"] if flat else []))
                self.assertEqual(sorted(read_folder(out)), expected)

    def test_folder_of_exports(self):
        os.mkdir(os.path.join(self.dir, "exports"))
        write_enex(os.path.join(self.dir, "exports", "One.enex"),
                   note_xml("N", "<div>x</div>"))
        run(os.path.join(self.dir, "exports"))
        self.assertEqual(
            list(read_folder(os.path.join(self.dir, "exports", "One"))), ["N.md"])

    def test_refusals(self):
        enex = write_enex(os.path.join(self.dir, "Book.enex"),
                          note_xml("N", "<div>x</div>"))
        os.mkdir(os.path.join(self.dir, "Book"))
        with open(os.path.join(self.dir, "Book", "keep.txt"), "w") as file:
            file.write("mine")
        for case, args in [("output folder isn't empty", [enex]), ("--flat without -o", [enex, "--flat"]),
                           ("missing input", [os.path.join(self.dir, "nope.enex")])]:
            with self.subTest(case), self.assertRaises(SystemExit) as raised:
                with contextlib.redirect_stderr(io.StringIO()):
                    enex2md.main(args)
            self.assertEqual(raised.exception.code, 2)
        with open(os.path.join(self.dir, "Book", "keep.txt")) as file:
            self.assertEqual(file.read(), "mine")

    def test_export_file_is_not_changed(self):
        enex = write_enex(os.path.join(self.dir, "Book.enex"),
                          note_xml("N", media(PNG), resources=(PNG,)))
        with open(enex, "rb") as file:
            before = file.read()
        stamp = os.stat(enex).st_mtime
        run(enex)
        with open(enex, "rb") as file:
            self.assertEqual(file.read(), before)
        self.assertEqual(os.stat(enex).st_mtime, stamp)


if __name__ == "__main__":
    unittest.main()
