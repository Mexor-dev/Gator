#!/home/user/Gator/venv/bin/python3
"""
Project Gator — Phase 1  |  src/extract_logic.py
─────────────────────────────────────────────────────────────────────────────
Donor Logit Extraction Pipeline

Loads the 32B Donor model CPU-only via mmap, runs 5,000 synthetic
reasoning prompts through it, and captures the top-K probability
distributions at the final token position of each prompt.

These "logic fingerprints" encode *how the Donor thinks* — which tokens
it assigns elevated probability to when reasoning — and are compressed
into a searchable binary dictionary: ~/Gator/bin/logic_map.gate

Usage:
    python3 ~/Gator/src/extract_logic.py [--batch-size 8] [--top-k 128]
                                          [--resume] [--dry-run]
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import os
import pickle
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generator

import numpy as np

# ── Path bootstrap ─────────────────────────────────────────────────────────────
GATOR_ROOT = Path.home() / "Gator"
DONOR_PATH = GATOR_ROOT / "models" / "donor.gguf"
OUTPUT_PATH = GATOR_ROOT / "bin" / "logic_map.gate"
LOG_PATH    = GATOR_ROOT / "logs" / "extract_logic.log"

# Verify paths exist early
for _p in (DONOR_PATH,):
    if not _p.exists():
        sys.exit(f"[FATAL] Required file not found: {_p}\n"
                 "        Run setup_env.py first to move models into place.")

for _d in (OUTPUT_PATH.parent, LOG_PATH.parent):
    _d.mkdir(parents=True, exist_ok=True)

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("gator.extract")

# ── Import llama_cpp (installed by setup_env.py) ───────────────────────────────
try:
    from llama_cpp import Llama
except ImportError:
    sys.exit("[FATAL] llama-cpp-python not installed.  Run: python3 ~/Gator/setup_env.py")


# ──────────────────────────────────────────────────────────────────────────────
# DATA STRUCTURES
# ──────────────────────────────────────────────────────────────────────────────

CATEGORY_TAGS = {
    "chain_of_thought": 0,
    "analysis":         1,
    "fact_checking":    2,
    "mathematical":     3,
    "causal":           4,
    "counterfactual":   5,
    "ethical":          6,
    "analogical":       7,
    "deductive":        8,
    "inductive":        9,
}

@dataclass
class LogitRecord:
    """One captured reasoning fingerprint."""
    prompt_hash: str               # sha256[:16] of the prompt
    category:    int               # CATEGORY_TAGS value
    token_ids:   np.ndarray        # shape (top_k,)  int32
    probs:       np.ndarray        # shape (top_k,)  float16

    def to_dict(self) -> dict:
        return {
            "h": self.prompt_hash,
            "c": self.category,
            "t": self.token_ids.tolist(),
            "p": self.probs.tolist(),
        }


@dataclass
class LogicMap:
    """Serialisable container for the full extraction run."""
    version:    str = "1.0"
    top_k:      int = 128
    vocab_size: int = 0
    records:    list[LogitRecord] = field(default_factory=list)
    meta:       dict              = field(default_factory=dict)

    def save(self, path: Path) -> int:
        """Serialise to gzip-compressed pickle. Returns bytes written."""
        payload = {
            "version":    self.version,
            "top_k":      self.top_k,
            "vocab_size": self.vocab_size,
            "meta":       self.meta,
            "records":    [r.to_dict() for r in self.records],
        }
        data = gzip.compress(pickle.dumps(payload, protocol=5), compresslevel=6)
        path.write_bytes(data)
        return len(data)

    @classmethod
    def load(cls, path: Path) -> "LogicMap":
        payload = pickle.loads(gzip.decompress(path.read_bytes()))
        lm = cls(
            version=payload["version"],
            top_k=payload["top_k"],
            vocab_size=payload["vocab_size"],
            meta=payload.get("meta", {}),
        )
        for r in payload["records"]:
            lm.records.append(LogitRecord(
                prompt_hash=r["h"],
                category=r["c"],
                token_ids=np.array(r["t"], dtype=np.int32),
                probs=np.array(r["p"], dtype=np.float16),
            ))
        return lm


# ──────────────────────────────────────────────────────────────────────────────
# SYNTHETIC PROMPT GENERATION  (5,000 prompts, 10 categories × 500 each)
# ──────────────────────────────────────────────────────────────────────────────

def _mk_prompts_chain_of_thought() -> list[str]:
    subjects = [
        "sorting a list of integers", "finding the shortest path in a graph",
        "proving that sqrt(2) is irrational", "balancing a chemical equation",
        "computing compound interest over 20 years",
        "solving a quadratic equation", "proving the triangle inequality",
        "determining Big-O for a nested loop", "factoring a large semiprime",
        "calculating the half-life decay of carbon-14",
    ]
    templates = [
        "Walk through, step by step, the process of {subject}.",
        "Think carefully and explain each stage when {subject}.",
        "Lay out your reasoning in full before concluding: {subject}.",
        "Before giving an answer, articulate every intermediate step for {subject}.",
        "Decompose the problem of {subject} into its constituent reasoning steps.",
    ]
    prompts = []
    for i in range(500):
        t = templates[i % len(templates)]
        s = subjects[i % len(subjects)]
        prompts.append(t.format(subject=s))
    return prompts


def _mk_prompts_analysis() -> list[str]:
    topics = [
        "the economic impact of automation on labour markets",
        "the trade-offs between privacy and national security",
        "the long-term consequences of antibiotic over-use",
        "the role of central banks in preventing recessions",
        "the evolutionary pressure that drives altruistic behaviour",
        "the thermodynamic limits of battery energy density",
        "the sociological effects of social-media filter bubbles",
        "the geopolitical implications of rare-earth mineral scarcity",
        "the cognitive biases that affect financial decision-making",
        "the systemic risks in a highly interconnected global supply chain",
    ]
    templates = [
        "Provide a rigorous analysis of {topic}.",
        "Examine the key drivers, feedback loops, and second-order effects of {topic}.",
        "Critically analyse {topic}, distinguishing correlation from causation.",
        "Break down {topic} from first principles and identify the most leveraged variables.",
        "Construct a multi-perspective analytical framework for understanding {topic}.",
    ]
    prompts = []
    for i in range(500):
        prompts.append(templates[i % len(templates)].format(topic=topics[i % len(topics)]))
    return prompts


def _mk_prompts_fact_checking() -> list[str]:
    claims = [
        "Einstein failed mathematics as a child",
        "Napoleon Bonaparte was extremely short",
        "Humans use only 10% of their brains",
        "The Great Wall of China is visible from space with the naked eye",
        "Lightning never strikes the same place twice",
        "Goldfish have a three-second memory",
        "Blood is blue inside the human body",
        "Sugar causes hyperactivity in children",
        "Shaving makes hair grow back thicker",
        "Antibiotics are effective against viral infections",
    ]
    templates = [
        "Fact-check the following claim and provide evidence: \"{claim}\"",
        "Is this statement true or false? Justify with sources and reasoning: \"{claim}\"",
        "Evaluate the veracity of: \"{claim}\". What does the evidence say?",
        "Trace the origin of the misconception: \"{claim}\". Where did it come from and why is it wrong?",
        "Verify and refute or confirm: \"{claim}\". Cite the mechanism or study.",
    ]
    prompts = []
    for i in range(500):
        prompts.append(templates[i % len(templates)].format(claim=claims[i % len(claims)]))
    return prompts


def _mk_prompts_mathematical() -> list[str]:
    problems = [
        "Given f(x)=3x^3 - 2x^2 + 5x - 7, find all real roots.",
        "Prove by induction that sum of first n integers is n(n+1)/2.",
        "Solve the differential equation dy/dx = y * sin(x).",
        "Find the determinant of the 3×3 matrix [[1,2,3],[4,5,6],[7,8,9]].",
        "Compute the Taylor series expansion of e^x around x=0 to the 6th term.",
        "Determine the eigenvalues of the matrix [[2,1],[1,2]].",
        "Solve: if 3^x = 81 and 2^y = 32, what is x*y?",
        "Integrate x^2 * e^x dx using integration by parts.",
        "Find the radius of convergence for sum(n=0 to inf) x^n / n!",
        "Prove that there are infinitely many prime numbers.",
    ]
    templates = [
        "{problem}",
        "Solve the following, showing all working: {problem}",
        "Work through this problem methodically: {problem}",
        "Explain your reasoning at each step: {problem}",
        "Identify the technique and apply it carefully: {problem}",
    ]
    prompts = []
    for i in range(500):
        prompts.append(templates[i % len(templates)].format(problem=problems[i % len(problems)]))
    return prompts


def _mk_prompts_causal() -> list[str]:
    scenarios = [
        "increased minimum wage on youth unemployment",
        "deforestation on regional rainfall patterns",
        "social media usage on teen mental health",
        "antibiotic use in livestock on human drug resistance",
        "urbanisation on biodiversity loss",
        "rising interest rates on housing affordability",
        "access to education on economic mobility",
        "chronic sleep deprivation on cognitive performance",
        "fossil fuel subsidies on renewable energy adoption",
        "air pollution on cardiovascular disease rates",
    ]
    templates = [
        "Explain the causal chain linking {scenario}.",
        "What are the direct and indirect causal mechanisms between {scenario}?",
        "Identify confounding variables when analysing the effect of {scenario}.",
        "How would you establish causality (not just correlation) for {scenario}?",
        "Trace the full causal pathway from cause to effect for {scenario}.",
    ]
    prompts = []
    for i in range(500):
        prompts.append(templates[i % len(templates)].format(scenario=scenarios[i % len(scenarios)]))
    return prompts


def _mk_prompts_counterfactual() -> list[str]:
    events = [
        "the printing press had never been invented",
        "the Cuban Missile Crisis had escalated to full nuclear exchange",
        "Alexander Fleming had not noticed the mould killing his bacteria culture",
        "the Roman Empire had never fallen",
        "the internet had remained a purely academic network",
        "vaccines had been rejected globally in the 19th century",
        "quantum mechanics had been proven false in 1930",
        "the Black Death had a 100% mortality rate",
        "fossil fuels had never been adopted as primary energy",
        "the Moon had never existed",
    ]
    templates = [
        "What would the world look like today if {event}?",
        "Reason through the cascading consequences if {event}.",
        "Construct the most plausible alternate history given that {event}.",
        "Which second-order effects would dominate if {event}?",
        "Identify the three most important divergence points if {event}.",
    ]
    prompts = []
    for i in range(500):
        prompts.append(templates[i % len(templates)].format(event=events[i % len(events)]))
    return prompts


def _mk_prompts_ethical() -> list[str]:
    dilemmas = [
        "a self-driving car must choose between swerving into one pedestrian or five",
        "a doctor can save five patients by harvesting organs from one healthy person",
        "a journalist must decide whether to publish information that is true but will cause panic",
        "a company discovers its product harms 0.01% of users but is beneficial to 99.99%",
        "a government can prevent a terrorist attack only by mass-surveilling all citizens",
        "a scientist can cure a disease but only by conducting painful animal experiments",
        "you find a wallet with $10,000 and no ID, but clearly belonging to someone wealthy",
        "leaking classified information that exposes government corruption but endangers agents",
        "allocating a scarce life-saving medicine between a young child and an elderly professor",
        "an AI system is 95% accurate at predicting crime — should it be used for pre-emptive arrest?",
    ]
    templates = [
        "Analyse the ethical dimensions of: {dilemma}.",
        "Apply utilitarian, deontological, and virtue-ethics frameworks to: {dilemma}.",
        "What is the morally correct action in this scenario, and why: {dilemma}?",
        "Identify where ethical frameworks agree and diverge for: {dilemma}.",
        "Reason from first principles about the obligations involved in: {dilemma}.",
    ]
    prompts = []
    for i in range(500):
        prompts.append(templates[i % len(templates)].format(dilemma=dilemmas[i % len(dilemmas)]))
    return prompts


def _mk_prompts_analogical() -> list[str]:
    pairs = [
        ("the structure of an atom", "the solar system"),
        ("the flow of electricity", "water through pipes"),
        ("evolution by natural selection", "software version control"),
        ("the immune system", "a national defence force"),
        ("compiler optimisation", "a skilled editor pruning a manuscript"),
        ("gradient descent", "a blind hiker descending a foggy mountain"),
        ("memory consolidation during sleep", "defragmenting a hard drive"),
        ("the stock market", "a voting machine for short-term and weighing machine for long-term"),
        ("neural networks", "the brain's synaptic plasticity"),
        ("encryption", "a combination lock with an astronomical number of combinations"),
    ]
    templates = [
        "Explain the analogy between {a} and {b}, noting both strengths and limits.",
        "How far does the analogy between {a} and {b} extend before it breaks down?",
        "Use {b} as a mental model to explain {a} to a non-expert.",
        "List the structural correspondences and disanalogies between {a} and {b}.",
        "What does framing {a} as {b} reveal, and what does it obscure?",
    ]
    prompts = []
    for i in range(500):
        a, b = pairs[i % len(pairs)]
        prompts.append(templates[i % len(templates)].format(a=a, b=b))
    return prompts


def _mk_prompts_deductive() -> list[str]:
    syllogisms = [
        ("All mammals are warm-blooded. Whales are mammals.", "Are whales warm-blooded?"),
        ("If P then Q. P is true.", "What can we conclude about Q?"),
        ("No reptiles have fur. Snakes are reptiles.", "Do snakes have fur?"),
        ("All prime numbers greater than 2 are odd. 17 is a prime number greater than 2.", "Is 17 odd?"),
        ("If it is raining, the ground is wet. The ground is not wet.", "Is it raining?"),
        ("All bachelors are unmarried. John is a bachelor.", "Is John married?"),
        ("Either the suspect was at home or at the office. She was not at home.", "Where was the suspect?"),
        ("If A implies B, and B implies C, and A is true.", "What follows?"),
        ("All squares are rectangles. ABCD is a square.", "Is ABCD a rectangle?"),
        ("If the test result is positive, the patient has the disease. The result is negative.", "Can we confirm the patient is disease-free?"),
    ]
    templates = [
        "Given: {premises}  Question: {question}  Reason deductively to the conclusion.",
        "Apply formal deductive logic to: {premises}  Conclude: {question}",
        "What necessarily follows from these premises? {premises}  Consider: {question}",
        "Identify the valid deductive argument form here: {premises}  {question}",
        "State whether the argument is valid and sound: {premises}  {question}",
    ]
    prompts = []
    for i in range(500):
        prem, q = syllogisms[i % len(syllogisms)]
        prompts.append(templates[i % len(templates)].format(premises=prem, question=q))
    return prompts


def _mk_prompts_inductive() -> list[str]:
    observations = [
        "Every swan observed in Europe over 200 years has been white",
        "The sun has risen every day for all of recorded human history",
        "Every controlled trial of aspirin has found it reduces fever",
        "All observed samples of pure water boil at 100°C at sea level",
        "Every prime number tested above 2 has been odd",
        "All 500 surveyed customers preferred the new packaging",
        "Each of the last 30 winters in this region has brought snowfall",
        "Every charged particle observed has an integer multiple of the electron charge",
        "All mammals observed to date have been born and eventually died",
        "Every tested sample of the compound was found to be toxic above 50mg/kg",
    ]
    templates = [
        "What general principle can be induced from: {obs}? Assess the strength of the induction.",
        "How confident should we be in the generalisation derived from: {obs}?",
        "What sample bias or confounding could undermine the induction: {obs}?",
        "Construct the strongest and weakest forms of the inductive argument from: {obs}.",
        "Identify what evidence would falsify the inductive conclusion from: {obs}.",
    ]
    prompts = []
    for i in range(500):
        prompts.append(templates[i % len(templates)].format(obs=observations[i % len(observations)]))
    return prompts


def build_prompt_dataset() -> list[tuple[str, int]]:
    """Returns list of (prompt_text, category_id) tuples — exactly 5,000 entries."""
    cat = CATEGORY_TAGS
    dataset: list[tuple[str, int]] = []
    for prompts, tag in [
        (_mk_prompts_chain_of_thought(), cat["chain_of_thought"]),
        (_mk_prompts_analysis(),         cat["analysis"]),
        (_mk_prompts_fact_checking(),    cat["fact_checking"]),
        (_mk_prompts_mathematical(),     cat["mathematical"]),
        (_mk_prompts_causal(),           cat["causal"]),
        (_mk_prompts_counterfactual(),   cat["counterfactual"]),
        (_mk_prompts_ethical(),          cat["ethical"]),
        (_mk_prompts_analogical(),       cat["analogical"]),
        (_mk_prompts_deductive(),        cat["deductive"]),
        (_mk_prompts_inductive(),        cat["inductive"]),
    ]:
        assert len(prompts) == 500, f"Expected 500, got {len(prompts)}"
        dataset.extend((p, tag) for p in prompts)
    assert len(dataset) == 5000
    random.shuffle(dataset)
    log.info("Prompt dataset ready: %d entries across %d categories", len(dataset), len(CATEGORY_TAGS))
    return dataset


# ──────────────────────────────────────────────────────────────────────────────
# MODEL LOADING
# ──────────────────────────────────────────────────────────────────────────────

def load_donor(n_ctx: int = 1024) -> Llama:
    """
    Load the 32B Donor model CPU-only with mmap so that only accessed
    weight pages are resident in RAM at any time.
    """
    log.info("Loading Donor model (CPU-only, mmap): %s", DONOR_PATH)
    log.info("Context window: %d tokens", n_ctx)
    t0 = time.perf_counter()
    llm = Llama(
        model_path=str(DONOR_PATH),
        n_gpu_layers=0,          # strictly CPU — no VRAM usage
        use_mmap=True,           # OS-managed page loading; prevents RAM overflow
        use_mlock=False,         # do not pin pages; allow OS to swap cold weights
        n_ctx=n_ctx,
        n_batch=512,
        n_threads=os.cpu_count() or 4,
        n_threads_batch=os.cpu_count() or 4,
        logits_all=False,        # only last-token logits; faster + less memory
        verbose=False,
    )
    elapsed = time.perf_counter() - t0
    vocab_size = llm.n_vocab()
    log.info("Donor loaded in %.1fs  |  vocab_size=%d", elapsed, vocab_size)
    return llm


# ──────────────────────────────────────────────────────────────────────────────
# LOGIT EXTRACTION
# ──────────────────────────────────────────────────────────────────────────────

def _hash_prompt(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _softmax(x: np.ndarray) -> np.ndarray:
    x = x - x.max()          # numerical stability
    e = np.exp(x)
    return e / e.sum()


def extract_top_k_logits(
    llm: Llama,
    prompt: str,
    top_k: int,
    category: int,
) -> LogitRecord | None:
    """
    Tokenise `prompt`, run a single forward pass through the Donor,
    and return a LogitRecord containing the top-K token probabilities
    from the final sequence position.

    Returns None on any failure so the caller can continue with the rest.
    """
    try:
        tokens = llm.tokenize(prompt.encode("utf-8"), add_bos=True)
        if not tokens:
            log.warning("Empty token list for prompt: %r", prompt[:60])
            return None

        # Truncate to fit in context (leave one slot for generation)
        max_tokens = llm.n_ctx() - 1
        if len(tokens) > max_tokens:
            tokens = tokens[:max_tokens]

        # Forward pass — eval writes logits for the last token into llm.scores
        llm.reset()
        llm.eval(tokens)

        # Retrieve raw logits: shape (vocab_size,)
        # llm.scores is a 2D array (seq_len × vocab_size) when logits_all=True,
        # but with logits_all=False only the last row is valid.
        raw_logits = np.array(llm.scores[-1], dtype=np.float32)

        if raw_logits.ndim != 1 or raw_logits.size == 0:
            log.warning("Unexpected logits shape %s — skipping", raw_logits.shape)
            return None

        # Top-K selection
        if top_k >= raw_logits.size:
            top_k_actual = raw_logits.size
        else:
            top_k_actual = top_k

        top_indices = np.argpartition(raw_logits, -top_k_actual)[-top_k_actual:]
        top_indices = top_indices[np.argsort(raw_logits[top_indices])[::-1]]  # sorted desc
        top_probs   = _softmax(raw_logits[top_indices]).astype(np.float16)

        return LogitRecord(
            prompt_hash=_hash_prompt(prompt),
            category=category,
            token_ids=top_indices.astype(np.int32),
            probs=top_probs,
        )

    except Exception as exc:
        log.error("Extraction failed for prompt %r: %s", prompt[:60], exc, exc_info=True)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# RESUME SUPPORT  — skip already-extracted prompt hashes
# ──────────────────────────────────────────────────────────────────────────────

def load_existing_hashes(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        lm = LogicMap.load(path)
        hashes = {r.prompt_hash for r in lm.records}
        log.info("Resuming: %d records already in %s", len(hashes), path.name)
        return hashes
    except Exception as exc:
        log.warning("Could not read existing gate file (%s) — starting fresh", exc)
        return set()


def load_existing_records(path: Path) -> list[LogitRecord]:
    if not path.exists():
        return []
    try:
        lm = LogicMap.load(path)
        return lm.records
    except Exception:
        return []


# ──────────────────────────────────────────────────────────────────────────────
# CHECKPOINT SAVE — write incrementally every N records
# ──────────────────────────────────────────────────────────────────────────────

CHECKPOINT_INTERVAL = 100   # save every 100 extracted records


def checkpoint(
    logic_map: LogicMap,
    path: Path,
    count: int,
    total: int,
) -> None:
    bytes_written = logic_map.save(path)
    log.info(
        "Checkpoint: %d/%d records saved → %s (%.1f KB)",
        count, total, path.name, bytes_written / 1024,
    )


# ──────────────────────────────────────────────────────────────────────────────
# MAIN EXTRACTION LOOP
# ──────────────────────────────────────────────────────────────────────────────

def run_extraction(
    top_k:       int  = 128,
    resume:      bool = True,
    dry_run:     bool = False,
    n_ctx:       int  = 1024,
    max_prompts: int | None = None,
) -> None:
    log.info("=" * 64)
    log.info("Project Gator — Donor Logit Extraction")
    log.info("Donor:   %s", DONOR_PATH.name)
    log.info("Output:  %s", OUTPUT_PATH)
    log.info("top_k=%d  resume=%s  dry_run=%s", top_k, resume, dry_run)
    log.info("=" * 64)

    # Build full 5,000-prompt dataset
    dataset = build_prompt_dataset()

    # Resume: skip already-processed prompts
    done_hashes:     set[str]         = set()
    existing_records: list[LogitRecord] = []
    if resume:
        done_hashes      = load_existing_hashes(OUTPUT_PATH)
        existing_records = load_existing_records(OUTPUT_PATH)

    todo = [(p, c) for p, c in dataset if _hash_prompt(p) not in done_hashes]
    if max_prompts is not None and max_prompts > 0:
        todo = todo[:max_prompts]
    log.info("Prompts to process: %d  (skipping %d already done)", len(todo), len(done_hashes))

    if dry_run:
        log.info("[dry-run] Showing first 10 prompts then exiting.")
        for i, (p, c) in enumerate(todo[:10]):
            cat_name = next(k for k, v in CATEGORY_TAGS.items() if v == c)
            log.info("  [%02d] (%s) %s", i, cat_name, p[:80])
        return

    # Load model
    llm = load_donor(n_ctx=n_ctx)
    vocab_size = llm.n_vocab()

    # Initialise LogicMap
    logic_map = LogicMap(
        top_k=top_k,
        vocab_size=vocab_size,
        records=existing_records,
        meta={
            "donor_model": DONOR_PATH.name,
            "extraction_start": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "categories": CATEGORY_TAGS,
        },
    )

    total          = len(todo)
    processed      = 0
    failed         = 0
    t_start        = time.perf_counter()
    last_checkpoint = len(existing_records)

    for idx, (prompt, category) in enumerate(todo):
        cat_name = next(k for k, v in CATEGORY_TAGS.items() if v == category)

        record = extract_top_k_logits(llm, prompt, top_k, category)

        if record is not None:
            logic_map.records.append(record)
            processed += 1
        else:
            failed += 1

        # Progress log every 50 prompts
        if (idx + 1) % 50 == 0 or (idx + 1) == total:
            elapsed   = time.perf_counter() - t_start
            rate      = (idx + 1) / elapsed if elapsed > 0 else 0
            eta_s     = (total - idx - 1) / rate if rate > 0 else 0
            eta_min   = eta_s / 60
            log.info(
                "Progress: %d/%d  ok=%d  fail=%d  %.2f prompts/s  ETA %.1f min",
                idx + 1, total, processed, failed, rate, eta_min,
            )

        # Periodic checkpoint
        new_since_ckpt = len(logic_map.records) - last_checkpoint
        if new_since_ckpt >= CHECKPOINT_INTERVAL:
            checkpoint(logic_map, OUTPUT_PATH, len(logic_map.records), total + len(existing_records))
            last_checkpoint = len(logic_map.records)

    # Final save
    logic_map.meta["extraction_end"]    = time.strftime("%Y-%m-%dT%H:%M:%S")
    logic_map.meta["total_records"]     = len(logic_map.records)
    logic_map.meta["failed_extractions"] = failed
    final_bytes = logic_map.save(OUTPUT_PATH)

    log.info("=" * 64)
    log.info("Extraction complete.")
    log.info("  Total records : %d", len(logic_map.records))
    log.info("  Failed        : %d", failed)
    log.info("  Output file   : %s", OUTPUT_PATH)
    log.info("  File size     : %.2f MB", final_bytes / 1_048_576)
    log.info("  Elapsed       : %.1f min", (time.perf_counter() - t_start) / 60)
    log.info("=" * 64)


# ──────────────────────────────────────────────────────────────────────────────
# QUICK INSPECTION UTILITY  — run with --inspect to dump stats without loading model
# ──────────────────────────────────────────────────────────────────────────────

def inspect_gate(path: Path) -> None:
    if not path.exists():
        print(f"File not found: {path}")
        return
    lm = LogicMap.load(path)
    print(f"\nlogic_map.gate — Inspection Report")
    print(f"  Version     : {lm.version}")
    print(f"  Records     : {len(lm.records)}")
    print(f"  top_k       : {lm.top_k}")
    print(f"  vocab_size  : {lm.vocab_size}")
    print(f"  Meta        : {json.dumps(lm.meta, indent=4)}")
    if lm.records:
        from collections import Counter
        cat_counts = Counter(r.category for r in lm.records)
        inv = {v: k for k, v in CATEGORY_TAGS.items()}
        print(f"\n  Category breakdown:")
        for cat_id, cnt in sorted(cat_counts.items()):
            print(f"    {inv.get(cat_id, cat_id):25s} : {cnt}")
        sample = lm.records[0]
        print(f"\n  Sample record [0]:")
        print(f"    prompt_hash : {sample.prompt_hash}")
        print(f"    category    : {inv.get(sample.category, sample.category)}")
        print(f"    top token   : id={sample.token_ids[0]}  prob={float(sample.probs[0]):.4f}")


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Project Gator — Phase 1 Donor Logit Extraction",
    )
    p.add_argument("--top-k",    type=int,  default=128,
                   help="Number of top-probability tokens to store per prompt (default: 128)")
    p.add_argument("--n-ctx",    type=int,  default=1024,
                   help="Context window size for the Donor model (default: 1024)")
    p.add_argument("--max-prompts", type=int, default=None,
                   help="Optional cap on prompts processed in this run")
    p.add_argument("--resume",   action="store_true", default=True,
                   help="Skip prompts already present in an existing gate file (default: on)")
    p.add_argument("--no-resume", dest="resume", action="store_false",
                   help="Ignore any existing gate file and start fresh")
    p.add_argument("--dry-run",  action="store_true",
                   help="Print prompts and exit without loading the model")
    p.add_argument("--inspect",  action="store_true",
                   help="Print stats about an existing logic_map.gate file and exit")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if args.inspect:
        inspect_gate(OUTPUT_PATH)
        sys.exit(0)

    run_extraction(
        top_k=args.top_k,
        resume=args.resume,
        dry_run=args.dry_run,
        n_ctx=args.n_ctx,
        max_prompts=args.max_prompts,
    )
