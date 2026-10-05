"""Generate labeled synthetic fixtures; not a production-representative corpus."""
import argparse
import hashlib
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def generate(output: Path) -> Path:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    font = ImageFont.load_default(size=28)
    for index in range(20):
        family = ("invoice", "receipt", "purchase_order")[index % 3]
        number = f"TEST-{index + 1:04d}"
        amount = f"{100 + index * 7}.00"
        common = ["SYNTHETIC TEST DOCUMENT", "Example Trading Company", family.upper().replace("_", " ")]
        if family == "invoice":
            lines = [f"Invoice Number: {number}", "Invoice Date: 2026-09-01", "Vendor Name: Example Trading Company"]
            fields = {"invoice_number": number, "total_amount": amount, "currency": "USD"}
        elif family == "receipt":
            lines = [f"Receipt Number: {number}", "Receipt Date: 2026-09-01", "Merchant Name: Example Trading Company", "Payment Method: Cash"]
            fields = {"total_amount": amount, "payment_method": "Cash"}
        else:
            lines = [f"Purchase Order Number: {number}", "Order Date: 2026-09-01", "Supplier Name: Example Trading Company", "Buyer Name: Example Buyer"]
            fields = {"purchase_order_number": number, "total_amount": amount}
        count = 2 if index < 5 else 1
        texts = [common + lines]
        totals = ["Currency: USD", f"Total Amount: {amount}"]
        if count == 2:
            texts.append(["SYNTHETIC TEST DOCUMENT", f"Reference: {number}", "Order summary"] + totals)
        else:
            texts[0].extend(totals)
        images = []
        for page, text in enumerate(texts, 1):
            text.append(f"Page {page} of {count}")
            image = Image.new("RGB", (1200, 1500), "white")
            draw = ImageDraw.Draw(image)
            for line, value in enumerate(text):
                draw.text((60, 60 + line * 55), value, font=font, fill="black")
            images.append(image)
        path = output / f"{family}-{index + 1:03d}{'.tiff' if count > 1 else '.png'}"
        if count > 1:
            images[0].save(path, save_all=True, append_images=images[1:], compression="tiff_deflate")
        else:
            images[0].save(path)
        rows.append({
            "sample_id": f"synthetic-{index + 1:03d}", "dataset": "synthetic-bootstrap-v1",
            "split": "validation", "synthetic": True, "document_family": family,
            "image_path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "expected_page_count": count,
            "provenance": {"source": "scripts/generate_staging_gold_set.py", "usage_permission": "Project-generated synthetic fixtures; no customer data", "label_reviewer": "generator-derived; independent human review pending"},
            "ground_truth": {"expected_route": family, "expected_fields": fields,
                             "expected_ocr_text": "\n".join("\n".join(text) for text in texts)},
        })
    manifest = output / "manifest.jsonl"
    manifest.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(generate(parser.parse_args().output))
