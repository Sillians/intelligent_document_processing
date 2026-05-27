**vast multimodal OCR training data** (image + text + layout):


1. [IDL-WDS (Hugging Face)](https://huggingface.co/datasets/pixparse/idl-wds)  
Huge: dataset card states **19M pages**, with `pdf`, `tif`, OCR JSON, and OCR text.

2. [BLIP3-OCR-200M (Hugging Face)](https://huggingface.co/datasets/Salesforce/blip3-ocr-200m)  
Very large multimodal OCR corpus: about **96M rows**.

3. [PubLayNet (IBM/GitHub)](https://github.com/ibm-aur-nlp/PubLayNet) + [IBM paper page](https://research.ibm.com/publications/publaynet-largest-dataset-ever-for-document-layout-analysis)  
Large layout dataset; IBM reports it was built from **1M+ PDFs** and has **360k+ document images**.

4. [DocBank](https://github.com/doc-analysis/DocBank)  
Large document layout benchmark: **500k pages** (400k train / 50k val / 50k test).

5. [RVL-CDIP](https://adamharley.com/rvl-cdip/)  
Large doc classification set: **400k images**, 16 classes (also links to IIT-CDIP lineage).

Use these for fine-tuning/evaluation quality:

6. [DocLayNet](https://github.com/DS4SD/DocLayNet)  
Human-annotated layout dataset, **80,863 pages**.

7. [DocVQA](https://site.docvqa.org/datasets/docvqa)  
Document QA benchmark: **50k questions, 12k images**.

8. [XFUND](https://github.com/doc-analysis/XFUND)  
Multilingual forms (7 languages), key-value extraction.

9. [SROIE (HF mirror)](https://huggingface.co/datasets/jsdnrs/ICDAR2019-SROIE) and [official challenge portal](https://rrc.cvc.uab.es/?ch=13&com=downloads)  
Receipt OCR + key info extraction, ~1k receipts (HF card has 987 rows).

10. [CORD](https://github.com/clovaai/cord)  
Receipt parsing dataset (public release is 1k sample set in v1/v2).

11. [FUNSD](https://guillaumejaume.github.io/FUNSD/)  
Small but high-quality form understanding benchmark (199 annotated forms).

Also useful: [UCSF IDL data/API background](https://www.library.ucsf.edu/news/idl-data-sets-201910/) for large archive access patterns.


---

