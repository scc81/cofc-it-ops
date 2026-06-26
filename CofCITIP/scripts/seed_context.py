"""
seed_context.py — JARVIS Context Seeder
========================================
CofCITIP — College of Charleston IT Infrastructure Platform

Seeds ChromaDB with facts about the CofC IT environment.
Run this anytime you learn something new — it upserts, won't duplicate.

Usage:
    python3 seed_context.py              # seed everything
    python3 seed_context.py --dry-run    # print facts without writing
    python3 seed_context.py --wipe       # clear context collection and reseed

Layers:
    Layer 1 — Team, tools, platform facts (populated now)
    Layer 2 — Naming conventions, group structures, policy names, thresholds
              (add as you discover them — just drop into LAYER_2_FACTS below)
    Corrections — "when asked X, the right answer is Y" entries that fix
              recurring JARVIS mistakes (CORRECTION_FACTS below)

Idempotency: doc IDs are md5(fact text) — the same fact always maps to the
same ID, so upsert is safe to re-run forever. Because EDITING a fact's text
changes its ID, the stale wording would linger in the collection; list any
retired wordings in RETIRED_FACTS and the seeder deletes them on every run.

Repo: github.com/scc81/cofc-it-ops
"""

import sys
import hashlib
import chromadb
from datetime import datetime
from dotenv import load_dotenv
import os

import structlog  # Session 5: structured logging for the Docling ingest path

from embedding import get_embedding_function  # FIX: shared Ollama embedder

load_dotenv("/etc/cofc-itip/config.env")

# Session 5: structlog for ingest_document error paths (the legacy manual-fact
# seeder keeps its print() CLI UX; the new document-ingestion path logs JSON so
# a multi-file run's skips/failures are machine-readable in journald).
structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.JSONRenderer(),
    ],
)
log = structlog.get_logger("jarvis.seed_context")

CHROMA_PATH = os.getenv("CHROMA_PATH", "/var/lib/cofc-itip/chroma")
DRY_RUN     = "--dry-run" in sys.argv
WIPE        = "--wipe"    in sys.argv

# FIX: the "context" collection is opened in seed(), retire(), and main().
# Build the embedding function ONCE and reuse it everywhere so the seeder
# embeds with the same Ollama model jarvis_core uses at query time — and so
# it never falls back to Chroma's default ONNX embedder (which writes to the
# service account's non-writable home dir and crashes).
EMBED_FN = get_embedding_function()


def _context_collection(client: "chromadb.PersistentClient"):
    """Single accessor for the 'context' collection with the right embedder."""
    return client.get_or_create_collection("context", embedding_function=EMBED_FN)


# =============================================================================
# LAYER 1 — TEAM STRUCTURE
# What JARVIS needs to know about the people it works with.
# =============================================================================

LAYER_1_TEAM = [

    # ── Management ────────────────────────────────────────────────────────────
    "Philip Paradise is the IT Endpoint Manager's direct supervisor and manager.",
    "Philip Paradise prefers proactive communication and candid, informal updates.",
    "Philip Paradise should be looped in on anything with strategic or compliance implications.",

    # ── Endpoint Team ─────────────────────────────────────────────────────────
    "Steven is the IT Endpoint Manager at the College of Charleston. He started June 1, 2026.",
    "Steven's role centers on endpoint deployment management and application support.",
    "Steven's primary tools are Intune, Jamf, image prep and deployment, and app packaging.",

    "Greg Gray is a Senior Endpoint Engineer at CofC with approximately 15 years at the college.",
    "Greg Gray works full-time in the office in Room 530.",
    "Greg Gray is the most tenured member of the endpoint team and holds deep institutional knowledge.",

    "Mitch Versoza is an Endpoint Engineer at CofC with approximately 4 years at the college.",
    "Mitch Versoza is fully remote.",

    "Matt Agostosa-Viado is an Endpoint Engineer at CofC with approximately 6 years at the college.",
    "Matt Agostosa-Viado is hybrid and works from Room 534.",

    "Andrew Bergstrom is the Field Support Manager at CofC.",

    # ── InfoSec ───────────────────────────────────────────────────────────────
    "Alejandro Torres is on the InfoSec team at CofC.",
    "Alejandro Torres handles Secureworks and Taegis vendor coordination.",
    "Alejandro Torres is the point of contact for Taegis API access.",

    "Joe Gibson is the incoming CISO at CofC.",
    "Joe Gibson joined from Trident Technical College and holds a CISM certification.",
    "Joe Gibson has a higher education security background.",

    "Joe Mahon is a security stakeholder at CofC.",
]


# =============================================================================
# LAYER 1 — PLATFORM AND TOOLING FACTS
# What JARVIS needs to know about the technical environment.
# =============================================================================

LAYER_1_PLATFORM = [

    # ── Endpoint Management ───────────────────────────────────────────────────
    "CofC endpoint management platforms are: SCCM/MECM, Jamf Pro, and Microsoft Intune.",
    "Microsoft Intune is the primary Windows endpoint management platform at CofC.",
    "Jamf Pro is the Mac endpoint management platform at CofC.",
    "SCCM/MECM is the legacy Windows management platform. Migration to Intune is in progress.",
    "The CM-to-Intune migration is an active project. Not all devices have been migrated yet.",
    "AppsAnywhere is in use at CofC and is being evaluated for migration.",
    "Dell integration with Intune is an active project.",
    "Summer Reimage is an active seasonal project.",
    "Secure Boot certificate management is an active project.",

    # ── Identity and Directory ────────────────────────────────────────────────
    "CofC uses Microsoft Entra ID (formerly Azure AD) for identity and device management.",
    "CofC runs Microsoft 365 for productivity.",
    "CofC runs Google Workspace. Gemini is available under the institutional plan.",
    "CofC uses SharePoint as part of Microsoft 365.",

    # ── Security Stack ────────────────────────────────────────────────────────
    "CofC's current endpoint security tool is SentinelOne.",
    "SentinelOne may be replaced by Microsoft Defender for Endpoint or Sophos. Decision pending.",
    "Sophos is under active evaluation as a potential SentinelOne replacement.",
    "Microsoft Defender for Endpoint (MDE) deployment is an active project.",
    "Taegis by Secureworks is the SIEM platform at CofC.",
    "Taegis replaced Splunk at CofC.",
    "Taegis uses a GraphQL API. API access requires coordination with Alejandro Torres.",
    "Secureworks manages Taegis vendor coordination through Alejandro Torres.",

    # ── Compliance ────────────────────────────────────────────────────────────
    "FERPA compliance is a standing requirement for all tooling and data handling decisions at CofC.",
    "No sensitive operational data — device data, user data, SIEM data — should leave the CofC network.",
    "CofCITIP is designed to be FERPA-safe and zero-egress by default.",

    # ── JARVIS Platform ───────────────────────────────────────────────────────
    "CofCITIP is the College of Charleston IT Infrastructure Platform — an on-premises AI operations platform.",
    "CofCITIP runs on decommissioned hardware called the Bloomberg Box (BB).",
    "BB is the primary AI inference node for CofCITIP. It has an Intel i7, 64GB RAM, 1TB drive, and an NVIDIA RTX A2000 GPU.",
    "The BB GPU is an NVIDIA RTX A2000 with 12GB VRAM — confirmed. The model stack is sized to this ceiling.",
    "CofCITIP uses Ollama for local LLM inference. No model data leaves the box.",
    "CofCITIP uses ChromaDB for semantic memory with two collections: context and behavioral.",
    "CofCITIP uses GLaDOS for voice output and faster-whisper for speech to text.",
    "CofCITIP uses OpenWakeWord for wake word detection. It replaced Porcupine because Porcupine's free tier requires network validation and fails silently offline.",
    "The JARVIS Phase 1 wake word is 'hey jarvis'. The custom wake phrase 'Boo Boo Kitty' is deferred to Phase 2.",
    "CofCITIP is built and maintained by Steven. The repo is at github.com/scc81/cofc-it-ops.",
    "No production action — package deployment or policy remediation — can occur without explicit human confirmation. This is a hard design requirement.",

    # ── Tool Ownership ────────────────────────────────────────────────────────
    "Intune manages the Windows fleet at CofC. Jamf Pro manages the Mac fleet.",
    "Taegis is the SIEM at CofC. It replaced Splunk and is queried via a GraphQL API.",
    "SentinelOne is the current EDR at CofC. It may be replaced by Microsoft Defender for Endpoint or Sophos — decision pending.",

    # ── Briefing Schedule ─────────────────────────────────────────────────────
    "The JARVIS morning briefing runs at 8am on weekdays.",
    "The morning briefing covers fleet health, compliance, overnight security alerts, and patch status.",

    # ── Human Oversight ───────────────────────────────────────────────────────
    "JARVIS cannot deploy packages or push policies to production without human approval.",
    "The blast radius concept applies to all deployment decisions — scope must be validated before any production action.",
    "Package pipeline approvers are: primary Steven, backup Greg Gray.",
]


# =============================================================================
# LAYER 2 — ENVIRONMENT SPECIFICS
# Add facts here as you discover them. Rerun the script to upsert.
# Format: plain English sentences, one fact per string.
# =============================================================================

LAYER_2_FACTS = [

    # ── Naming Conventions ────────────────────────────────────────────────────
    # Add device, group, and policy naming patterns as you learn them.
    # Example:
    # "Windows devices in Intune follow the naming convention WIN-COFC-XXXXXX.",
    # "Jamf smart groups for compliance use the prefix COMP- followed by the policy name.",

    # ── Intune Groups ─────────────────────────────────────────────────────────
    # "The Intune test group for Windows package pipeline is named TEST-WIN-PackagePipeline.",
    # "The Intune test group for Mac package pipeline is named TEST-MAC-PackagePipeline.",

    # ── Known Thresholds ──────────────────────────────────────────────────────
    # "The blast radius threshold for a single deployment is 50 devices.",
    # "Devices with no check-in for more than 30 days are considered stale.",

    # ── Known Problem Areas ───────────────────────────────────────────────────
    # "The AppsAnywhere migration has a known issue with..."

]


# =============================================================================
# CORRECTIONS — recurring-mistake fixes
# Pattern: "When asked about X, the correct answer is Y."
# These rank high in semantic retrieval for the exact question they fix.
# The entries below are PLACEHOLDER examples — replace the bracketed values
# with real ones as the environment is discovered, then rerun the seeder.
# =============================================================================

CORRECTION_FACTS = [
    "CORRECTION: When asked about test groups, the correct Intune test group "
    "is TEST-WIN-PackagePipeline. (placeholder — confirm real group name)",

    "CORRECTION: When asked about the Mac test group, the correct Jamf smart "
    "group is TEST-MAC-PackagePipeline. (placeholder — confirm real group name)",

    "CORRECTION: When asked who approves package deployments, the answer is "
    "Steven as primary approver and Greg Gray as backup — never JARVIS itself.",

    "CORRECTION: When asked about the blast radius limit, the threshold for a "
    "single deployment is 50 devices. (placeholder — confirm with Philip)",

    "CORRECTION: When asked when a device counts as stale, the answer is no "
    "check-in for more than 30 days. (placeholder — confirm team standard)",
]


# =============================================================================
# RETIRED FACTS — old wordings to DELETE from the collection
# When you edit a fact above, paste its previous exact text here so the
# stale version is removed on the next run (IDs are content hashes, so an
# edit alone leaves the old doc behind).
# =============================================================================

RETIRED_FACTS = [
    "The RTX A2000 GPU VRAM is either 6GB or 12GB — not yet confirmed. This determines the model stack.",
    "CofCITIP uses GLaDOS for voice output and Porcupine for wake word detection.",
    "The JARVIS wake word is Boo Boo Kitty.",
]


# =============================================================================
# SEEDER
# =============================================================================

def make_id(text: str) -> str:
    """Stable ID from content — same fact always gets the same ID, enabling upsert."""
    return "fact_" + hashlib.md5(text.encode()).hexdigest()


def seed(client: chromadb.PersistentClient, facts: list[str], category: str):
    collection = _context_collection(client)
    now        = datetime.now().isoformat()
    added      = 0
    skipped    = 0

    for fact in facts:
        fact = fact.strip()
        if not fact:
            continue

        doc_id = make_id(fact)

        if DRY_RUN:
            print(f"  [dry-run] {fact}")
            continue

        try:
            # Upsert — safe to run multiple times, won't create duplicates
            collection.upsert(
                documents=[fact],
                metadatas=[{
                    "category":  category,
                    "timestamp": now,
                    "source":    "seed_context.py"
                }],
                ids=[doc_id]
            )
            added += 1
        except Exception as e:
            print(f"  [error] {e} — {fact[:60]}")
            skipped += 1

    return added, skipped


def retire(client: chromadb.PersistentClient, facts: list[str]) -> int:
    """Delete retired fact wordings by their deterministic IDs. Safe to run
    when the IDs don't exist — Chroma delete is a no-op for missing IDs."""
    if not facts:
        return 0
    collection = _context_collection(client)
    ids = [make_id(f.strip()) for f in facts if f.strip()]
    if DRY_RUN:
        for f in facts:
            print(f"  [dry-run retire] {f[:70]}")
        return 0
    try:
        collection.delete(ids=ids)
        return len(ids)
    except Exception as e:
        print(f"  [retire error] {e}")
        return 0


# =============================================================================
# DOCUMENT INGESTION (Session 5 — Docling)
# Converts a PDF/DOCX/HTML/etc. file to clean markdown, chunks it, and upserts
# each chunk into the SAME context collection using the SAME content-hash ID
# pattern as make_id() — so re-ingesting an unchanged file creates no
# duplicates. This is ADDITIVE: the manual-fact seeding above is untouched.
# =============================================================================

# Target chunk size in words. "A few hundred words each" per spec. We pack whole
# paragraphs up to this size and only ever split ON paragraph/sentence
# boundaries, never mid-sentence.
CHUNK_TARGET_WORDS = 250
CHUNK_MAX_WORDS    = 400  # hard ceiling before forcing a sentence-boundary split


def make_chunk_id(text: str) -> str:
    """Stable per-chunk ID from content — same chunk text always maps to the
    same ID, so upsert is idempotent across re-ingests. Mirrors make_id()'s
    content-hash approach (the file's established pattern) rather than using a
    timestamp, which would duplicate on every run."""
    return "doc_" + hashlib.md5(text.encode()).hexdigest()


def _split_sentences(paragraph: str) -> list[str]:
    """Lightweight sentence splitter for the rare paragraph that exceeds
    CHUNK_MAX_WORDS on its own. Splits after . ! ? followed by whitespace —
    good enough to avoid mid-sentence cuts without pulling in an NLP dep."""
    import re
    parts = re.split(r"(?<=[.!?])\s+", paragraph.strip())
    return [p for p in parts if p.strip()]


def _chunk_markdown(markdown: str) -> list[str]:
    """Chunk markdown into ~CHUNK_TARGET_WORDS passages on paragraph
    boundaries. A single oversized paragraph is split on sentence boundaries.
    Never splits mid-sentence."""
    paragraphs = [p.strip() for p in markdown.split("\n\n") if p.strip()]
    chunks: list[str] = []
    buf: list[str] = []
    buf_words = 0

    def flush():
        nonlocal buf, buf_words
        if buf:
            chunks.append("\n\n".join(buf).strip())
            buf, buf_words = [], 0

    for para in paragraphs:
        words = len(para.split())

        # Oversized single paragraph -> split on sentences, emit greedily.
        if words > CHUNK_MAX_WORDS:
            flush()
            sent_buf: list[str] = []
            sent_words = 0
            for sent in _split_sentences(para):
                sw = len(sent.split())
                if sent_words + sw > CHUNK_TARGET_WORDS and sent_buf:
                    chunks.append(" ".join(sent_buf).strip())
                    sent_buf, sent_words = [], 0
                sent_buf.append(sent)
                sent_words += sw
            if sent_buf:
                chunks.append(" ".join(sent_buf).strip())
            continue

        # Adding this paragraph would overflow the target -> flush first.
        if buf_words + words > CHUNK_TARGET_WORDS and buf:
            flush()
        buf.append(para)
        buf_words += words

    flush()
    return [c for c in chunks if c]


def ingest_document(path: str, category: str) -> dict:
    """Convert a document at `path` to markdown via Docling, chunk it, and
    upsert each chunk into the context collection. Idempotent (content-hash
    IDs). A malformed/corrupt document is LOGGED and SKIPPED (returns a status
    dict) rather than raising — so a multi-file seeding run keeps going.

    Returns {"path","category","status","chunks_upserted","chunks_total"}."""
    if not os.path.exists(path):
        log.warning("ingest.missing_file", path=path)
        return {"path": path, "category": category, "status": "missing",
                "chunks_upserted": 0, "chunks_total": 0}

    # Docling conversion is wrapped: a corrupt/unsupported file logs + skips.
    try:
        from docling.document_converter import DocumentConverter
        converter = DocumentConverter()
        result = converter.convert(path)
        markdown = result.document.export_to_markdown()
    except ImportError:
        log.error("ingest.docling_missing",
                  detail="docling not installed — pip install docling")
        return {"path": path, "category": category, "status": "docling_missing",
                "chunks_upserted": 0, "chunks_total": 0}
    except Exception as e:
        # Malformed/corrupt/unsupported document — skip, don't crash the run.
        log.error("ingest.convert_failed", path=path, error=str(e))
        return {"path": path, "category": category, "status": "convert_failed",
                "chunks_upserted": 0, "chunks_total": 0}

    chunks = _chunk_markdown(markdown)
    if not chunks:
        log.warning("ingest.empty", path=path)
        return {"path": path, "category": category, "status": "empty",
                "chunks_upserted": 0, "chunks_total": 0}

    if DRY_RUN:
        for c in chunks:
            print(f"  [dry-run ingest] {os.path.basename(path)} :: {c[:70]}...")
        log.info("ingest.dry_run", path=path, chunks=len(chunks))
        return {"path": path, "category": category, "status": "dry_run",
                "chunks_upserted": 0, "chunks_total": len(chunks)}

    client = chromadb.PersistentClient(path=CHROMA_PATH)
    collection = _context_collection(client)
    now = datetime.now().isoformat()
    fname = os.path.basename(path)
    upserted = 0

    for i, chunk in enumerate(chunks):
        try:
            collection.upsert(
                documents=[chunk],
                metadatas=[{
                    "category": category,
                    "timestamp": now,
                    "source": "seed_context.py:ingest_document",
                    "source_file": fname,
                    "chunk_index": i,
                }],
                ids=[make_chunk_id(chunk)],
            )
            upserted += 1
        except Exception as e:
            # Per-chunk failure is logged and skipped; the rest still ingest.
            log.error("ingest.chunk_failed", path=path, chunk_index=i,
                      error=str(e))

    log.info("ingest.done", path=path, category=category,
             chunks_upserted=upserted, chunks_total=len(chunks))
    return {"path": path, "category": category, "status": "ok",
            "chunks_upserted": upserted, "chunks_total": len(chunks)}


def main():
    print("=" * 60)
    print("CofCITIP — Context Seeder")
    print("=" * 60)

    if DRY_RUN:
        print("DRY RUN — nothing will be written\n")

    client = chromadb.PersistentClient(path=CHROMA_PATH)

    if WIPE and not DRY_RUN:
        print("Wiping context collection...")
        client.delete_collection("context")
        _context_collection(client)
        print("Wiped.\n")

    all_facts = [
        (LAYER_1_TEAM,     "team"),
        (LAYER_1_PLATFORM, "platform"),
        (LAYER_2_FACTS,    "environment"),
        (CORRECTION_FACTS, "correction"),
    ]

    # Retire stale wordings FIRST so an edited fact never coexists with its
    # previous version inside the same run.
    removed = retire(client, RETIRED_FACTS)
    if not DRY_RUN and removed:
        print(f"\nRetired {removed} stale fact(s).")

    total_added   = 0
    total_skipped = 0

    for facts, category in all_facts:
        non_empty = [f for f in facts if f.strip()]
        if not non_empty:
            print(f"  [{category}] No facts to seed — skipping")
            continue

        print(f"\n[{category}] Seeding {len(non_empty)} facts...")
        added, skipped = seed(client, non_empty, category)
        total_added   += added
        total_skipped += skipped
        if not DRY_RUN:
            print(f"  → {added} upserted, {skipped} errors")

    if not DRY_RUN:
        collection = _context_collection(client)
        total = collection.count()
        print(f"\n{'=' * 60}")
        print(f"Done. Total facts in context collection: {total}")
        print(f"{'=' * 60}")
    else:
        print(f"\nDry run complete. {len([f for fl, _ in all_facts for f in fl if f.strip()])} facts would be written.")


if __name__ == "__main__":
    # Session 5: --ingest /path/to/file --category <cat> routes to the Docling
    # document-ingestion path. Without --ingest, behavior is unchanged: the
    # manual-fact seeder runs as before. --dry-run is honored in both paths.
    if "--ingest" in sys.argv:
        try:
            ingest_path = sys.argv[sys.argv.index("--ingest") + 1]
        except IndexError:
            print("Usage: python3 seed_context.py --ingest /path/to/file "
                  "--category <category> [--dry-run]")
            sys.exit(2)
        category = "documents"
        if "--category" in sys.argv:
            try:
                category = sys.argv[sys.argv.index("--category") + 1]
            except IndexError:
                print("--category requires a value")
                sys.exit(2)

        print("=" * 60)
        print(f"CofCITIP — Document Ingestion ({ingest_path})")
        print(f"Category: {category}")
        if DRY_RUN:
            print("DRY RUN — nothing will be written")
        print("=" * 60)

        res = ingest_document(ingest_path, category)
        print(f"\nStatus: {res['status']} | "
              f"{res['chunks_upserted']}/{res['chunks_total']} chunks upserted")
        # Non-ok, non-dry-run outcomes exit non-zero so a wrapping shell/cron
        # loop can detect a bad file without parsing stdout.
        sys.exit(0 if res["status"] in ("ok", "dry_run") else 1)
    else:
        main()
