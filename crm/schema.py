"""Shared constants for the dataset contract. Change these only with a PLAN.md decision."""

FIELDS = (
    "budget",
    "decision_timeline",
    "competitors",
    "economic_buyer",
    "champion",
    "pain_points",
    "use_case",
    "next_step",
    "stage_signal",
)

STATUSES = ("stated", "hedged", "negated", "superseded", "not_mentioned")
CRM_RELATIONS = ("matches", "stale", "empty")
SPLITS = ("learn", "validation", "test")
CALL_TYPES = ("discovery", "demo", "negotiation")

TRAPS = (
    "negation",
    "hedged_timeline",
    "superseded_value",
    "speaker_attribution",
    "buried_next_step",
)
HOUSE_RULES = (
    "competitor_threshold",
    "dated_next_step",
    "ballpark_budget",
    "quarter_timeline",
    "committed_champion",
)
CASE_IDS = TRAPS + HOUSE_RULES

# Text that must never appear inside a transcript: it would leak the answer key
# or the case labels to the extractor.
LEAK_TOKENS = tuple(CASE_IDS) + (
    "not_mentioned",
    "answer key",
    "ground truth",
    "house rule",
    "REVEAL",
    "EVIDENCE",
)

MIN_TURNS = 10
DEMO_PREFIX = "demo-"
