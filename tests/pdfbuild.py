"""A minimal PDF writer, so the PDF tests need no library and no network."""

from __future__ import annotations


def make_pdf(pages: list[list[str]], title: str | None = None) -> bytes:
    """One text object per line, laid out top to bottom on each page."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids: list[int] = []
    content_ids: list[int] = []
    for lines in pages:
        parts = [b"BT", b"/F1 11 Tf", b"72 720 Td", b"14 TL"]
        for line in lines:
            escaped = (
                line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
            ).encode("latin-1", "replace")
            parts.append(b"(" + escaped + b") Tj T*")
        parts.append(b"ET")
        stream = b"\n".join(parts)
        content_ids.append(
            add(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
        )
        page_ids.append(0)   # filled in once the Pages object has an id

    pages_id = len(objects) + len(pages) + 1
    for i, content in enumerate(content_ids):
        page_ids[i] = add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
            % (pages_id, font, content)
        )
    kids = b" ".join(b"%d 0 R" % pid for pid in page_ids)
    pages_obj = add(b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_ids)))
    assert pages_obj == pages_id, "page tree id must match what the pages referenced"

    info = b""
    info_id = 0
    if title:
        info_id = add(b"<< /Title (%s) >>" % title.encode("latin-1", "replace"))
        info = b" /Info %d 0 R" % info_id
    catalog = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)

    start = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root %d 0 R%s >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1, catalog, info, start,
    )
    return bytes(out)
