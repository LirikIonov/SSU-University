from pathlib import Path
from urllib.parse import quote

import mkdocs_gen_files


SLIDES_DIR = Path("go/slides")

pdf_files = sorted(
    SLIDES_DIR.glob("*.pdf"),
    key=lambda path: path.name.lower(),
)


# Главная страница
with mkdocs_gen_files.open("index.md", "w") as out:
    out.write("# Go — углублённое изучение\n\n")
    out.write("## Материалы\n\n")
    out.write("- [Лекции](go/lectures/)\n")
    out.write("- [Презентации](go/slides/)\n")


# Страница презентаций
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