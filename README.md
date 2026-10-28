# EvernoteExtractor

Converts Evernote export files (`.enex`) into a folder of Markdown notes, ready
for Obsidian, a Git repository or any Markdown editor.

## Requirements

Python 3 (tested with 3.14). The script only uses the standard library, so
there is nothing to install.

## Export your notes from Evernote

In Evernote, right-click a notebook and choose **Export notebook…**, then pick
the **ENEX** format. Evernote writes one `.enex` file per notebook.

## Convert them

```sh
python3 src/enex2md.py MyNotebook.enex
```

This writes the notes to a new folder named after the file, next to it
(`MyNotebook/`).

More examples:

```sh
# several notebooks at once, each into its own folder next to its file
python3 src/enex2md.py Work.enex Personal.enex

# every .enex file in a folder
python3 src/enex2md.py ~/Downloads/evernote-export/

# choose the output folder: one subfolder per notebook
python3 src/enex2md.py ~/Downloads/evernote-export/ -o ~/Obsidian/Evernote

# put every note in the output folder, without subfolders
python3 src/enex2md.py ~/Downloads/evernote-export/ -o ~/Obsidian/Evernote --flat
```

| Option                  | Meaning                                                                                                         |
| ----------------------- | --------------------------------------------------------------------------------------------------------------- |
| `-o`, `--output FOLDER` | Folder to write the notes to. It must be new or empty.                                                          |
| `--flat`                | With `-o` and several `.enex` files, put every note in the output folder instead of one subfolder per notebook. |

The script never writes into a folder that already has files in it, and never
changes the `.enex` files. When it finishes, it prints how many notes and
attachments it wrote, plus any warnings.

## What you get

```
MyNotebook/
  Meeting notes.md
  Recipes.md
  attachments/
    photo.jpg
    invoice.pdf
```

- **One Markdown file per note**, named after its title. If two notes share a
  title, the second becomes `Title (2).md`.
- **Front matter** with the title, created and updated dates, tags, and the
  source URL, author and location when the note has them. Tags are made valid
  Obsidian tags, so `Work - Ideas` becomes `Work-Ideas`.
- **File dates** set to the note's: on macOS both the creation and
  modification dates, elsewhere the modification date.
- **Attachments** saved once each in `attachments/`. Pictures and PDFs are
  shown in the note; other files are links.
- **Links between notes** become relative links to the note's file. A link to
  a note that isn't in the export keeps its `evernote://` address.
- **Formatting** kept: headings, bold, italics, strikethrough, lists, to-dos,
  tables, quotes and dividers. Highlights become `==highlights==`; coloured
  and underlined text become small bits of HTML, which Obsidian shows.
- **Code** becomes fenced code blocks, with the language Evernote recorded or
  a guessed one. Monospaced text pasted from an editor is recognised as code
  too.
- **Encrypted text** can't be exported and is replaced by a note saying so.

The top of [src/enex2md.py](src/enex2md.py) explains the conversion choices in
more detail.

## Run the tests

```sh
python3 tests/test_enex2md.py
```
