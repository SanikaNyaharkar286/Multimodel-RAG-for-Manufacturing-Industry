import unittest

from google.cloud import documentai
from scripts.extract_qr_links import read_qr_results, valid_url


class DocumentAiQrTests(unittest.TestCase):
    def test_decoded_urls_keep_original_manual_provenance(self):
        barcode = documentai.Document.Page.DetectedBarcode(
            barcode=documentai.Barcode(format='QR_CODE', raw_value='https://example.com/video'),
        )
        document = documentai.Document(pages=[documentai.Document.Page(detected_barcodes=[barcode, barcode])])
        rows = read_qr_results(document, 'HSG-2550', 'Manual.pdf', 7)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['page_number'], 7)
        self.assertEqual(rows[0]['origin'], 'document_ai_qr')

    def test_no_qr_result_and_unsafe_urls(self):
        self.assertEqual(read_qr_results(documentai.Document(), 'HSG-2550', 'Manual.pdf', 1), [])
        self.assertIsNone(valid_url('javascript:alert(1)'))
        self.assertIsNone(valid_url('https://user:secret@example.com'))
