Yes. For an on-prem/local-stack IDP OCR system, the best current approach is a **hybrid pipeline**, and **Docker Compose is a good starting choice**.

As of **May 15, 2026**, practical SOTA for local deployment is:

1. **Primary OCR + layout engine (fast, controllable)**
- Use PaddleOCR 3.x (`PP-OCRv5` + `PP-StructureV3`) for mainstream OCR/layout/KIE workloads.
- Keep a strong preprocessing path (deskew/cleanup) for scanned PDFs.

2. **VLM fallback for hard pages**
- Route low-confidence or complex docs (tables, warped scans, mixed layouts) to a VLM parser like `PaddleOCR-VL-1.5` or `Qwen2.5-VL`.
- This gives better robustness on messy real-world docs than OCR-only pipelines.

3. **Model serving**
- Use Triton for high-throughput OCR serving on NVIDIA GPUs.
- Use vLLM for local VLM serving (OpenAI-compatible API).

4. **Confidence gating + HITL**
- Add confidence thresholds and human-review queues for low-confidence fields.

**Should you use Docker Compose?**
- **Yes** if you are on a **single host** (even production on one server).
- Move to **K3s/Kubernetes** when you need multi-node HA, autoscaling, zero-downtime rollouts, or strict SLOs.

Inference note: the “hybrid” recommendation is based on combining the official capabilities of PaddleOCR/Docling/VLM serving stacks, plus orchestration limits of Compose vs K8s.

*Sources:*
- [PaddleOCR 3.0 report (PP-OCRv5, PP-StructureV3, PP-ChatOCRv4)](https://arxiv.org/abs/2507.05595)  
- [PaddleOCR releases (v3.5.0, Apr 21, 2026)](https://github.com/PaddlePaddle/PaddleOCR/releases)  
- [PaddleOCR-VL](https://arxiv.org/abs/2510.14528), [PaddleOCR-VL-1.5](https://arxiv.org/abs/2601.21957)  
- [Qwen2.5-VL report](https://arxiv.org/abs/2502.13923)  
- [Docker: Use Compose in production](https://docs.docker.com/compose/how-tos/production/)  
- [K3s docs](https://docs.k3s.io/)  
- [NVIDIA Triton docs](https://docs.nvidia.com/triton-inference-server/index.html)  
- [vLLM OpenAI-compatible server](https://docs.vllm.ai/en/v0.20.2/serving/openai_compatible_server/)  
- [Docling model/pipeline options](https://docling-project.github.io/docling/usage/model_catalog/), [KServe/Triton OCR option](https://docling-project.github.io/docling/reference/pipeline_options/)  
- [OCRmyPDF](https://ocrmypdf.readthedocs.io/en/stable/), [Tesseract quality guidance](https://tesseract-ocr.github.io/tessdoc/ImproveQuality.html)



