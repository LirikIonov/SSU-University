from pathlib import Path
from urllib.parse import quote

import mkdocs_gen_files


DOCS_DIR = Path(mkdocs_gen_files.config.docs_dir)

LECTURES_DIR = DOCS_DIR / "lectures"
SLIDES_DIR = DOCS_DIR / "slides"


def encode_path(path: Path) -> str:
    return "/".join(quote(part) for part in path.parts)


def get_title(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8") as file:
            for line in file:
                if line.startswith("# "):
                    return line[2:].strip()
    except OSError:
        pass

    return path.stem


# ---------------------------------------------------------------------------
# Главная
# ---------------------------------------------------------------------------

with mkdocs_gen_files.open("index.md", "w") as out:
    out.write("# Go — углублённое изучение\n\n")
    out.write("## Материалы\n\n")
    out.write("- [Лекции](lectures/)\n")
    out.write("- [Презентации](slides/)\n")


# ---------------------------------------------------------------------------
# Лекции
# ---------------------------------------------------------------------------

lecture_files = sorted(
    (
        path
        for path in LECTURES_DIR.rglob("*.md")
        if path.name != "index.md"
    ),
    key=lambda path: str(path).lower(),
)

with mkdocs_gen_files.open("lectures/index.md", "w") as out:
    out.write("# Лекции\n\n")

    for lecture in lecture_files:
        relative_path = lecture.relative_to(LECTURES_DIR)
        url = encode_path(relative_path)
        title = get_title(lecture)

        out.write(f"- [{title}]({url})\n")


# ---------------------------------------------------------------------------
# Презентации
# ---------------------------------------------------------------------------

pdf_files = sorted(
    SLIDES_DIR.glob("*.pdf"),
    key=lambda path: path.name.lower(),
)

with mkdocs_gen_files.open("slides/index.md", "w") as out:
    out.write("# Презентации\n\n")

    for pdf in pdf_files:
        title = pdf.stem
        page_name = f"{pdf.stem}.md"

        out.write(
            f"- [{title}]({quote(page_name)})\n"
        )


# ---------------------------------------------------------------------------
# Отдельная страница для каждого PDF
# ---------------------------------------------------------------------------

for pdf in pdf_files:
    title = pdf.stem
    page_name = f"{pdf.stem}.md"
    pdf_url = quote(pdf.name)

    with mkdocs_gen_files.open(
        Path("slides") / page_name,
        "w",
    ) as out:
        out.write(f"# {title}\n\n")
        out.write(f"[Открыть PDF](./{pdf_url})\n")