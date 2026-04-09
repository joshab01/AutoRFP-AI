# AutoRFP-AI

**A multi-agent system that drafts enterprise RFP responses in minutes instead of weeks.**

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](#quick-start)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

---

## The Problem

Enterprise sales teams spend **20-40 hours per RFP response**. A company handling 10 RFPs/month burns **200-400 hours** of senior sales engineering time — roughly **$30K-$60K/month** — on repetitive copy-paste-edit work.

The bottleneck isn't strategy. It's assembly: finding past answers, rewriting for context, and checking compliance. That's what AI agents are good at.

## What AutoRFP Does

Feed it an RFP document. It returns a fully drafted response with compliance checks, confidence scores, and flags for human review.

**Input:** A 60-page RFP (PDF)
**Output:** Structured draft response with per-answer quality scores
**Time:** ~15 minutes vs ~30 hours manual

---

## Architecture

```
┌──────────────────┐
│  Incoming RFP    │
│  (PDF / Text)    │
└────────┬─────────┘
         │
┌────────▼─────────┐
│  PARSER AGENT    │  Extracts every question
│                  │  into structured JSON
└────────┬─────────┘
         │
┌────────▼─────────────────────────┐
│  DRAFT AGENT                     │
│                                  │
│  For each question:              │
│  1. Query FAISS for past answers │
│  2. Draft via few-shot prompting │
│  3. Self-evaluate confidence     │
│                                  │
│  ┌────────────────────────┐      │
│  │ RETRY LOOP + DEDUP FIX │      │
│  │ confidence < 0.7?      │      │
│  │ → retrieve MORE context│      │
│  │ → dedup hash check     │      │
│  │ → max 3 retries        │      │
│  └────────────────────────┘      │
└────────┬─────────────────────────┘
         │
┌────────▼─────────┐
│ COMPLIANCE AGENT │  Checks against 10
│                  │  business rules
└────────┬─────────┘
         │
┌────────▼─────────┐
│    ASSEMBLY      │  Merges into final
│                  │  response document
└──────────────────┘
```

Orchestrated with [LangGraph](https://github.com/langchain-ai/langgraph) state machine.

---

## Tech Stack

| Component | Technology | Why |
|-----------|-----------|-----|
| Agent orchestration | LangGraph | Typed state machine for multi-agent flows |
| Vector retrieval | FAISS + OpenAI embeddings | Fast, free, local similarity search |
| LLM | GPT-4o-mini | Quality/speed/cost balance |
| Drafting | Few-shot prompting (5 examples) | Simulates fine-tuning; swappable for prod |
| PDF parsing | PyMuPDF | Reliable text extraction |
| Compliance | Structured JSON output | Deterministic rule validation |

---

## The Interesting Bug

During testing, the Draft Agent and retrieval step fell into a **silent infinite loop**.

**What happened:** The Draft Agent requests "more context" when confidence < 0.7. The retrieval step re-queries FAISS but keeps returning nearly identical chunks (finite knowledge base). Draft Agent evaluates, still low confidence, loops again. No error. No crash. Just mounting API costs.

Caught it when one question triggered 200+ retrieval calls.

**The fix:**
1. **Hard retry cap** — max 3 attempts per question in LangGraph state
2. **Chunk deduplication** — hash each chunk, compare against already-seen hashes. If >80% overlap, stop loop, flag for human review

See: [`src/agents.py` → `DraftAgent.draft_single()`](src/agents.py)

---

## Quick Start

### Prerequisites
- Python 3.10+
- [OpenAI API key](https://platform.openai.com/api-keys) (total cost: ~$3-5)

### Run in Google Colab

Add this to the first cell of each notebook:
```python
!pip install openai faiss-cpu langgraph langchain-core PyMuPDF tiktoken -q
import os
os.environ["OPENAI_API_KEY"] = "sk-..."
```

Run notebooks in order:

| # | Notebook | What It Does | Time |
|---|----------|-------------|------|
| 01 | [01_data_prep.py](notebooks/01_data_prep.py) | Generate synthetic RFP dataset (50 Q&A pairs) + sample RFP | ~3 min |
| 02 | [02_vector_store.py](notebooks/02_vector_store.py) | Embed knowledge base, build FAISS index | ~2 min |
| 03 | [03_agents_pipeline.py](notebooks/03_agents_pipeline.py) | **Full multi-agent pipeline with LangGraph** | ~5-10 min |
| 04 | [04_fine_tuning_demo.py](notebooks/04_fine_tuning_demo.py) | Fine-tuning workflow demo (optional $1 to run) | ~1 min |

### Run Locally
```bash
git clone https://github.com/YOUR_USERNAME/AutoRFP-AI.git
cd AutoRFP-AI
pip install -r requirements.txt
export OPENAI_API_KEY="sk-..."
cd notebooks
python 01_data_prep.py
python 02_vector_store.py
python 03_agents_pipeline.py
```

---

## Project Structure

```
AutoRFP-AI/
├── README.md
├── requirements.txt
├── .gitignore
├── src/
│   ├── __init__.py
│   └── agents.py              # ParserAgent, DraftAgent (with dedup fix), ComplianceAgent
├── notebooks/
│   ├── 01_data_prep.py        # Generate synthetic RFP data
│   ├── 02_vector_store.py     # Build FAISS retrieval index
│   ├── 03_agents_pipeline.py  # Full LangGraph pipeline
│   └── 04_fine_tuning_demo.py # Fine-tuning workflow
└── data/                      # Generated at runtime
    ├── past_responses.json
    ├── sample_rfp.pdf
    ├── sample_rfp_text.txt
    ├── faiss_index.bin
    ├── retriever_meta.json
    ├── fine_tune_training.jsonl
    └── generated_rfp_response.md
```

---

## Sample Output

```
PIPELINE STATISTICS
  Requirements answered:   12
  Compliance pass rate:    10/12 (83%)
  High-confidence (>=0.7): 9/12 (75%)
  Flagged for review:      3/12
  Average confidence:      0.78
  Total time:              90s (7.5s per question)
```

---

## What I'd Add for Production

- **Fine-tuned model** on 400+ real Q&A pairs (Notebook 04 shows how)
- **Pinecone** for persistent, team-shared vector storage
- **Google Drive API** for automatic RFP intake and delivery
- **python-docx output** matching original RFP structure
- **Feedback loop** — human edits feed back into training data
- **Multi-tenant** — per-client knowledge base and compliance rules

---

## Cost

| Item | Cost |
|------|------|
| Data generation (NB 01) | ~$0.50 |
| Embeddings (NB 02) | ~$0.02 |
| Pipeline run (NB 03) | ~$1-3 |
| Fine-tuning demo (NB 04) | ~$1 if run |
| FAISS | Free |
| **Total** | **Under $5** |

---

## License

MIT

---

*Built as part of my Build in Public series on LinkedIn.*
