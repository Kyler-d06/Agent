import json

import pytest


pytest.importorskip("docx")
pytest.importorskip("openpyxl")
pytest.importorskip("reportlab")
pytest.importorskip("fitz")
pytest.importorskip("PIL")


def test_pdf_creation_reading_and_actual_page_render(core):
    docs = core.universal_platform.documents
    artifact = docs.document_create("report.pdf", title="Quarterly Results", paragraphs=["The total is 42."], sheets={"Values": [["Label", "Value"], ["Zero", 0]]})
    assert core.universal_platform.work.verify_artifact(artifact["id"])["verified"]
    read = docs.document_read("report.pdf")
    assert "The total is 42" in read["text"]
    assert "0" in read["text"]
    rendered = docs.document_render("report.pdf", "preview")
    assert rendered["all_pages_rendered"]
    from PIL import Image
    for page in rendered["pages"]:
        with Image.open(core.universal_platform.work.root / page["path"]) as image:
            assert image.width > 500 and image.height > 500
        assert core.universal_platform.work.verify_artifact(page["id"])["verified"]


def test_docx_and_spreadsheet_roundtrip(core):
    docs = core.universal_platform.documents
    docs.document_create("memo.docx", title="Decision", paragraphs=["Use the measured result."], sheets={"Checks": [["Case", "Result"], ["Example", True]]})
    memo = docs.document_read("memo.docx")
    assert "Use the measured result." in memo["paragraphs"]
    assert memo["tables"][0][1] == ["Example", "True"]
    result = docs.document_create("budget.xlsx", sheets={"Budget": [["Amount"], [10], [20], ["=SUM(A2:A3)"]]})
    assert "recalculation" in result["formula_values"]
    workbook = docs.document_read("budget.xlsx")
    assert workbook["sheets"]["Budget"][3][0] == "=SUM(A2:A3)"
    with pytest.raises(ValueError):
        docs.document_create("budget.xlsx", sheets={"Other": [[1]]})


def test_vision_review_requires_vision_provider_and_sends_image(core, monkeypatch):
    from PIL import Image
    root = core.universal_platform.work.root
    Image.new("RGB", (10, 10), "white").save(root / "page.png")
    observed = {}
    def chat(messages, **kwargs):
        observed.update(kwargs)
        assert messages[0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
        return {"content": "No clipping visible."}
    monkeypatch.setattr(core.universal_platform.models, "chat", chat)
    result = core.universal_platform.documents.inspect_artifact_image("page.png")
    assert result["review"] == "No clipping visible."
    assert observed["required_capabilities"] == ["vision"]
