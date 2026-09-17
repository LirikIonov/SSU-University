from pathlib import Path
from urllib.parse import quote

import mkdocs_gen_files


LECTURES_DIR = Path("go/lectures")
SLIDES_DIR = Path("go/slides")


def encode_path(path: Path) -> str:
    return "/".join(quote(part) for part in path.parts)


def get_title(path: Path) -> str:
    """
    Берём первый H1 из Markdown.
    Если его нет — используем имя файла.
    """
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
    out.write("- [Лекции](go/lectures/)\n")
    out.write("- [Презентации](go/slides/)\n")


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

with mkdocs_gen_files.open(
    "go/lectures/index.md",
    "w",
) as out:
    out.write("# Лекции\n\n")

    if not lecture_files:
        out.write("Лекции не найдены.\n")
    else:
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

with mkdocs_gen_files.open(
    "go/slides/index.md",
    "w",
) as out:
    out.write("# Презентации\n\n")

    if not pdf_files:
        out.write("PDF-файлы не найдены.\n")
    else:
        for pdf in pdf_files:
            title = pdf.stem
            url = quote(pdf.name)

            out.write(f"- [{title}](./{url})\n")