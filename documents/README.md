# documents/: what profile expansion reads

`python -m src.expand` reads every file in this folder, plus your public
GitHub repositories, and asks the local model what you have done that
`cv/base_cv.md` does not show. It only proposes: the result is
`output/profile_expansion.md`, each proposal with a verbatim quote and the
file it came from. Nothing in `cv/` is changed.

These files are personal. Only this README is meant to be committed; check
`git status` before you commit anything.

## What to drop here

- **Old CVs and cover letters**: earlier versions often name tools and
  projects that were cut for space.
- **Thesis, papers and project write-ups**: methods, datasets, results.
- **Your LinkedIn data export** (LinkedIn > Settings > Data privacy > Get a
  copy of your data). Unzip it here; the useful files are `Profile.csv`,
  `Positions.csv`, `Projects.csv`, `Skills.csv`, `Certifications.csv`,
  `Education.csv`, `Courses.csv`, `Honors.csv`, `Publications.csv` and
  `Recommendations_Received.csv`. Connections, messages and ad data are
  skipped.
- **Certificates and course transcripts**: names, issuers, dates, modules.
- **Performance reviews and reference letters**: what others say you did.

Subfolders are fine (`linkedin/`, `old_cvs/`, …).

## Formats

- `.md`, `.txt` and `.csv` are read as text.
- `.pdf` is read with `pdftotext` (`brew install poppler`). Without it, PDFs
  are skipped with a note. A scanned PDF has no text; run OCR on it first.
- Anything else (`.docx`, `.pages`, images, `.zip`) is skipped: save it as
  PDF or text, and unzip archives.

## Limits

The local model reads about 24,000 characters in one go, CV included. Each
file is capped at 8,000 characters and the budget is shared between files,
so a short note is never crowded out by a long export. Anything cut is listed
as truncated at the bottom of the output. Put the documents that matter most
here and leave the rest out.
