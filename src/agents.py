"""
src/agents.py — AutoRFP Agent Definitions

Three core agents:
  1. ParserAgent     — Extracts structured questions from raw RFP text
  2. DraftAgent      — Retrieves context + drafts answers (with retry/dedup bug fix)
  3. ComplianceAgent — Validates drafted answers against compliance rules
"""

import json
import logging
from openai import OpenAI

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(name)s | %(message)s")
logger = logging.getLogger("AutoRFP")

client = OpenAI()
MODEL = "gpt-4o-mini"

# ─── Few-shot examples (replaces fine-tuning for demo) ───────────────────────

FEW_SHOT_EXAMPLES = [
    {
        "question": "Describe your data encryption approach for data at rest.",
        "answer": (
            "Acme Cloud Platform encrypts all data at rest using AES-256 encryption. "
            "Encryption keys are managed through our dedicated Key Management Service (KMS), "
            "which supports customer-managed keys (BYOK) for organizations requiring direct "
            "key control. All key rotations occur automatically every 90 days, and our KMS "
            "is SOC 2 Type II audited independently."
        ),
    },
    {
        "question": "What is your guaranteed uptime SLA?",
        "answer": (
            "Acme Cloud Platform provides a 99.95% uptime SLA for our Enterprise tier, "
            "backed by service credits. In the last 12 months, our actual measured uptime "
            "was 99.98%. Our status page at status.acmecloud.io provides real-time and "
            "historical availability data. Planned maintenance windows are communicated "
            "14 days in advance and scheduled during off-peak hours."
        ),
    },
    {
        "question": "How do you handle GDPR data subject access requests?",
        "answer": (
            "We provide a self-service Data Subject Request (DSR) portal within our admin "
            "console. Authorized administrators can process access, rectification, and "
            "deletion requests directly. Our platform fulfills DSR requests within 72 hours. "
            "We also offer a DPA aligned with Standard Contractual Clauses for cross-border "
            "data transfers."
        ),
    },
    {
        "question": "Describe your implementation timeline for a 1,000-user deployment.",
        "answer": (
            "A typical 1,000-user deployment follows our Accelerated Onboarding Program: "
            "Week 1-2 covers environment provisioning and SSO/SCIM integration. Week 3-4 "
            "handles data migration and custom workflow configuration. Week 5-6 includes "
            "UAT and phased rollout by department. A dedicated Customer Success Engineer is "
            "assigned throughout. Most Enterprise clients are fully operational within 6 weeks."
        ),
    },
    {
        "question": "What support tiers do you offer?",
        "answer": (
            "We offer three support tiers: Standard (email support, 24-hour response), "
            "Premium (email + chat, 4-hour response, dedicated CSM), and Enterprise "
            "(24/7 phone + chat, 1-hour critical response, dedicated TAM and quarterly "
            "business reviews). All tiers include access to our knowledge base, community "
            "forums, and monthly product webinars."
        ),
    },
]

# ─── Compliance Rules ────────────────────────────────────────────────────────

COMPLIANCE_RULES = [
    {"id": "C-01", "rule": "Never promise 100% uptime — max claimable is 99.99%"},
    {"id": "C-02", "rule": "SOC 2 must specify 'Type II', not just 'SOC 2'"},
    {"id": "C-03", "rule": "GDPR references must mention lawful basis for processing"},
    {"id": "C-04", "rule": "No pricing commitments below $15/user/month for Enterprise"},
    {"id": "C-05", "rule": "Encryption must specify AES-256, not just 'AES'"},
    {"id": "C-06", "rule": "Do not guarantee specific migration timelines under 4 weeks"},
    {"id": "C-07", "rule": "Never claim 'zero downtime' — use 'minimal downtime' instead"},
    {"id": "C-08", "rule": "All certifications mentioned must include year of last audit"},
    {"id": "C-09", "rule": "Support SLAs must specify business hours vs 24/7 clearly"},
    {"id": "C-10", "rule": "Do not reference competitor products by name"},
]


# ═════════════════════════════════════════════════════════════════════════════
# AGENT 1: Parser Agent
# ═════════════════════════════════════════════════════════════════════════════

class ParserAgent:
    """Extracts structured questions/requirements from raw RFP text."""

    SYSTEM_PROMPT = """You are an RFP parsing specialist. Extract every
question, requirement, and information request from an RFP document.

For each item, provide:
- id: sequential identifier like "REQ-001"
- question: the full text of the question or requirement
- category: one of [Security, Technical Architecture, Data Privacy,
  Implementation, Pricing, Support, General]

Return ONLY a JSON array. No markdown fences. No extra text.
Example: [{"id": "REQ-001", "question": "Describe your...", "category": "Security"}]"""

    def parse(self, rfp_text: str) -> list[dict]:
        logger.info("ParserAgent: Extracting requirements from RFP...")
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": f"Extract all questions and requirements:\n\n{rfp_text}"},
            ],
            temperature=0.2,
            max_tokens=4000,
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        requirements = json.loads(raw)
        logger.info(f"ParserAgent: Extracted {len(requirements)} requirements")
        return requirements


# ═════════════════════════════════════════════════════════════════════════════
# AGENT 2: Draft Agent (Retriever + Writer with retry/dedup)
# ═════════════════════════════════════════════════════════════════════════════

class DraftAgent:
    """
    For each RFP question:
      1. Retrieves similar past answers from FAISS
      2. Drafts a response using few-shot prompting
      3. Self-evaluates confidence
      4. If confidence < 0.7, retries with NEW context (dedup check)
      5. Hard cap at 3 retries to prevent infinite loops

    Contains the "bug fix" from the LinkedIn post.
    """

    MAX_RETRIES = 3
    CONFIDENCE_THRESHOLD = 0.7
    DEDUP_OVERLAP_LIMIT = 0.8

    def __init__(self, retrieve_fn):
        self.retrieve_fn = retrieve_fn
        self.retry_log = []

    def _build_system_prompt(self) -> str:
        examples_text = ""
        for i, ex in enumerate(FEW_SHOT_EXAMPLES, 1):
            examples_text += f"\n--- Example {i} ---\n"
            examples_text += f"Q: {ex['question']}\nA: {ex['answer']}\n"

        return f"""You are a senior sales engineer at Acme Cloud Platform drafting
RFP responses. Match the tone and detail level shown in these examples:
{examples_text}

RULES:
- Be specific with concrete details (numbers, feature names, timelines)
- Professional but human — not robotic or generic
- Use reference answers as a base but adapt to the specific question
- Keep answers between 80 and 200 words

After your answer, on a new line write CONFIDENCE: followed by a number
between 0.0 and 1.0:
  0.9-1.0 = Excellent, directly relevant context
  0.7-0.8 = Good, adequate context
  0.5-0.6 = Partial, some gaps
  0.0-0.4 = Poor, mostly guessing"""

    def draft_single(self, question: str, requirement_id: str) -> dict:
        """Draft an answer with retry logic + dedup fix."""
        seen_hashes: set = set()
        all_context_chunks: list = []
        attempt = 0
        best_result = None

        while attempt < self.MAX_RETRIES:
            attempt += 1
            logger.info(f"DraftAgent: [{requirement_id}] Attempt {attempt}/{self.MAX_RETRIES}")

            # Retrieve context (with dedup exclusion)
            retrieved = self.retrieve_fn(
                query=question, top_k=3, exclude_hashes=seen_hashes,
            )

            # ═══ DEDUP CHECK — THE BUG FIX ═══════════════════════════════
            new_hashes = {r["hash"] for r in retrieved}
            if seen_hashes:
                overlap_ratio = len(new_hashes & seen_hashes) / max(len(new_hashes), 1)
            else:
                overlap_ratio = 0.0

            if attempt > 1 and overlap_ratio >= self.DEDUP_OVERLAP_LIMIT:
                logger.warning(
                    f"DraftAgent: [{requirement_id}] "
                    f"Dedup triggered — {overlap_ratio:.0%} overlap. "
                    f"Stopping loop, flagging for human review."
                )
                self.retry_log.append({
                    "requirement_id": requirement_id,
                    "attempt": attempt,
                    "action": "DEDUP_STOP",
                    "overlap_ratio": overlap_ratio,
                })
                break
            # ═══════════════════════════════════════════════════════════════

            for r in retrieved:
                seen_hashes.add(r["hash"])
            all_context_chunks.extend(retrieved)

            # Build context string
            context_str = ""
            for i, chunk in enumerate(all_context_chunks, 1):
                context_str += f"\n[Reference {i}]\nPast Q: {chunk['question']}\nPast A: {chunk['answer']}\n"

            user_msg = (
                f"Using the reference answers below, draft a response to this RFP question.\n\n"
                f"RFP QUESTION: {question}\n\n"
                f"REFERENCE ANSWERS FROM PAST PROPOSALS:\n{context_str}"
            )

            response = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": self._build_system_prompt()},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.4,
                max_tokens=1000,
            )

            raw_answer = response.choices[0].message.content.strip()

            # Parse confidence score
            confidence = 0.5
            answer_text = raw_answer
            if "CONFIDENCE:" in raw_answer:
                parts = raw_answer.rsplit("CONFIDENCE:", 1)
                answer_text = parts[0].strip()
                try:
                    confidence = float(parts[1].strip())
                except ValueError:
                    confidence = 0.5

            result = {
                "requirement_id": requirement_id,
                "question": question,
                "answer": answer_text,
                "confidence": confidence,
                "attempts": attempt,
                "context_chunks_used": len(all_context_chunks),
                "flagged_for_review": False,
            }

            self.retry_log.append({
                "requirement_id": requirement_id, "attempt": attempt,
                "action": "DRAFT", "confidence": confidence,
                "chunks_retrieved": len(retrieved), "total_chunks": len(all_context_chunks),
            })

            best_result = result

            if confidence >= self.CONFIDENCE_THRESHOLD:
                logger.info(f"DraftAgent: [{requirement_id}] Confidence {confidence:.2f} — accepted")
                break
            else:
                logger.info(f"DraftAgent: [{requirement_id}] Confidence {confidence:.2f} — retrying")

        if best_result and (
            best_result["confidence"] < self.CONFIDENCE_THRESHOLD or attempt >= self.MAX_RETRIES
        ):
            best_result["flagged_for_review"] = True
            logger.warning(f"DraftAgent: [{requirement_id}] Flagged for human review")

        return best_result


# ═════════════════════════════════════════════════════════════════════════════
# AGENT 3: Compliance Agent
# ═════════════════════════════════════════════════════════════════════════════

class ComplianceAgent:
    """Validates each drafted RFP answer against compliance rules."""

    def __init__(self, rules=None):
        self.rules = rules or COMPLIANCE_RULES

    def check(self, draft: dict) -> dict:
        logger.info(f"ComplianceAgent: Checking [{draft['requirement_id']}]...")
        rules_text = "\n".join(f"- {r['id']}: {r['rule']}" for r in self.rules)

        prompt = f"""Review this RFP response for compliance violations.

COMPLIANCE RULES:
{rules_text}

DRAFTED RESPONSE:
{draft['answer']}

Check against EVERY rule. Return ONLY a JSON object:
{{
  "passed": true/false,
  "flags": [{{"rule_id": "C-XX", "issue": "brief description"}}]
}}
If no violations: {{"passed": true, "flags": []}}
No markdown fences."""

        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1, max_tokens=500,
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            result = {"passed": False, "flags": [{"rule_id": "PARSE_ERROR", "issue": "Could not parse compliance output"}]}

        result["requirement_id"] = draft["requirement_id"]
        result["checked_rules"] = len(self.rules)
        logger.info(f"ComplianceAgent: [{draft['requirement_id']}] {'PASSED' if result['passed'] else 'FLAGGED'}")
        return result
