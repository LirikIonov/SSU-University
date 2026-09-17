from pathlib import Path
from urllib.parse import quote

import mkdocs_gen_files


PHYSICAL_DIR = Path("memory/university/go/slides")
VIRTUAL_DIR = Path("university/go/slides")

pdf_files = sorted(
    PHYSICAL_DIR.glob("*.pdf"),
    key=lambda path: path.name.lower(),
)

with mkdocs_gen_files.open(
    VIRTUAL_DIR / "index.md",
    "w",
) as out:
    out.write("# Презентации\n\n")

    for pdf in pdf_files:
        title = pdf.stem
        url = quote(pdf.name)

        out.write(f"- [{title}](./{url})\n")