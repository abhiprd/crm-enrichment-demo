import importlib.util
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("make_deals", ROOT / "scripts" / "make_deals.py")
make_deals = importlib.util.module_from_spec(spec)
sys.modules["make_deals"] = make_deals
spec.loader.exec_module(make_deals)

from crm.deals import validate_deal  # noqa: E402

DATA = make_deals.build_all(1337)
DEALS = DATA["deals"]


def test_counts_and_split():
    assert len(DEALS) == 100
    assert Counter(d["split"] for d in DEALS) == {"learn": 40, "validation": 40, "test": 20}
    assert sum(not d["cases"] for d in DEALS) == 20
    assert sum(d["hubspot"]["seed"] for d in DEALS) == 15


def test_deterministic_and_valid():
    assert make_deals.build_all(1337) == DATA
    assert make_deals.build_all(1) != DATA
    assert [e for d in DEALS for e in validate_deal(d)] == []


def test_fictional_and_unique():
    assert all(d["company"]["domain"].endswith(".example") for d in DEALS)
    assert len({d["company"]["name"] for d in DEALS}) == 100


def test_every_split_sees_every_case():
    ids = {c for opts in make_deals.SLOTS.values() for c in opts}
    wanted = {make_deals.CASE_OF.get(c, c) for c in ids}
    for split in ("learn", "validation", "test"):
        seen = {c["id"] for d in DEALS if d["split"] == split for c in d["cases"]}
        assert seen == wanted, split


def test_house_rules_apply_to_every_deal():
    for d in DEALS:
        f = d["fields"]
        nxt = f["next_step"]
        if nxt["truth"]["value"] and nxt["truth"]["value"]["date"] is None:
            assert nxt["expected_proposal"] is None
        tl = f["decision_timeline"]["expected_proposal"]
        if tl:
            assert "Q" in tl["value"]
        if any(c["id"] == "ballpark_budget" for c in d["cases"]):
            assert f["budget"]["expected_proposal"] is None
        if any(c["id"] == "committed_champion" for c in d["cases"]):
            assert f["champion"]["expected_proposal"] is None


def test_not_mentioned_never_proposed_and_cases_target_distinct_fields():
    for d in DEALS:
        for spec in d["fields"].values():
            if spec["truth"]["status"] == "not_mentioned":
                assert spec["expected_proposal"] is None
        fields = [c["field"] for c in d["cases"]]
        assert len(fields) == len(set(fields))


# ---- hand-check sampler --------------------------------------------------------------------
spec_hc = importlib.util.spec_from_file_location("handcheck_sheet", ROOT / "scripts" / "handcheck_sheet.py")
handcheck = importlib.util.module_from_spec(spec_hc)
sys.modules["handcheck_sheet"] = handcheck
spec_hc.loader.exec_module(handcheck)


def fake_keys():
    cases = sorted(handcheck.CASE_IDS)
    keys, n = {}, 0
    for i in range(100):
        split = ["learn", "learn", "validation", "validation", "test"][i % 5]
        cs = [] if i % 5 == 4 and i % 10 == 4 else [{"id": cases[n % len(cases)]}]
        n += bool(cs)
        keys[f"d{i:03d}"] = {"split": split, "cases": cs}
    return keys


def test_handcheck_sample_is_seeded_covers_cases_and_splits():
    keys = fake_keys()
    a = handcheck.sample(keys, 15, 7)
    assert a == handcheck.sample(keys, 15, 7) and len(a) == 15
    assert {c["id"] for d in a for c in keys[d]["cases"]} == set(handcheck.CASE_IDS)
    from collections import Counter
    assert Counter(keys[d]["split"] for d in a) == {"learn": 6, "validation": 6, "test": 3}


def test_handcheck_utterance_parser():
    assert handcheck.utterances("hdr\n[0] [00:00] A (R): hi\n[12] [01:00] B (R): yo\n") == {
        0: "[0] [00:00] A (R): hi", 12: "[12] [01:00] B (R): yo"}
