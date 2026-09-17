"""Opt-in real file parsers and OCR, using only generated local fixtures."""

from dataclasses import replace
import io
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
import zipfile

from backend.extensions.web_tools.asset_config import AssetConfig
from backend.extensions.web_tools.asset_common import AssetFailure, normalize
from backend.extensions.web_tools.asset_extract import extract
from backend.extensions.web_tools.asset_worker import run_isolated
from backend.extensions.web_tools.egress import BoundedHTTPServer, EgressHandler


class AssetFixtureProxy(EgressHandler):
    def do_GET(self):
        if urlsplit(self.path).hostname != "fixture.example.org":
            self._json(403, {"error": "No external requests in fixtures"})
            return
        self.server.calls += 1
        self.send_response(200)
        self.send_header("Content-Type", "text/csv")
        self.send_header("Content-Length", str(len(self.server.data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(self.server.data)
        self.close_connection = True


@unittest.skipUnless(os.environ.get("WEB_TOOLS_TEST_ASSETS") == "1", "opt-in: requires the crawler-assets image")
class AssetIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="web-assets-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = AssetConfig(documents_enabled=True, images_enabled=True, asset_max_chars=10000)

    def parse(self, data, filename, *, config=None, **options):
        config = config or self.config
        path = self.root / filename
        path.write_bytes(data)
        kind = "image" if filename.endswith(".png") else "document"
        request = normalize({"url": "https://fixture.example.org/" + filename, "kind": kind, **options}, config)
        media = "text/csv" if filename.endswith(".csv") else "application/octet-stream"
        return extract(path, request, {"final_url": request["url"], "content_type": media, "downloaded_bytes": len(data)},
                       config, "http://127.0.0.1:9")

    def text_pdf(self):
        from pypdf import PdfWriter
        from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
        writer = PdfWriter()
        for number in (1, 2, 3):
            page = writer.add_blank_page(width=612, height=792)
            font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
                                     NameObject('/BaseFont'): NameObject('/Helvetica')})
            page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
            stream = DecodedStreamObject()
            stream.set_data(f'BT /F1 18 Tf 40 700 Td (Report page {number}: revenue 12345 TRY) Tj ET'.encode())
            page[NameObject('/Contents')] = writer._add_object(stream)
        output = io.BytesIO()
        writer.write(output)
        return output.getvalue()

    def image(self):
        from PIL import Image, ImageDraw, ImageFont
        image = Image.new("RGB", (1000, 350), "white")
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 44)
        draw = ImageDraw.Draw(image)
        draw.text((40, 60), "BANK REPORT 2026", fill="black", font=font)
        draw.text((40, 140), "REVENUE 12345 TRY", fill="black", font=font)
        return image

    def test_pdf_text_preserves_page_locations_and_page_limits(self):
        result = self.parse(self.text_pdf(), "report.pdf", max_pages=1, start_page=2)
        self.assertEqual(result["total_pages"], 3)
        self.assertEqual(result["pages_processed"], 1)
        self.assertIn("Report page 2", result["content"])
        self.assertNotIn("Report page 1", result["content"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["sections"][0]["location"], "Page 2")

    def test_pdf_model_failure_keeps_native_text_and_metadata(self):
        from backend.model_clients.kloudeks import KloudeksClient, ModelFailure
        from backend.extensions.web_tools.asset_cache import AssetStore
        config = replace(self.config, vision_enabled=True, kloudeks_api_key="fixture-only")
        with patch.object(AssetStore, "consume_model_call"), patch.object(
                KloudeksClient, "interpret", side_effect=ModelFailure("model_timeout")):
            result = self.parse(self.text_pdf(), "report.pdf", config=config, max_pages=1, vision=True)
        self.assertEqual(result["status"], "partial")
        self.assertIn("revenue 12345 TRY", result["content"])
        self.assertEqual(result["model_calls"], 1)
        self.assertEqual(result["processing_errors"][0]["code"], "model_timeout")
        self.assertIsNone(result["error"])

    def test_pdf_table_retains_rows_and_source_page(self):
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import NameObject, DecodedStreamObject
        reader = PdfReader(io.BytesIO(self.text_pdf()))
        writer = PdfWriter()
        page = writer.add_page(reader.pages[0])
        content = DecodedStreamObject()
        content.set_data(b'''0.5 w 40 600 m 550 600 l S 40 550 m 550 550 l S 40 500 m 550 500 l S
40 600 m 40 500 l S 300 600 m 300 500 l S 550 600 m 550 500 l S
BT /F1 16 Tf 50 570 Td (Metric) Tj ET BT /F1 16 Tf 310 570 Td (Amount) Tj ET
BT /F1 16 Tf 50 520 Td (Revenue) Tj ET BT /F1 16 Tf 310 520 Td (12345 TRY) Tj ET''')
        page[NameObject('/Contents')] = writer._add_object(content)
        output = io.BytesIO()
        writer.write(output)
        result = self.parse(output.getvalue(), "table.pdf")
        tables = [section for section in result["sections"] if "rows" in section]
        self.assertEqual(tables[0]["rows"], [["Metric", "Amount"], ["Revenue", "12345 TRY"]])
        self.assertEqual(tables[0]["location"], "Page 1, table")

    def test_csv_and_xlsx_tables_respect_row_sheet_limits(self):
        from openpyxl import Workbook
        config = replace(self.config, asset_max_rows=2, asset_max_sheets=1)
        csv_result = self.parse('Ay;Tutar\nOcak;100\nŞubat;120\n'.encode(), "report.csv", config=config)
        self.assertEqual(csv_result["sections"][0]["rows"], [["Ay", "Tutar"], ["Ocak", "100"]])
        self.assertTrue(csv_result["truncated"])
        book = Workbook()
        sheet = book.active
        sheet.title = "Figures"
        for row in [["Month", "Amount"], ["January", 100], ["February", 120]]:
            sheet.append(row)
        book.create_sheet("Extra")
        output = io.BytesIO()
        book.save(output)
        result = self.parse(output.getvalue(), "report.xlsx", config=config)
        self.assertEqual(result["sections"][0]["rows"][1], ["January", "100"])
        self.assertEqual(len(result["sections"]), 1)
        self.assertTrue(result["truncated"])

    def test_docx_extracts_paragraphs_and_tables_without_running_content(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("word/document.xml", '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
            <w:p><w:r><w:t>Quarterly report</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Revenue</w:t></w:r></w:p></w:tc>
            <w:tc><w:p><w:r><w:t>12345 TRY</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>''')
        result = self.parse(output.getvalue(), "report.docx")
        self.assertIn("Quarterly report", result["content"])
        self.assertEqual(result["sections"][1]["rows"], [["Revenue", "12345 TRY"]])

    def test_legacy_xls_dates_and_row_limits(self):
        import base64
        import zlib
        # Generated with xlwt 1.3.0: Figures sheet, date/amount header,
        # 2026-03-31 / 12345, and one extra row. No runtime writer dependency.
        encoded = (
            "eNrtWE1oE0EU/maTND+0+amp0AolFKxa24N48dKulbQ9pVQv/iBoaoOUaiJrCurF2pqjIHhSvBTqwUvViz+oUG8ehBY9CIKQKHjxJCh4aLO+97KLSURoRAuV+Zb55u2b92ZeMjNvdndlOVacf9BRQh0G4EHZDqKpSqeoBN2bKKjdtll06wAVW2NTIRigiWzy4WnLKz/PIc93CQbue18QAx+oHMc5jOaymcQG4oDEkFYcQz+xwm3ShNEuUbUKnxLeInxPLJ8J7xfNNeF+si2qY1g2R3v2Oav4iNElbWFwv4/E551o9qANL3kVX76uKrY+DFqT6TObrKHT24wF0ISOZLIZi+8juIMQcJTQl0r1JZNFxGmyF/DNTgBf3V29lND6jdUrkP57rd7/G32AJrFef8PwAjOwT8omKSCCkJdb/BiePD1tZc6vkoOHFFyoJZnOZ2iTD57NTWfzPmDoQt5KhzizSyaI1mSCFtkhzcQT1DHLMdknUQpl9e6X16nxMfOEaGYk+1fOiO0cEmxcYQ9yDkuLR5hte4R3C89Kr9tE7hCO0yKmunuszRGG58TmqrR20zh7BW/MHVXyTpILnw8+7ix8NHeRvDhSuhRffGvOo4vOrAny52sOvapX3brJeGK6tXLyyXvh9l9yS8CIOrHbzkEYwRrvJkJMuHLH/45y7FWd/XMaQ0nmml0xyO7TEtuzpSGetT0b0jPzgNGKhyxQVvuJEDQ0NDQ0NDQ0NDT+BMp5JnfeDuiRu/Lk73e+66xRKevPJP8tDiFHV57eEYeQpdrCxYbWz1b4lNuXWqeP+72QcZhGtzCFcYljquH1S29uqvr3rNsx+ve2UKPjlxuJ8x+P/wO6jtJq"
        )
        result = self.parse(zlib.decompress(base64.b64decode(encoded)), "report.xls",
                            config=replace(self.config, asset_max_rows=2))
        self.assertEqual(result["sections"][0]["rows"][1], ["2026-03-31T00:00:00", "12345.0"])
        self.assertEqual(len(result["sections"][0]["rows"]), 2)
        self.assertEqual(result["status"], "partial")

    def test_image_and_scanned_pdf_ocr_are_local_and_bounded(self):
        config = replace(self.config, ocr_enabled=True, ocr_languages="eng", ocr_max_pages=1)
        with self.image() as image:
            png, pdf = io.BytesIO(), io.BytesIO()
            image.save(png, format="PNG")
            image.save(pdf, format="PDF")
        for data, name in ((png.getvalue(), "scan.png"), (pdf.getvalue(), "scan.pdf")):
            with self.subTest(name=name):
                result = self.parse(data, name, config=config)
                self.assertIn("12345", result["content"])
                self.assertIn("BANK REPORT", result["content"])
                self.assertEqual(result["ocr_pages"], 1)
                self.assertEqual(result["model_calls"], 0)
                self.assertTrue(any(section["method"] == "local_ocr" for section in result["sections"]))

    def test_archive_pixel_and_output_limits(self):
        with self.image() as image:
            output = io.BytesIO()
            image.save(output, format="PNG")
        with self.assertRaises(AssetFailure):
            self.parse(output.getvalue(), "large.png", config=replace(self.config, asset_max_image_pixels=10000))
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as stream:
            stream.writestr("word/document.xml", "x" * 100000)
        with self.assertRaises(AssetFailure):
            self.parse(archive.getvalue(), "bomb.docx")
        result = self.parse(self.text_pdf(), "report.pdf", max_chars=100)
        self.assertLessEqual(len(result["content"]), 100)
        self.assertTrue(result["truncated"])

    def test_mia_scanned_pdf_batches_and_call_pixel_limits(self):
        from PIL import Image
        from backend.extensions.web_tools.asset_cache import AssetStore
        from backend.model_clients.kloudeks import KloudeksClient
        config = replace(self.config, ocr_enabled=True, ocr_provider="kloudeks", ocr_max_pages=4,
                         kloudeks_api_key="fixture-only", asset_max_image_pixels=10000)
        with self.image() as image:
            output = io.BytesIO()
            image.save(output, format="PDF", save_all=True, append_images=[image, image, image])
        response = {"text": "Extracted report text", "model": config.kloudeks_ocr_model, "truncated": False}
        with patch.object(AssetStore, "consume_model_call") as budget, patch.object(
                KloudeksClient, "interpret", return_value=response) as model:
            result = self.parse(output.getvalue(), "scans.pdf", config=config)
        model.assert_called_once()
        budget.assert_called_once()
        images = model.call_args.args[0]
        self.assertEqual(len(images), 3)
        for data in images:
            with Image.open(io.BytesIO(data)) as image:
                self.assertLessEqual(image.width * image.height, 10000)
        self.assertEqual(result["ocr_pages"], 3)
        self.assertEqual(result["model_calls"], 1)
        self.assertEqual(result["sections"][0]["location"], "Page 1, Page 2, Page 3")
        self.assertEqual(result["status"], "partial")
        self.assertTrue(any("per-read model call limit" in warning for warning in result["warnings"]))

    def test_isolated_download_and_cache_avoid_repeated_network_requests(self):
        with BoundedHTTPServer(("127.0.0.1", 0), AssetFixtureProxy) as server:
            server.data, server.calls = b"Month,Amount\nJanuary,100\n", 0
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                env = {"WEB_DOCUMENTS_ENABLED": "true", "WEB_IMAGES_ENABLED": "false", "WEB_OCR_ENABLED": "false",
                       "WEB_VISION_ENABLED": "false", "WEB_ASSET_CACHE_ENABLED": "true", "WEB_ASSET_CACHE_DIR": str(self.root / "cache")}
                with patch.dict(os.environ, env):
                    config = AssetConfig.from_environ()
                    request = normalize({"kind": "document", "url": "http://fixture.example.org/report.csv"}, config)
                    proxy = f"http://127.0.0.1:{server.server_port}"
                    first = run_isolated(request, proxy, 30)
                    second = run_isolated(request, proxy, 30)
                    self.assertEqual(first["status"], "ok", first)
                    self.assertTrue(second["cache"]["hit"], second)
                    self.assertEqual(server.calls, 1)
                    fresh = run_isolated(request | {"refresh": True}, proxy, 30)
                    self.assertFalse(fresh["cache"]["hit"])
                    self.assertEqual(server.calls, 2)
            finally:
                server.shutdown()
                thread.join()


if __name__ == "__main__":
    unittest.main()
