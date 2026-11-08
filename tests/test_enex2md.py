#!/usr/bin/env python3
"""Tests for src/enex2md.py: run with `python3 tests/test_enex2md.py` (add -v to list each case).

Most tests build a small .enex file, run the script on it as a user would and
read the Markdown it writes, so they check the output itself. Cases that
check the same thing are tables run as subtests. All data is made up. The
test classes follow the order of the script: the command line, the files it
writes, the notes' front matter, then how a note's body is rendered.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import html
import io
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))), "src"))
import enex2md  # noqa: E402

RED = "rgb(252, 18, 51)"
RED_SPAN = '<span style="color: rgb(252,18,51)">'
YELLOW_SPAN = '<span style="color: rgb(229,158,37)">'
INDENT = 'style="padding-left:40px;"'
OLD_INDENT = 'style="margin: 0px 0px 0px 40px; border: none; padding: 0px;"'
CONSOLAS = 'face="Consolas"'


def resource(data: bytes, mime: str, file_name: str | None = None) -> dict:
    return {"data": data, "mime": mime, "file_name": file_name}


def media(res: dict) -> str:
    """The <en-media> tag that shows a resource in a note's body."""
    return f'<en-media hash="{hashlib.md5(res["data"]).hexdigest()}" type="{res["mime"]}"/>'


def short_hash(data: bytes) -> str:
    """The name an attachment with no file name of its own is saved under."""
    return hashlib.md5(data).hexdigest()[:12]


PNG = resource(b"fake png one", "image/png", "one.png")
PNG2 = resource(b"fake png two", "image/png", "two.png")
PDF = resource(b"%PDF-1.4 fake", "application/pdf", "Report.pdf")
XLSX = resource(b"fake sheet", "application/vnd.ms-excel", "data.xlsx")
RESOURCES = (PNG, PNG2, PDF, XLSX)
XLSX_BLOCK = "> [!attachment] [data.xlsx](_attachments/data.xlsx)"


def note_xml(title: str, content: str, created: str | None = "20240115T093000Z",
             updated: str | None = "20240116T101500Z", tags: tuple[str, ...] = (),
             attributes: dict[str, str] | None = None, resources: tuple[dict, ...] = (),
             raw_resources: tuple[str, ...] = ()) -> str:
    """A note of an export; raw_resources are <resource> elements written as given."""
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
    parts += raw_resources
    return "".join(parts) + "</note>"


def write_enex(path: str, *notes: str) -> str:
    with open(path, "w", encoding="utf-8") as file:
        file.write('<?xml version="1.0" encoding="UTF-8"?>\n<en-export>' +
                   "".join(notes) + "</en-export>")
    return path


def run(*args: str) -> str:
    """Runs the script's command line, returning its progress messages and warnings."""
    messages = io.StringIO()
    with contextlib.redirect_stderr(messages):
        enex2md.main(list(args))
    return messages.getvalue()


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


def guess_language(code: str, hints: tuple[str, ...] = ()) -> str:
    return enex2md.confident_language(enex2md.language_scores(code, hints))


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

    def test_summary(self):
        messages = run(write_enex(os.path.join(self.dir, "Book.enex"), note_xml(
            "N", f'<div><a href="evernote:///view/1/s1/a/a/">Gone</a></div>{media(PNG)}', resources=(PNG,))))
        self.assertIn("Wrote 1 notes and 1 attachments", messages)
        self.assertIn("Linked 0 of 1 links between notes", messages)

    def test_refusals(self):
        enex = write_enex(os.path.join(self.dir, "Book.enex"),
                          note_xml("N", "<div>x</div>"))
        os.mkdir(os.path.join(self.dir, "Book"))
        with open(os.path.join(self.dir, "Book", "keep.txt"), "w") as file:
            file.write("mine")
        os.mkdir(os.path.join(self.dir, "empty"))
        for case, args in [("output folder isn't empty", [enex]), ("--flat without -o", [enex, "--flat"]),
                           ("missing input", [
                            os.path.join(self.dir, "nope.enex")]),
                           ("folder with no .enex files", [os.path.join(self.dir, "empty")])]:
            with self.subTest(case), self.assertRaises(SystemExit) as raised:
                run(*args)
            self.assertEqual(raised.exception.code, 2)
        with open(os.path.join(self.dir, "Book", "keep.txt")) as file:
            self.assertEqual(file.read(), "mine")

    def test_hidden_files_dont_make_a_folder_full(self):
        enex = write_enex(os.path.join(self.dir, "Book.enex"),
                          note_xml("N", "<div>x</div>"))
        os.makedirs(os.path.join(self.dir, "Book", ".obsidian"))
        run(enex)
        self.assertEqual(
            list(read_folder(os.path.join(self.dir, "Book"))), ["N.md"])

    def test_broken_export(self):
        enex = os.path.join(self.dir, "Broken.enex")
        with open(enex, "w", encoding="utf-8") as file:
            file.write("<en-export><note><title>N</title>")
        with self.assertRaises(SystemExit) as raised:
            run(enex)
        self.assertIn("is not a valid Evernote export",
                      str(raised.exception.code))

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


class TestFilesAndDates(ExportTest):
    """File names, duplicates, dates and attachments."""

    def test_note_names_keep_the_title(self):
        for title, expected in [("Operatori ?? e ?", "Operatori ?? e ?"), ("Thinking in C#", "Thinking in C#"),
                                ('From IEnumerble<Task> to "Task"',
                                 'From IEnumerble<Task> to "Task"'),
                                ("Try/Catch", "Try\u2215Catch"), ("Interview: Gigs",
                                                                  "Interview\ua789 Gigs"),
                                (".Net Core - Intro", "\u2024Net Core - Intro"), ("  a\tb  ",
                                                                                  "a b"), ("", "Untitled"),
                                ("a" * 300, "a" * 240), ("\u00e0" * 200, "\u00e0" * 120)]:
            with self.subTest(title):
                self.assertEqual(enex2md.note_name(title), expected)

    def test_attachment_names(self):
        for name, expected in [("a/b: c?", "a b c"), ("  .hidden", "hidden"), ("x#y^[z]|", "x y z"), ("", "Untitled"),
                               ("a" * 120, "a" * 100)]:
            with self.subTest(name):
                self.assertEqual(enex2md.safe_name(name), expected)

    def test_link_to_a_note_whose_title_has_special_characters(self):
        notes = self.export(
            note_xml("Source", '<div><a href="evernote:///view/1/s1/a/a/">Thinking in C#</a> and '
                               '<a href="evernote:///view/1/s1/b/b/">Try/Catch?</a></div>'),
            note_xml("Thinking in C#", "<div>x</div>"), note_xml("Try/Catch?", "<div>y</div>"))
        self.assertEqual(
            sorted(notes), ["Source.md", "Thinking in C#.md", "Try\u2215Catch?.md"])
        self.assertEqual(body_of(notes["Source.md"]),
                         "[Thinking in C#](Thinking%20in%20C%23.md) and [Try/Catch?](Try\u2215Catch%3F.md)")

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
            self.dir, "Notebook", "_attachments")))
        self.assertEqual(
            files, [short_hash(b"no name") + ".png", "shared (2).png", "shared.png"])

    def test_unreadable_attachments_are_skipped(self):
        notes = self.export(note_xml("N", "<div>x</div>", raw_resources=(
            "<resource><data>abc</data><mime>image/png</mime></resource>",  # not base64
            "<resource><data></data><mime>image/png</mime></resource>")))
        self.assertEqual(body_of(notes["N.md"]), "x")
        self.assertFalse(os.path.exists(
            os.path.join(self.dir, "Notebook", "_attachments")))


class TestFrontMatterAndTags(ExportTest):
    """Front matter holds the note's details; tags become valid Obsidian tags."""

    def test_front_matter(self):
        notes = self.export(note_xml(
            'Plan: "Q1"', "<div>x</div>", tags=("Management - Excel", "work stuff", "#ideas", "a--b", "Management - Excel"),
            attributes={"source-url": "https://example.com/clip", "author": "Someone", "latitude": "45.1",
                        "longitude": "9.2"}))

        def local(stamp): return datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%dT%H:%M:%S")
        self.assertEqual(notes['Plan\ua789 "Q1".md'].split("\n---\n")[0], "\n".join([
            "---", 'title: "Plan: \\"Q1\\""', f"created: {local('20240115T093000Z')}",
            f"updated: {local('20240116T101500Z')}", "tags:", '  - "Management-Excel"', '  - "work-stuff"',
            '  - "ideas"', '  - "a-b"', 'source: "https://example.com/clip"', 'author: "Someone"',
            'location: "45.1, 9.2"']))

    def test_missing_fields_are_left_out(self):
        notes = self.export(
            note_xml("Bare", "<div>x</div>", created=None, updated=None))
        self.assertEqual(notes["Bare.md"], '---\ntitle: "Bare"\n---\nx\n')

    def test_unreadable_fields_are_left_out(self):
        notes = self.export(note_xml("Odd", "<div>x</div>", created="2024-01-15", updated=" ",
                                     attributes={"author": " ", "latitude": "45.1"}))
        self.assertEqual(notes["Odd.md"], '---\ntitle: "Odd"\n---\nx\n')

    def test_tags(self):
        for tag, expected in [("Management - Excel", "Management-Excel"), ("work stuff", "work-stuff"),
                              ("#ideas", "ideas"), ("a--b",
                                                    "a-b"), ("Dev/AI", "Dev/AI"), ("C++", "C"),
                              ("Develop - C#", "Develop-CSharp"), ("Develop - .Net Core", "Develop-Net-Core")]:
            with self.subTest(tag):
                self.assertEqual(enex2md.obsidian_tag(tag), expected)


class TestWarnings(ExportTest):
    """A note that can't be converted is kept as HTML with a warning; a normal one gives none."""

    def convert(self, content: str, resources: tuple[dict, ...] = ()) -> tuple[str, str]:
        """Returns the converted note's body and the script's messages."""
        enex = write_enex(os.path.join(self.dir, "Book.enex"),
                          note_xml("N", content, resources=resources))
        messages = run(enex)
        return body_of(read_folder(os.path.join(self.dir, "Book"))["N.md"]), messages

    def test_note_using_every_feature_converts_without_warnings(self):
        body, messages = self.convert(
            "<h2>Plan</h2><div>Some <b>bold</b> and "
            f'<span style="color:{RED};">red</span> text, a <a href="https://example.com">link</a>.</div>'
            '<ul><li>one</li><li>two</li></ul><ul style="--en-todo:true;"><li style="--en-checked:true;">done</li></ul>'
            "<table><tr><td>a</td><td>b</td></tr></table>"
            '<div style="--en-codeblock:true; --en-syntaxLanguage:ruby;"><div>puts 1</div></div>'
            "<pre>foo bar</pre><hr/>" + media(PNG) + media(XLSX),
            resources=(PNG, XLSX))
        self.assertNotIn("warning:", messages)
        self.assertNotIn("<div", body)

    def test_failed_conversion_keeps_the_note_as_html(self):
        with mock.patch.object(enex2md.NoteRenderer, "render", side_effect=ValueError("boom")):
            body, messages = self.convert("<div>kept</div>")
        self.assertIn("<div>kept</div>", body)
        self.assertIn(
            "warning: N: kept as HTML, conversion failed (boom)", messages)

    def test_missing_attachment(self):
        body, messages = self.convert(f"<div>a</div>{media(PNG)}")
        self.assertEqual(body, "a")
        self.assertIn(
            "warning: N: 1 attachment(s) missing from the export", messages)


class TestNoteLinks(ExportTest):
    """Links between notes become relative links to the note's file."""

    def test_links(self):
        notes = self.export(
            note_xml("Source", '<div><a href="evernote:///view/1/s1/abc/abc/">target note</a> and '
                               '<a href="evernote:///view/1/s1/zzz/zzz/">Missing</a></div>'),
            note_xml("Target Note", "<div>x</div>"))
        self.assertEqual(body_of(notes["Source.md"]),
                         "[target note](Target%20Note.md) and [Missing](evernote:///view/1/s1/zzz/zzz/)")

    def test_formatted_link_label(self):
        notes = self.export(
            note_xml(
                "Source", '<div><a href="evernote:///view/1/s1/abc/abc/"><b>Target</b></a></div>'),
            note_xml("Target", "<div>x</div>"))
        self.assertEqual(
            body_of(notes["Source.md"]), "[**Target**](Target.md)")

    def test_link_to_a_duplicated_title_opens_the_first_note(self):
        notes = self.export(
            note_xml(
                "Source", '<div><a href="evernote:///view/1/s1/abc/abc/">Plan</a></div>'),
            note_xml("Plan", "<div>first</div>"),
            note_xml("Plan", "<div>second</div>"))
        self.assertEqual(body_of(notes["Source.md"]), "[Plan](Plan.md)")
        self.assertEqual(body_of(notes["Plan (2).md"]), "second")

    def test_link_into_another_notebook(self):
        write_enex(os.path.join(self.dir, "A.enex"),
                   note_xml("From", '<div><a href="evernote:///view/1/s1/a/a/">To</a></div>'))
        write_enex(os.path.join(self.dir, "B.enex"),
                   note_xml("To", "<div>x</div>"))
        run(os.path.join(self.dir, "A.enex"), os.path.join(
            self.dir, "B.enex"), "-o", os.path.join(self.dir, "out"))
        self.assertEqual(body_of(read_folder(os.path.join(self.dir, "out"))[os.path.join("A", "From.md")]),
                         "[To](../B/To.md)")


class TestLinesAndBlankLines(ExportTest):
    """One <div> per line; a typed blank line becomes a visible &nbsp; line."""

    def test_lines_and_blank_lines(self):
        self.check_bodies([
            ("each line is a paragraph", "<div>a</div><div>b</div>", "a\n\nb"),
            ("typed blank line between text, two lines ending the paragraph above",
             "<div>a</div><div><br/></div><div>b</div>", "a\n&nbsp;\n&nbsp;\n\nb"),
            ("blank line in nested spans", "<div>a</div><div><span><span><br/></span></span></div><div>b</div>",
             "a\n&nbsp;\n&nbsp;\n\nb"),
            ("two line breaks inside a line",
             "<div>a<br/><br/>b</div>", "a\n&nbsp;\n&nbsp;\n\nb"),
            ("a <br> ending a line draws nothing",
             "<div>a<br/></div><div>b</div>", "a\n\nb"),
            ("a <br> ending a line before spaces draws nothing",
             "<div>a<br/> </div><div>b</div>", "a\n\nb"),
            ("no backslash line breaks",
             "<div>a</div><div>b</div><div>c</div>", "a\n\nb\n\nc"),
            ("blank line after a list",
             "<ul><li>x</li></ul><div><br/></div><div>b</div>", "- x\n\n&nbsp;\nb"),
            ("blank line between text and a list, two lines as between text",
             "<div>a</div><div><br/></div><ul><li>x</li></ul>", "a\n&nbsp;\n&nbsp;\n\n- x"),
            ("a line starting with a space is still text",
             "<div> a</div><div><br/></div><div>b</div><div><br/></div><ul><li>x</li></ul>",
             "a\n&nbsp;\n&nbsp;\n\nb\n&nbsp;\n&nbsp;\n\n- x"),
            ("after code, one line, as after a list", "<pre>c</pre><div><br/></div><div>b</div>",
             "```\nc\n```\n\n&nbsp;\nb"),
            ("between code and a list, one line", "<pre>c</pre><div><br/></div><ul><li>x</li></ul>",
             "```\nc\n```\n&nbsp;\n\n- x"),
            ("two blank lines between text and a list",
             "<div>a</div><div><br/></div><div><br/></div><ul><li>x</li></ul>", "a\n&nbsp;\n&nbsp;\n&nbsp;\n&nbsp;\n\n- x"),
            ("blank line between text and an attachment",
             f"<div>a</div><div><br/></div>{media(XLSX)}", f"a\n&nbsp;\n&nbsp;\n\n{XLSX_BLOCK}"),
            ("blank line between a list and an attachment is a paragraph of its own",
             f"<ul><li>x</li></ul><div><br/></div>{media(XLSX)}", f"- x\n\n&nbsp;\n\n{XLSX_BLOCK}"),
            ("blank line between a quote and a list is a paragraph of its own",
             "<blockquote><div>q</div></blockquote><div><br/></div><ul><li>x</li></ul>",
             "> q\n\n&nbsp;\n\n- x"),
            ("text after a list starts a paragraph",
             "<ul><li>x</li></ul><div>b</div>", "- x\n\nb"),
            ("text before a list", "<div>a</div><ul><li>x</li></ul>", "a\n\n- x"),
            ("text after a to-do starts a paragraph",
             '<div><en-todo checked="true"/>a</div><div><en-todo/>b</div><div>c</div>', "- [x] a\n- [ ] b\n\nc"),
            ("paragraphs", "<p>a</p><p>b</p>", "a\n\nb"),
            ("blank lines at the start and end are dropped",
             "<div><br/></div><div>a</div><div><br/></div>", "a"),
            ("a closing tag with no opening one is ignored",
             "<div>a</b>b</div>", "ab"),
            ("two typed blank lines",
             "<div>a</div><div><br/></div><div><br/></div><div>b</div>", "a\n&nbsp;\n&nbsp;\n&nbsp;\n&nbsp;\n\nb"),
            ("a line break within a line stays a line break",
             "<div>a<br/>b</div>", "a\nb"),
            ("the lines of a list item stay lines",
             "<ul><li><div>a</div><div>b</div></li></ul>", "- a\n  b"),
            ("a typed blank line in a list item stays a line of it",
             "<ul><li><div>a</div><div><br/></div><div>b</div></li></ul>", "- a\n  &nbsp;\n  b"),
            ("a <br> before a new line draws nothing",
             "a<br/><div>b</div>", "a\n\nb"),
            ("nor one ending a line inside formatting",
             "<div><span>a<br/></span></div><div><span><br/></span></div><div>b</div>", "a\n&nbsp;\n&nbsp;\n\nb"),
            ("nor one inside formatting before a new line",
             "<div><u>a<br/></u><div><br/></div><div>b</div></div>", "<u>a</u>\n&nbsp;\n&nbsp;\n\nb"),
            ("but one on a line of its own, between two blocks, is a blank line",
             "<ul><li>q</li></ul><span><br/></span><div>b</div>", "- q\n\n&nbsp;\nb"),
            ("nor one followed by a space inside formatting",
             "<div><span>a<br/> </span></div><div><br/></div><div>b</div>", "a\n&nbsp;\n&nbsp;\n\nb"),
            ("two before a new line draw one blank line",
             "a<br/><br/><div>b</div>", "a\n&nbsp;\n&nbsp;\n\nb"),
            ("lines holding only a link sit tight, as a list does",
             '<a href="http://a">a</a><div><a href="http://b">b</a></div><div><b><a href="http://c">c</a></b></div>'
             '<div>text</div>', "[a](http://a)\n[b](http://b)\n**[c](http://c)**\n\ntext"),
            ("a line break written as in HTML, with no slash",
             "<div>a<br>b</div>", "a\nb"),
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
             "![](_attachments/one.png)\n\n---\n\nb"),
        ])


class TestIndentation(ExportTest):
    """Indented lines lose their indent and get a blank line after them; old indents aren't quotes."""

    def test_indentation(self):
        self.check_bodies([
            ("indented lines end with a blank line",
             f"<div>Task</div><div {INDENT}>x</div><div {INDENT}>y</div><div>Action</div>",
             "Task\n\nx\n\ny\n&nbsp;\n&nbsp;\n\nAction"),
            ("a typed blank line isn't doubled",
             f"<div>Task</div><div {INDENT}>x</div><div><br/></div><div>Next</div>", "Task\n\nx\n&nbsp;\n&nbsp;\n\nNext"),
            ("deeper indent stays in the same run",
             f'<div {INDENT}>x</div><div style="padding-left:80px;">y</div><div>z</div>', "x\n\ny\n&nbsp;\n&nbsp;\n\nz"),
            ("old Evernote indent isn't a quote", f"<blockquote {OLD_INDENT}><div>x</div></blockquote><div>y</div>",
             "x\n&nbsp;\n&nbsp;\n\ny"),
            ("a real quote", "<blockquote><div>q1</div><div>q2</div></blockquote>", "> q1\n> q2"),
            ("a quote holding code is an indent, as older Evernote drew one",
             f"<blockquote><div>q</div><div><font {CONSOLAS}>x();</font></div></blockquote>", "q\n\n```\nx();\n```"),
            ("an empty quote is dropped",
             "<div>a</div><blockquote> </blockquote>", "a"),
        ])


class TestFormatting(ExportTest):
    """Bold, italic, strike, underline, highlights, headings, links and escaping."""

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
             f'<span style="color:{RED};font-weight:bold;--en-highlight:yellow;">x</span>',
             f"{RED_SPAN}**=={RED_SPAN}x</span>==**</span>"),
            ("a highlight in coloured text keeps the colour, which Obsidian would draw it without",
             f'<div><font color="{RED}"><u><span style="--en-highlight:yellow;">S:</span> Single</u></font></div>',
             f"{RED_SPAN}<u>=={RED_SPAN}S:</span>== Single</u></span>"),
            ("an italic sentence runs through coloured, partly highlighted text",
             f'<div><i>a </i><i><span style="color:{RED};"><span style="--en-highlight:yellow;">b</span></span></i><i> c</i></div>',
             f"*a =={RED_SPAN}b</span>== c*"),
            ("coloured text highlighted in full has the highlight around the colour",
             f'<div><span style="color:{RED};--en-highlight:yellow;">x</span></div>', f"=={RED_SPAN}x</span>=="),
            ("nested bold isn't doubled", "<b>a <b>b</b></b>", "**a b**"),
            ("heading", "<h2>Title <i>two</i></h2>", "## Title *two*"),
            ("escaping", "<div>5 * 3 [x] &lt;tag&gt; a_b _c_ \\</div>",
             "5 \\* 3 \\[x\\] \\<tag> a_b \\_c\\_ \\\\"),
            ("encrypted text", '<en-crypt hint="x">AAAA</en-crypt>',
             "*(encrypted content not exported)*"),
            ("page parts that aren't text are dropped",
             "<div>a<script>alert(1)</script><style>p { }</style></div>", "a"),
            ("touching highlights join into one",
             '<div><span style="--en-highlight:yellow;">Everywhere else</span>'
             '<span style="--en-highlight:yellow;">:</span></div>', "==Everywhere else:=="),
            ("touching bold and strikethrough join into one",
             "<div><b>a</b><b>b</b> <s>c</s><s>d</s></div>", "**ab** ~~cd~~"),
            ("touching italics join into one, rather than reading as bold",
             "<div><i>a</i><i>b</i></div>", "*ab*"),
            ("an italic sentence split around a highlight is one, with the space it holds",
             '<div><i>a </i><i><span style="--en-highlight:yellow;">b</span></i><i>. c</i></div>',
             "*a ==b==. c*"),
            ("a highlighted sentence with a word in another colour is one highlight",
             '<div><span style="--en-highlight:yellow;">a </span>'
             '<span style="--en-highlight:yellow;color:rgb(227, 0, 0);">b</span><span style="--en-highlight:yellow;">.</span></div>',
             '==a <span style="color: rgb(227,0,0)">b</span>.=='),
            ("touching underlines join into one",
             "<div><u>a</u><u>b</u></div>", "<u>ab</u>"),
            ("bold coloured text with nothing to join stays inside its colour",
             f'<div>a <span style="color: {RED};"><b>b</b></span> c</div>', f"a {RED_SPAN}**b**</span> c"),
            ("even next to an underline it would join",
             f'<div><u>a</u><span style="color: {RED};"><u>b</u></span></div>', f"<u>a</u>{RED_SPAN}<u>b</u></span>"),
            ("an underline goes inside the colour, to be drawn in it",
             f'<div><u><span style="color: {RED};">a</span></u> <span style="color: {RED};"><u>b</u></span></div>',
             f"{RED_SPAN}<u>a</u></span> {RED_SPAN}<u>b</u></span>"),
            ("Evernote's highlight yellow as a plain background is a highlight",
             '<div><span style="background-color: rgb(255, 250, 165);">a</span> '
             '<span style="background-color: white;">b</span></div>', "==a== b"),
            ("highlights with a space between them stay apart",
             '<div><span style="--en-highlight:yellow;">a</span> <span style="--en-highlight:yellow;">b</span></div>',
             "==a== ==b=="),
            ("marks typed in code stay", "<div>x <code>a**b</code></div><pre>a====b\n~~~~</pre>",
             "x `a**b`\n\n```\na====b\n~~~~\n```"),
        ])

    def test_text_that_looks_like_markdown_at_the_start_of_a_line(self):
        """A line typed as text but starting like a list, heading or quote stays text."""
        self.check_bodies([
            ("numbers typed by hand aren't a list",
             "<div>1) a</div><div>2. b</div>", "1\\) a\n\n2\\. b"),
            ("nor bullets, headings or quotes",
             "<div>- a</div><div>+ b</div><div># c</div><div>&gt; d</div>", "\\- a\n\n\\+ b\n\n\\# c\n\n\\> d"),
            ("spacing as text, not as a list",
             "<div>t</div><div><br/></div><div>1) a</div><div><br/></div><div>2) b</div>",
             "t\n&nbsp;\n&nbsp;\n\n1\\) a\n&nbsp;\n&nbsp;\n\n2\\) b"),
            ("after a line break", "<div>a<br/>1) b</div>", "a\n1\\) b"),
            ("a line of a list item",
             "<ul><li><div>a</div><div>1) b</div></li></ul>", "- a\n  1\\) b"),
            ("in a quote", "<blockquote><div>1) a</div></blockquote>", "> 1\\) a"),
            ("not in the middle of a line",
             "<div>x 1) a - b # c</div>", "x 1) a - b # c"),
            ("not as a list item's text", "<ul><li>1) a</li></ul>", "- 1) a"),
            ("not after a to-do box",
             "<div><en-todo/>1) a</div>", "- [ ] 1) a"),
            ("not in coloured text",
             f'<div><span style="color:{RED};">1) a</span></div>', f"{RED_SPAN}1) a</span>"),
        ])

    def test_links(self):
        self.check_bodies([
            ("link", '<a href="https://example.com/a (b)">site</a>',
             "[site](https://example.com/a%20%28b%29)"),
            ("spaces stay outside the link", '<div>see<a href="https://example.com"> site </a>now</div>',
             "see [site](https://example.com) now"),
            ("no address is plain text", "<a>site</a>", "site"),
            ("a link within the page is plain text",
             '<a href="#top">top</a>', "top"),
            ("a script link is plain text",
             '<a href="javascript:void(0)">run</a>', "run"),
        ])


class TestListsAndTables(ExportTest):
    """Lists, checklists and tables."""

    def test_lists(self):
        self.check_bodies([
            ("nested list", "<ul><li>a<ul><li>b</li></ul></li><li>c</li></ul>",
             "- a\n  - b\n- c"),
            ("sublist directly in its list",
             "<ul><li>a</li><ul><li>b</li></ul><li>c</li></ul>", "- a\n  - b\n- c"),
            ("sublist before any item", "<ul><ul><li>b</li></ul><li>c</li></ul>",
             "- b\n- c"),
            ("a typed blank line after a sublist stays in the item",
             "<ul><li><div>a</div><ul><li>b</li></ul><div><br/></div><div>c</div></li></ul>",
             "- a\n  - b\n\n  &nbsp;\n  c"),
            ("numbered list", '<ol start="3"><li>a</li><li>b</li></ol>', "3. a\n4. b"),
            ("text directly in a list is an item", "<ul><div>a</div><li>b</li></ul>",
             "- a\n- b"),
            ("empty items are dropped", "<ul><li>a</li><li> </li></ul>", "- a"),
            ("an empty list is dropped", "<div>a</div><ul> </ul>", "a"),
            ("an empty sublist is dropped", "<ul><li>a</li><ul> </ul></ul>", "- a"),
            ("old checklist", '<div><en-todo checked="true"/>done</div><div><en-todo checked="false"/>open</div>',
             "- [x] done\n- [ ] open"),
            ("new checklist", '<ul style="--en-todo:true;"><li style="--en-checked:true;">done</li>'
                              '<li style="--en-checked:false;">open</li></ul>', "- [x] done\n- [ ] open"),
        ])

    def test_tables(self):
        self.check_bodies([
            ("table", "<table><tr><td>a</td><td>b|c</td></tr><tr><td>1</td><td><div>x</div><div>y</div></td></tr></table>",
             "| a | b\\|c |\n| --- | --- |\n| 1 | x<br>y |"),
            ("header and body rows", "<table>\n<colgroup></colgroup><thead><tr><th>h1</th><th>h2</th></tr></thead>"
                                     "<tbody><tr><td>1</td></tr></tbody></table>",
             "| h1 | h2 |\n| --- | --- |\n| 1 |  |"),
            ("one-column table is a box",
             "<table><tr><td><div>inside</div></td></tr></table>", "inside"),
            ("an empty table is dropped", "<div>a</div><table><tr></tr></table>", "a"),
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
            ("short hex colour", '<span style="color:#f00;">r</span>',
             '<span style="color: #f00">r</span>'),
            ("named colour", '<span style="color:red;">r</span>',
             '<span style="color: red">r</span>'),
            ("black is dropped", '<span style="color:rgb(0, 0, 0);">k</span>', "k"),
            ("grey is dropped", '<span style="color:#333333;">g</span>', "g"),
            ("short grey is dropped", '<span style="color:#333;">g</span>', "g"),
            ("near-black with a tint, as pasted text has, is dropped",
             '<span style="color:rgb(23, 31, 38);">k</span>', "k"),
            ("grey with a tint is dropped",
             '<span style="color:#5a6066;">g</span>', "g"),
            ("short grey with a tint is dropped",
             '<span style="color:#344;">g</span>', "g"),
            ("dark red isn't a grey", '<span style="color:#800000;">r</span>',
             '<span style="color: #800000">r</span>'),
            ("named black is dropped", '<span style="color:black;">k</span>', "k"),
            ("background colour isn't text colour",
             '<span style="background-color:#ff0000;">b</span>', "b"),
            ("bold inside colour",
             f'<span style="color:{RED};">a <b>b</b></span>', f"{RED_SPAN}a **b**</span>"),
            ("coloured lines each get a span", f'<div style="color:{RED};">one<br/>two</div>',
             f"{RED_SPAN}one</span>\n{RED_SPAN}two</span>"),
            ("coloured list items", f'<ul><li><span style="color:{RED};">x</span></li><li style="color:{RED};">y</li></ul>',
             f"- {RED_SPAN}x</span>\n- {RED_SPAN}y</span>"),
            ("a list in coloured text", f'<div style="color:{RED};"><ul><li>x</li><li>y</li></ul></div>',
             f"- {RED_SPAN}x</span>\n- {RED_SPAN}y</span>"),
            ("to-do box stays outside the colour", f'<div style="color:{RED};"><en-todo/>task</div>',
             f"- [ ] {RED_SPAN}task</span>"),
            ("colour inside colour",
             f'<div style="color:{RED};">a <span style="color:rgb(229, 158, 37);">b</span></div>',
             f"{RED_SPAN}a {YELLOW_SPAN}b</span></span>"),
        ])


class TestPictures(ExportTest):
    """A picture gets exactly one blank line under it; PDFs show inline, other files are blocks."""

    def test_blank_line_under_pictures(self):
        """A typed blank line under a picture is two; with none typed there is none,
        just the gap between paragraphs."""
        image, image2 = "![](_attachments/one.png)", "![](_attachments/two.png)"
        self.check_bodies([
            ("no blank line typed, none added",
             f"<div>a</div>{media(PNG)}<div>b</div>", f"a\n\n{image}\n\nb"),
            ("a typed blank line is two", f"<div>{media(PNG)}</div><div><br/></div><div>b</div>",
             f"{image}\n&nbsp;\n&nbsp;\n\nb"),
            ("two typed blank lines are two as well",
             f"<div>{media(PNG)}</div><div><br/></div><div><br/></div><div>b</div>", f"{image}\n&nbsp;\n&nbsp;\n\nb"),
            ("pictures one after the other, with no blank line typed between them",
             f"{media(PNG)}{media(PNG2)}<div>b</div>", f"{image}\n\n{image2}\n\nb"),
            ("before a list, with no blank line typed",
             f"{media(PNG)}<ul><li>x</li></ul>", f"{image}\n\n- x"),
            ("before a list, with a typed blank line, two lines end the picture's section",
             f"{media(PNG)}<div><br/></div><ul><li>x</li></ul>", f"{image}\n&nbsp;\n&nbsp;\n\n- x"),
            ("before an attachment", f"{media(PNG)}<div><br/></div>{media(XLSX)}",
             f"{image}\n&nbsp;\n&nbsp;\n\n{XLSX_BLOCK}"),
            ("inside a list item, the usual gap above it, as Evernote draws it on a line of its own",
             f"<ul><li><div>a</div>{media(PNG)}</li><li>b</li></ul>",
             f"- a\n\n  {image}\n- b"),
            ("inside a list item, a typed blank line under it is a line of the item",
             f"<ul><li><div>a</div>{media(PNG)}<div><br/></div><div>c</div></li></ul>",
             f"- a\n\n  {image}\n  &nbsp;\n  c"),
            ("on the same line as text in a list item, it stays there",
             f"<ul><li><div>a {media(PNG)}</div></li></ul>", f"- a {image}"),
            ("after a typed blank line under text, two lines, as before anything",
             f"<div>a</div><div><br/></div>{media(PNG)}", f"a\n&nbsp;\n&nbsp;\n\n{image}"),
            ("last in the note", f"<div>a</div>{media(PNG)}", f"a\n\n{image}"),
            ("a <br> between two pictures, before a new line, draws nothing",
             f"{media(PNG)}<br/><div>{media(PNG2)}<br/></div>", f"{image}\n\n{image2}"),
        ])

    def test_blank_line_under_pdfs(self):
        """A PDF isn't a picture: a typed blank line under it is one line."""
        pdf = "![](_attachments/Report.pdf)"
        self.check_bodies([
            ("a typed blank line",
             f"{media(PDF)}<div><br/></div><div>b</div>", f"{pdf}\n&nbsp;\n\nb"),
            ("no blank line typed",
             f"{media(PDF)}<div>b</div>", f"{pdf}\n\nb"),
            ("before a list",
             f"{media(PDF)}<div><br/></div><ul><li>x</li></ul>", f"{pdf}\n&nbsp;\n\n- x"),
        ])

    def test_attachment_embeds(self):
        self.check_bodies([
            ("picture", media(PNG), "![](_attachments/one.png)"),
            ("PDF shown inline", media(PDF), "![](_attachments/Report.pdf)"),
            ("other file is a block, as Evernote shows it", media(XLSX),
             f"{XLSX_BLOCK}"),
            ("a file and its picture each get a line",
             f"<div>a</div>{media(XLSX)}{media(PNG)}<div>b</div>",
             f"a\n\n{XLSX_BLOCK}\n\n![](_attachments/one.png)\n\nb"),
            ("a file in a sentence gets its own block", f"<div>see {media(XLSX)} here</div>",
             f"see\n\n{XLSX_BLOCK}\n\nhere"),
            ("a file in a table cell stays a link, since a block would break the table",
             f"<table><tr><td>a</td><td>{media(XLSX)}</td></tr></table>",
             "| a | [data.xlsx](_attachments/data.xlsx) |\n| --- | --- |"),
        ])

    def test_attachment_not_shown_in_the_text_is_added_at_the_end(self):
        for case, content, expected in [("after the text", "<div>a</div>", f"a\n\n{XLSX_BLOCK}"),
                                        ("after a picture, with no blank line, as none was typed",
                                         media(PNG), f"![](_attachments/one.png)\n\n{XLSX_BLOCK}"),
                                        ("in an empty note", "", XLSX_BLOCK)]:
            with self.subTest(case):
                self.setUp()
                shown = (PNG, XLSX) if media(PNG) in content else (XLSX,)
                self.assertEqual(self.body(content, resources=shown), expected)

    def test_pictures_from_the_web(self):
        self.check_bodies([
            ("address and description", '<img src="https://example.com/a b.png" alt="a *b"/>',
             "![a \\*b](https://example.com/a%20b.png)"),
            ("no address", '<div>a<img src=" "/></div>', "a"),
        ])

    def test_picture_inside_the_note(self):
        png = base64.b64encode(b"inline image").decode()
        self.check_bodies([
            ("base64", f'<div><img src="data:image/png;base64,{png}"/></div>',
             f"![](_attachments/{short_hash(b'inline image')}.png)"),
            ("percent-encoded", '<div><img src="data:image/svg+xml,%3Csvg%2F%3E"/></div>',
             f"![](_attachments/{short_hash(b'<svg/>')}.svg)"),
            ("unreadable data is dropped",
             '<div>a<img src="data:image/png;base64,abc"/></div>', "a"),
            ("no data is dropped", '<div>a<img src="data:image/png;base64,"/></div>', "a"),
            ("not a data address is dropped",
             '<div>a<img src="data:nothing"/></div>', "a"),
        ])


class TestCode(ExportTest):
    """Evernote code blocks, <pre> and code pasted from an editor become fenced code."""

    def test_code(self):
        self.check_bodies([
            ("Evernote code block with its language",
             '<div style="--en-codeblock:true; --en-syntaxLanguage:cs;"><div>int x = 1;</div>'
             "<div>&nbsp;&nbsp;&nbsp; y();</div></div>", "```csharp\nint x = 1;\n    y();\n```"),
            ("plain text code block has no language",
             '<div style="--en-codeblock:true; --en-syntaxLanguage:plaintext;"><div>public class A { }</div></div>',
             "```\npublic class A { }\n```"),
            ("pre", "<pre>raw   text\n  indented</pre>",
             "```\nraw   text\n  indented\n```"),
            ("an empty pre is dropped", "<div>a</div><pre>  </pre>", "a"),
            ("page parts that aren't text are dropped from code",
             f"<pre>a<style>p {{ }}</style>b</pre><div><font {CONSOLAS}>c<script>d</script></font></div>",
             "```\nab\nc\n```"),
            ("pasted lines in a monospaced font",
             f'<div>Before</div><div><font {CONSOLAS}><font color="#0000FF">int</font> x = 1;</font></div>'
             f"<div><font {CONSOLAS}><br/></font></div><div><font {CONSOLAS}>&nbsp;&nbsp;&nbsp; y();</font></div>"
             "<div>After</div>", "Before\n\n```\nint x = 1;\n\n    y();\n```\n\nAfter"),
            ("an empty line in the code's font after it is a typed blank line, not code",
             f"<div><font {CONSOLAS}>a</font></div><div><font {CONSOLAS}><br/></font></div><div>b</div>",
             "```\na\n```\n\n&nbsp;\nb"),
            ("so is one before it",
             f"<div>a</div><div><font {CONSOLAS}><br/></font></div><div><font {CONSOLAS}>c</font></div>",
             "a\n&nbsp;\n&nbsp;\n\n```\nc\n```"),
            ("pasted lines with spaces between them",
             f"<div><font {CONSOLAS}>a</font></div>\n<div><font {CONSOLAS}>b</font></div>", "```\na\nb\n```"),
            ("Courier lines", "<div><span style=\"font-family: 'Courier New';\">a</span></div>"
                              "<div><span style=\"font-family: 'Courier New';\">&nbsp; b</span></div>", "```\na\n  b\n```"),
            ("a font name whose quotes are stored encoded",
             '<div style="font-family: &amp;apos;Courier New&amp;apos;;">&lt;h1&gt;</div>'
             '<div style="font-family: &amp;apos;courier new&amp;apos;, courier, monospace;">&nbsp; x</div>'
             '<div style="font-family: &amp;apos;Courier New&amp;apos;;">&lt;/h1&gt;</div>',
             "```html\n<h1>\n  x\n</h1>\n```"),
            ("one monospaced block with line breaks",
             '<div style="font-family: Consolas;">f(<br/>\n    x)</div>', "```\nf(\n    x)\n```"),
            ("old indent inside code becomes spaces",
             f"<div><font {CONSOLAS}>class A</font></div><div><font {CONSOLAS}>{{</font></div>"
             f"<blockquote {OLD_INDENT}><div><font {CONSOLAS}>int x;</font></div></blockquote>"
             f"<div><font {CONSOLAS}>}}</font></div>", "```\nclass A\n{\n    int x;\n}\n```"),
            ("code all inside an indent starts at the margin",
             f"<blockquote {OLD_INDENT}><div><font {CONSOLAS}>a</font></div><div><font {CONSOLAS}>b</font></div></blockquote>",
             "```\na\nb\n```"),
            ("backticks in code get a longer fence",
             "<pre>a ``` b</pre>", "````\na ``` b\n````"),
        ])

    def test_pieces_of_code_one_after_the_other(self):
        """Code pasted as lines in a monospaced font, before Evernote had code blocks: pieces
        with only blank lines between them are one piece, a divider between them parts them."""
        def code(text, font=CONSOLAS):
            return "".join(f"<div><font {font}>{line}</font></div>" for line in text.split("\n"))
        blank, divider = "<div><br/></div>", "<div><br/></div><div><hr/></div><div><br/></div>"
        courier = 'face="Courier New"'
        self.check_bodies([
            ("only blank lines between them: one piece, with the blank line",
             code("a = 1;") + blank + code("b = 2;"), "```\na = 1;\n\nb = 2;\n```"),
            ("a divider between them: two pieces, and no divider",
             code("a = 1;") + divider + code("b = 2;"), "```\na = 1;\n```\n\n```\nb = 2;\n```"),
            ("a divider between blank lines in the code font: two pieces, nothing between",
             code("a = 1;\n<br/>") + "<div><hr/></div>" +
             code("<br/>\nb = 2;"),
             "```\na = 1;\n```\n\n```\nb = 2;\n```"),
            ("in two different fonts: two pieces",
             code("a = 1;") + blank + code("b = 2;", courier), "```\na = 1;\n```\n\n```\nb = 2;\n```"),
            ("in two clearly different languages: two pieces",
             code("SELECT name FROM users\nWHERE id = 1") + blank +
             code("function load(x) {\n    console.log(x);\n}"),
             "```sql\nSELECT name FROM users\nWHERE id = 1\n```\n\n```javascript\nfunction load(x) {\n    console.log(x);\n}\n```"),
            ("one piece too short to tell its language is joined to the other",
             code("import { A } from './a';") + blank +
             code("function load(x) {\n    console.log(x);\n}"),
             "```javascript\nimport { A } from './a';\n\nfunction load(x) {\n    console.log(x);\n}\n```"),
            ("with whitespace between the lines, as Evernote writes it",
             f"<div><font {CONSOLAS}>a = 1;</font> </div>\n" + blank + "\n" + code("b = 2;"), "```\na = 1;\n\nb = 2;\n```"),
            ("a blank line in the code's font between text is a blank line",
             f"<div>a</div><div><font {CONSOLAS}><br/></font></div><div>b</div>", "a\n&nbsp;\n&nbsp;\n\nb"),
            ("text after a blank line ends the piece", code("a = 1;") + blank + "<div>text</div>",
             "```\na = 1;\n```\n\n&nbsp;\ntext"),
        ])

    def test_code_that_lost_its_indent(self):
        """Lines of code nested in a block but no deeper than the line that opens it get one
        level deeper; every other line keeps its indent, as typed."""
        def block(code, language=""):
            recorded = f"--en-syntaxLanguage:{language};" if language else ""
            return f"<div style=\"-en-codeblock:true;{recorded}\">" + "".join(
                f"<div>{html.escape(line) or '<br/>'}</div>" for line in code.split("\n")) + "</div>"
        cases = [
            ("markup", block('<xsl:template match="/">\n...\n<xsl:apply-templates select="//a"/>\n</xsl:template>'),
             'xml\n<xsl:template match="/">\n    ...\n    <xsl:apply-templates select="//a"/>\n</xsl:template>'),
            ("braces, however deep", block("class A {\nvoid f() {\nif (x) {\ny();\n}\n}\n}", "cs"),
             "csharp\nclass A {\n    void f() {\n        if (x) {\n            y();\n        }\n    }\n}"),
            ("an indented block keeps its indent, even in its own style",
             block("function f() {\n  if (x) {\n      y();\n  }\n}", "js"),
             "javascript\nfunction f() {\n  if (x) {\n      y();\n  }\n}"),
            ("lines moved with the line that opens their block keep their place in it",
             block("var a = {\nb: function () {\n    c();\n}\n};", "js"),
             "javascript\nvar a = {\n    b: function () {\n        c();\n    }\n};"),
            ("braces in strings and comments don't count", block('function f() {\nvar s = "{"; // {\ny();\n}', "js"),
             'javascript\nfunction f() {\n    var s = "{"; // {\n    y();\n}'),
            ("tags that close themselves or never close don't open a block",
             block("<div>\n<br>\n<img src=\"a\"/>\n<p>a</p>\n</div>"),
             "html\n<div>\n    <br>\n    <img src=\"a\"/>\n    <p>a</p>\n</div>"),
            ("code that kept its indent is kept as typed, whatever its style",
             block("<html>\n<head>\n    <title>a</title>\n</head>\n<body>\n    <p>a</p>\n    <p>b</p>\n</body>\n</html>"),
             "html\n<html>\n<head>\n    <title>a</title>\n</head>\n<body>\n    <p>a</p>\n    <p>b</p>\n</body>\n</html>"),
            ("a brace opening a function passed to a call may sit as deep as the call",
             block("void f() {\ng(x =>\n{\ny();\n});\n}", "cs"),
             "csharp\nvoid f() {\n    g(x =>\n    {\n        y();\n    });\n}"),
            ("a quote with no closing one is just a character, a closing tag with a space still closes",
             block('[Route(“a")]\nclass A {\nint b;\n}',
                   "cs") + block('<a>\nx\n</ a>\n<b/>'),
             'csharp\n[Route(“a")]\nclass A {\n    int b;\n}\n```\n\n```html\n<a>\n    x\n</ a>\n<b/>'),
            ("Razor's braces among the tags of a page",
             block(
                 "<ul>\n@{\nforeach(var a in Model)\n{\n<li>\n@a.Name\n</li>\n}\n}\n</ul>"),
             "html\n<ul>\n    @{\n        foreach(var a in Model)\n        {\n            <li>\n                @a.Name\n"
             "            </li>\n        }\n    }\n</ul>"),
            ("a comment over several lines is lined up with its <!--",
             block(
                 "<p>\n    <!--\n        a\n          b\n        -->\n    <!-- one line -->\n</p>"),
             "html\n<p>\n    <!--\n    a\n      b\n    -->\n    <!-- one line -->\n</p>"),
            ("however deep its --> already is",
             block('<?xml version="1.0"?>\n<!--\n  a\n    b\n-->\n<c/>'),
             'xml\n<?xml version="1.0"?>\n<!--\na\n  b\n-->\n<c/>'),
            ("<head> and <body> may sit as deep as <html>",
             block("<html>\n<head>\n<title>a</title>\n</head>\n</html>"),
             "html\n<html>\n<head>\n    <title>a</title>\n</head>\n</html>"),
            ("a line ending with a closing bracket isn't lined up with the opening one",
             block("void f() {\ng(a,\n    b);\n}", "cs"), "csharp\nvoid f() {\n    g(a,\n        b);\n}"),
            ("blank lines and comments, even with braces, over several lines",
             block("void f() {\n/* a {\nb */\n\ng();\n}", "cs"), "csharp\nvoid f() {\n    /* a {\n    b */\n\n    g();\n}"),
            ("a closing bracket with no opening one closes nothing", block("void f() {\ng();\n}\n}\nh();", "cs"),
             "csharp\nvoid f() {\n    g();\n}\n}\nh();"),
            ("a brace under a call, shallower than it, comes as deep as the call",
             block("void f() {\n    g(x =>\n{\ny();\n});\n}", "cs"), "csharp\nvoid f() {\n    g(x =>\n    {\n        y();\n    });\n}"),
            ("a comment never closed is left as it is", block("<div>\n<p>\nx\n</p>\n<!--\nopen"),
             "html\n<div>\n    <p>\n        x\n    </p>\n    <!--\n    open"),
            ("a language with no braces or tags is kept as typed", block("SELECT a\nFROM b\nWHERE c = 1"),
             "sql\nSELECT a\nFROM b\nWHERE c = 1"),
        ]
        self.check_bodies(
            [(case, content, f"```{expected}\n```") for case, content, expected in cases])

    def test_code_in_a_list_item(self):
        """A line of a list item that is all code is a code block in it, with an empty line
        between it and the item's text, and two below the text, before the next item."""
        self.check_bodies([
            ("lines written with line breaks, however deep in formatting",
             f"<ul><li><span><font {CONSOLAS}>a();</font><font><font {CONSOLAS}>b();<br/></font></font>"
             "<span><br/>\nText.<br/><br/></span></span></li><li>next</li></ul>",
             "- ```\n  a();b();\n  ```\n  &nbsp;\n  Text.\n  &nbsp;\n  &nbsp;\n- next"),
            ("lines written as blocks, with no empty lines typed",
             f"<ul><li><div><font {CONSOLAS}>a();</font></div><div>Text.</div></li>"
             f"<li><div>Before:</div><div><font {CONSOLAS}>b();</font></div></li></ul>",
             "- ```\n  a();\n  ```\n  &nbsp;\n  Text.\n  &nbsp;\n  &nbsp;\n- Before:\n  &nbsp;\n  ```\n  b();\n  ```"),
            ("an item that is only code has one below it",
             f"<ul><li><font {CONSOLAS}>a();</font></li><li>next</li></ul>", "- ```\n  a();\n  ```\n  &nbsp;\n- next"),
            ("code in a sentence stays in it", f"<ul><li>call <font {CONSOLAS}>a()</font> first</li></ul>",
             "- call `a()` first"),
        ])

    def test_page_and_its_script(self):
        """A run of code lines is parted at an empty line between a page's markup and its
        script; a Razor view's C# and HTML, or C# and a LINQ query, stay one piece."""
        def code(text):
            return "".join(f"<div><font {CONSOLAS}>{html.escape(line) or '<br/>'}</font></div>" for line in text.split("\n"))
        self.check_bodies([
            ("markup, then its script",
             code('<div ng-app="app">\n    <p>{{title}}</p>\n</div>\n\nangular.module("app").controller("c", function ($scope) {\n'
                  '    $scope.title = "a";\n});'),
             '```html\n<div ng-app="app">\n    <p>{{title}}</p>\n</div>\n```\n\n```javascript\n'
             'angular.module("app").controller("c", function ($scope) {\n    $scope.title = "a";\n});\n```'),
            ("pieces in two fonts, with an empty line in the code font between",
             f'<div><font {CONSOLAS}>a = 1;</font></div><div><font {CONSOLAS}><br/></font></div>'
             '<div><font face="Courier New">b = 2;</font></div>', "```\na = 1;\n```\n\n```\nb = 2;\n```"),
            ("a Razor view", code("@model Shop.Models.Product\n\n<!DOCTYPE html>\n<html>\n<body>\n    <p>@Model.Name</p>\n</body>\n</html>"),
             "```\n@model Shop.Models.Product\n\n<!DOCTYPE html>\n<html>\n<body>\n    <p>@Model.Name</p>\n</body>\n</html>\n```"),
        ])

    def test_list_of_sections(self):
        """When an item of a list spans several lines or holds code, every item is a section:
        two empty lines below each, before the next, one below code."""
        self.check_bodies([
            ("an item over several lines spaces them all",
             "<ul><li>S: one<br/>what it means</li><li>O: two</li><li>L: three<br/>more</li></ul>",
             "- S: one\n  what it means\n  &nbsp;\n  &nbsp;\n- O: two\n  &nbsp;\n  &nbsp;\n- L: three\n  more"),
            ("an item holding code spaces them all",
             f"<ul><li>Not done</li><li><font {CONSOLAS}>due &lt;= now()</font></li><li>Not sent</li></ul>",
             "- Not done\n  &nbsp;\n  &nbsp;\n- ```\n  due <= now()\n  ```\n  &nbsp;\n- Not sent"),
            ("a list of one-line items stays tight",
             "<ul><li>a</li><li>b</li></ul>", "- a\n- b"),
            ("a sub-list doesn't make an item a section", "<ul><li>a<ul><li>x</li><li>y</li></ul></li><li>b</li></ul>",
             "- a\n  - x\n  - y\n- b"),
        ])

    def test_code_wrapped_in_plain_lines(self):
        """Lines that only wrap other lines don't keep pieces of code apart."""
        self.check_bodies([
            ("a piece inside a wrapping line is the next piece's neighbour",
             f"<div><font {CONSOLAS}>a = 1;</font></div><div><br/></div>"
             f"<div><div><font {CONSOLAS}>b = 2;</font></div><div>text</div></div>",
             "```\na = 1;\n\nb = 2;\n```\n\ntext"),
            ("a piece split across wrapping lines and an indent is one piece",
             f"<div><div><div><font {CONSOLAS}>x {{</font></div></div></div>"
             f"<blockquote><div><font {CONSOLAS}>y();</font></div></blockquote><div><font {CONSOLAS}>}}</font></div>",
             "```\nx {\n    y();\n}\n```"),
            ("preformatted pieces are pieces of code too, an empty one showing nothing, and one holding only ... stands apart",
             f"<pre {CONSOLAS}>a = [\n    1\n];\n</pre><pre {CONSOLAS}>...\n</pre><pre {CONSOLAS}></pre>"
             f"<div><font {CONSOLAS}>b = 2;</font></div>",
             "```\na = [\n    1\n];\n\n...\n\nb = 2;\n```"),
            ("a ... among the lines of a piece is left as it is",
             f"<pre {CONSOLAS}>f();\n...\ng();</pre>", "```\nf();\n...\ng();\n```"),
            ("an empty preformatted line is a blank line between pieces",
             f"<div><font {CONSOLAS}>a = 1;</font></div><pre {CONSOLAS}><span><br/></span></pre><pre {CONSOLAS}></pre>"
             f"<div><font {CONSOLAS}>b = 2;</font></div>",
             "```\na = 1;\n\nb = 2;\n```"),
            ("text in a wrapping line starts a line of its own",
             "<div><span>a</span><div><span><br/></span><div>b</div></div></div>",
             "a\n&nbsp;\n&nbsp;\n\nb"),
            ("code left loose between lines is a line of the code",
             f"<div><font {CONSOLAS}>a = {{</font></div><blockquote style=\"border: none;\">"
             f"<font {CONSOLAS}>b: 1</font></blockquote><span style=\"font-family: Consolas;\">}};</span>"
             f"<div><font {CONSOLAS}><br/></font></div><div>text</div>",
             "```\na = {\n    b: 1\n};\n```\n\n&nbsp;\ntext"),
            ("a word in another font glued to code is part of it",
             f'<div><font color="#0000FF" {CONSOLAS}>string</font> css<font {CONSOLAS}>Source = read();</font></div>'
             f"<div>css<font {CONSOLAS}>Out = f(</font>css<font {CONSOLAS}>Source);</font></div>",
             "```\nstring cssSource = read();\ncssOut = f(cssSource);\n```"),
            ("but a word with spaces around it is text", f"<div>call <font {CONSOLAS}>f()</font> now</div>",
             "call `f()` now"),
            ("nor is a word on a line of its own", f"<blockquote><div>q</div><div><font {CONSOLAS}>x();</font></div></blockquote>",
             "q\n\n```\nx();\n```"),
            ("a wrapping line with a colour keeps it", f'<div style="color:{RED};"><div>a</div><div>b</div></div>',
             f"{RED_SPAN}a</span>\n\n{RED_SPAN}b</span>"),
        ])

    def test_inline_code(self):
        self.check_bodies([
            ("monospaced word in a sentence", '<div>Call <span style="font-family: Consolas;">foo()</span> now</div>',
             "Call `foo()` now"),
            ("coloured tokens join into one piece",
             f'<ul><li><font color="#2B91AF" {CONSOLAS}>Action</font><font {CONSOLAS}>&lt;</font>'
             f'<font color="#0000FF" {CONSOLAS}>int</font><font {CONSOLAS}>&gt; cb;</font> declares it</li></ul>',
             "- `Action<int> cb;` declares it"),
            ("tokens with spaces between them join into one piece",
             "<div>x <code>a</code> <code>b</code> y</div>", "x `a b` y"),
            ("a space after joined tokens stays outside them",
             "<div>x <code>a</code><code>b</code> </div>", "x `ab`"),
            ("pieces at different nesting levels join",
             f'<div>x <span><font {CONSOLAS}>a</font></span><font {CONSOLAS}>b</font> y</div>', "x `ab` y"),
            ("code tag", "<div>use <code>x</code></div>", "use `x`"),
            ("backticks in code get longer ticks",
             "<div>x <code>a`b</code></div>", "x ``a`b``"),
            ("an empty code tag is plain space",
             "<div>a<code> </code>b</div>", "a b"),
        ])

    def test_touching_pieces_of_code_join(self):
        for text, expected in [("`a``b`", "`ab`"), ("`a``b``c` and `d`", "`abc` and `d`"),
                               ("`a` `b`", "`a` `b`"), ("```\ncode\n```", "```\ncode\n```")]:
            with self.subTest(text):
                self.assertEqual(enex2md.join_touching_code(text), expected)


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
            ("the note's other code blocks are JavaScript", "Notes", strong_js + "<div>text</div>" + weak_js,
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


class TestLanguageFromTags(ExportTest):
    """A note's tags name its language as its title does."""

    def test_markup_hint_needs_markup(self):
        """A title naming HTML or XML doesn't count for code with no markup outside its strings:
        HTML is then what the note is about, not what the code is written in."""
        for case, title, code, tags, expected in [
            ("code handling HTML in a note about it", "Decodificare HTML",
             '<pre>System.Net.WebUtility.HtmlDecode(text.Replace("&lt;p>", ""))</pre>', ("Develop - ASPX",), "```csharp"),
            ("a tag in a note about XML", "Chiavi XML", '<pre>&lt;xsl:key name="k" match="//e" use="id"/></pre>',
             ("Develop - XSLT",), "```xml"),
            ("an attribute of a tag in a note about XML", "Chiavi XML", "<pre>select=\"key('k', $id)\"></pre>",
             ("Develop - XSLT",), "```xml"),
        ]:
            with self.subTest(case):
                self.setUp()
                notes = self.export(note_xml(title, code, tags=tags))
                self.assertEqual(
                    body_of(notes[f"{title}.md"]).split("\n")[0], expected)

    def test_tags(self):
        csharp_or_js = '<pre>var id = new Regex("Pot(\\d+)").Matches(text)[0];</pre>'
        js_clue = "<pre>const id = text.match(pattern)[0];</pre>"
        for case, code, tags, expected in [
            ("no tag naming a language", csharp_or_js, ("Develop - Regex",), "```"),
            ("a C# tag tips code that could be either",
             csharp_or_js, ("Develop - C#",), "```csharp"),
            ("a JavaScript tag, with one clue of it in the code",
             js_clue, ("Develop - Javascript",), "```javascript"),
            ("a tag can't overrule the code", csharp_or_js,
             ("Develop - Javascript",), "```"),
            ("else { isn't a CSS selector, nor a // comment a command",
             "<pre>//Position is an object\nf = function (p) {\n    if (p) {\n        go();\n    }\n    else {\n        stop();\n    }\n};</pre>",
             ("Develop - CSS", "Develop - Javascript"), "```javascript"),
            ("LINQ's .Where( isn't SQL", "<pre>items.Where(x => x.On).ToList();</pre>",
             ("Develop - C#",), "```csharp"),
            ("with no clue in the code, the tag's language", "<pre>System.Environment.MachineName</pre>",
             ("Develop - ASPX",), "```csharp"),
            ("but not when the code is something else",
             "<pre>sails lift</pre>", ("Develop - Javascript",), "```bash"),
            ("nor when the tags name two languages", "<pre>doSomething(x);</pre>",
             ("Develop - CSS", "Develop - Javascript"), "```"),
        ]:
            with self.subTest(case):
                self.setUp()
                notes = self.export(
                    note_xml("Espressioni regolari", code, tags=tags))
                self.assertEqual(
                    body_of(notes["Espressioni regolari.md"]).split("\n")[0], expected)


class TestLanguageGuess(unittest.TestCase):
    """A language is named only when the code clearly shows it."""

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
            ("braces that aren't JSON", "{\n    console.log(x);\n}", ""),
            ("YAML", "services:\n  db:\n    image: postgres\n    ports:\n      - 5432:5432", "yaml"),
            ("ASPX directive", '<%@ Reference Control="WebUserControl.ascx" %>', "xml"),
            ("ASPX data binding", "SelectedValue='<%# Eval(\"id\") %>'", "xml"),
            ("ASPX controls, with an HTML tag among them",
             '<asp:Button ID="b" runat="server" />\n<hr />\n<asp:SqlDataSource ID="s" runat="server" />', "xml"),
            ("config with no closing tag in the snippet",
             "<configuration>\n  <system.webServer>", "xml"),
            ("XSLT around an HTML tag",
             '<div>\n  <xsl:attribute name="class">a</xsl:attribute>\n</div>', "xml"),
            ("an XML file with comments",
             '<?xml version="1.0"?>\n<!-- a -->\n<configuration/>', "xml"),
            ("an HTML template with EJS", "<h1>\n<%- title %>\n</h1>", "html"),
            ("the end of a page, from a closing tag", "</body>\n</html>", "html"),
            ("the end of a config, from a closing tag",
             "</rewrite>\n</system.WebServer>", "xml"),
            ("HTML attributes",
             "ng-class=\"{'even': $index % 2 == 0}\"", "html"),
            ("a terminal session",
             "Mac:Git me$ mkdir Test\nMac:Git me$ cd Test/\nThe template was created.", "bash"),
            ("commands of other tools", "sails new myApp\nnuget restore", "bash"),
            ("a program run by its path",
             "node_modules/eslint/bin/eslint.js --init", "bash"),
            ("a JSON fragment",
             '"JwtAuthentication": {\n  "Key": "x"\n}', "json"),
            ("a JSON fragment trimmed by hand",
             '"Logging": {\n  "Level": "Info",\n}', "json"),
            ("YAML with placeholders",
             "...\n\nsteps:\n  - task: UseDotNet@2\n    inputs:\n      version: '3.1'\n\n...", "yaml"),
            ("YAML with expressions",
             "name: Build\non: push\njobs:\n  build:\n    env:\n      KEY: ${{ secrets.KEY }}", "yaml"),
            ("YAML with a value over several lines, when nothing else fits",
             "trigger:\n- master\nsteps:\n- script: >\n    dotnet publish\n    --output $(dir)\n  displayName: 'build'", "yaml"),
            ("but only the lines under it",
             "steps:\n- script: >\n    dotnet publish\nthen deploy it by hand", ""),
            ("a Markdown badge",
             "[![Nuget](https://img.shields.io/nuget/v/X)](https://www.nuget.org/packages/X)", "markdown"),
            ("too little to tell", "x = y + 1", ""),
            ("plain text", "Hello there, this is a note.", ""),
        ]
        for case, code, expected in cases:
            with self.subTest(case):
                self.assertEqual(guess_language(code), expected)

    def test_attributes_of_xml(self):
        """Attributes are HTML's, unless the note's title or tags name XML, as XSLT's are."""
        self.assertEqual(guess_language('select="//nodeName"'), "html")
        self.assertEqual(guess_language(
            'select="//nodeName"', ("xml",)), "xml")

    def test_recorded_language(self):
        for style, expected in [("--en-syntaxlanguage:js", "javascript"), ("--en-syntaxlanguage:ruby", "ruby"),
                                ("--en-syntaxlanguage:plaintext", ""), ("-en-codeblock:true", None)]:
            with self.subTest(style):
                self.assertEqual(enex2md.recorded_language(style), expected)

    def test_title_hints(self):
        for title, expected in [("Gradients CSS3", ("css",)), ("Angular JS - Directives", ("javascript",)),
                                ("Net Core - Options", ("csharp",)), ("XSLT basics", ("xml",)), ("Holidays", ())]:
            with self.subTest(title):
                self.assertEqual(enex2md.title_languages(title), expected)


if __name__ == "__main__":
    unittest.main()
