import json
import re
import hashlib
import os
from collections import defaultdict
from pathlib import Path
from redis import Redis

ROOT = Path(__file__).resolve().parent.parent
CORPUS_DATA = json.loads((ROOT / "corpus" / "dgca_car.json").read_text(encoding="utf8"))
CORPUS = CORPUS_DATA["chunks"]
for _passage in CORPUS:
    _passage.setdefault("source_url", CORPUS_DATA.get("source_url", "https://www.dgca.gov.in/"))
    _passage.setdefault("source", CORPUS_DATA.get("source", "DGCA CAR Section 3, Series M, Part IV"))
_cache_redis = Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True,
                              socket_connect_timeout=0.15, socket_timeout=0.2)
_corpus_version = hashlib.sha256(json.dumps(CORPUS, sort_keys=True).encode()).hexdigest()[:12]


def classify_extract(claim):
    text = (claim.get("free_text") or "").lower()
    patterns = {
        "cancellation": r"\b(cancelled|canceled|cancellation)\b",
        "denied_boarding": r"\b(denied boarding|bumped|not allowed to board)\b",
        "delay": r"\b(delayed|delay)\b",
        "downgrading": r"\b(downgraded|downgrading|lower class)\b",
    }
    mentioned = [category for category, pattern in patterns.items() if re.search(pattern, text)]
    category_conflict = bool(len(mentioned) > 1 or (mentioned and claim.get("disruption") not in mentioned))
    out_of_scope_patterns = (
        r"\b(lost|missing|damaged) (?:baggage|luggage|bag)\b",
        r"\b(baggage|luggage) (?:claim|compensation|refund)\b",
        r"\b(frequent flyer|loyalty) miles\b",
        r"\bseat upgrade\b",
    )
    out_of_scope = any(re.search(pattern, text) for pattern in out_of_scope_patterns)
    return {"agent": "intake", "disruption": claim.get("disruption"),
            "text_categories": mentioned, "classification_conflict": category_conflict,
            "out_of_scope": out_of_scope,
            "reported_at_airport": claim.get("passenger_reported_at_airport"),
            "notice_minutes": claim.get("notice_minutes"),
            "alternate_provided": claim.get("alternate_flight_provided"),
            "alternate_accepted": claim.get("alternate_accepted"),
            "block_time_minutes": claim.get("block_time_minutes"),
            "mentions_cancellation": "cancel" in text,
            "fields_missing": [k for k in ("notice_minutes", "passenger_reported_at_airport", "contact_details_provided") if claim.get(k) is None]}


def search_corpus(query, strategy="lexical", limit=8):
    cache_key = "retrieval:" + hashlib.sha256(f"{_corpus_version}:{strategy}:{limit}:{query.lower().strip()}".encode()).hexdigest()
    if os.getenv("CACHE_RETRIEVALS", "true").lower() == "true":
        try:
            cached = _cache_redis.get(cache_key)
            if cached:
                _cache_redis.incr("retrieval:cache_hits")
                return json.loads(cached)
            _cache_redis.incr("retrieval:cache_misses")
        except Exception:
            pass
    terms = set(re.findall(r"[a-z0-9]+", query.lower()))
    lexical = []
    for p in CORPUS:
        hay = (p["text"] + " " + p["clause"] + " " + " ".join(p["tags"])).lower()
        score = sum(1 for term in terms if term in hay)
        if score:
            lexical.append((score, p))
    lexical.sort(key=lambda x: (-x[0], x[1]["id"]))
    try:
        from .retrieval import search as vector_search
        matches = vector_search(query, k=limit)
        matches = [p for p in matches if p.get("score", 0) > 0]
        if matches:
            vector_scores = {p["id"]: p["score"] for p in matches}
            lexical_scores = {p["id"]: score / max(len(terms), 1) for score, p in lexical}
            candidates = {p["id"]: p for p in [x[1] for x in lexical] + matches}
            ranked = sorted(candidates.values(), key=lambda p: -(0.7*lexical_scores.get(p["id"], 0) + 0.3*vector_scores.get(p["id"], 0)))
            output = ranked[:limit]
            if os.getenv("CACHE_RETRIEVALS", "true").lower() == "true":
                try: _cache_redis.setex(cache_key, 3600, json.dumps(output))
                except Exception: pass
            return output
    except Exception:
        # A missing or rebuilding index degrades to the deterministic lexical path.
        pass
    output = [p for _, p in lexical[:limit]]
    if os.getenv("CACHE_RETRIEVALS", "true").lower() == "true":
        try: _cache_redis.setex(cache_key, 3600, json.dumps(output))
        except Exception: pass
    return output


def lookup_clause(clause_id):
    return next((p for p in CORPUS if p["id"] == clause_id or p["clause"] == clause_id), None)


def lookup_claim_field(claim, field):
    return {"field": field, "value": claim.get(field)}


def make_result(position, explanation, route, basis, citations):
    return {"position": position, "recommendation": explanation, "route": route, "basis": basis,
            "citations": citations,
            "authority_note": "This is a triage recommendation only. It does not authorise or issue payment; a customer relations officer decides."}


def adjudicate(claim, facts, retrieved):
    byid = {p["id"]: p for p in retrieved}
    citations = []

    def cite(cid):
        if cid in byid:
            p = byid[cid]
            citations.append({"source_id": cid, "clause": p["clause"], "quote": p["text"],
                              "source": p.get("source"), "source_url": p.get("source_url"), "page": p.get("page")})

    if facts.get("out_of_scope"):
        return make_result("human_escalation", "The passenger's account concerns a service outside this compensation corpus. An officer must route it to the appropriate process.", "human_review", "Claim outside supported corpus", citations)
    if facts.get("classification_conflict"):
        return make_result("human_escalation", "The category selected in the claim differs from the disruption described in the passenger's account. An officer must confirm the category.", "human_review", "Disruption classification conflict", citations)

    clauses = defaultdict(set)
    for passage in retrieved:
        clauses[passage.get("clause")].add(passage.get("text", "").strip())
    conflicting = {clause for clause, texts in clauses.items() if clause and len(texts) > 1}
    if conflicting:
        for passage in retrieved:
            if passage.get("clause") in conflicting:
                cite(passage["id"])
        return make_result("human_escalation", "Retrieved sources contain different text for the same provision. An officer must resolve the source conflict.", "human_review", "Conflicting provision text", citations)

    if claim.get("cause_disputed"):
        cite("CAR-1.4-1.5")
        if claim.get("disruption") == "cancellation":
            cite("CAR-3.3.4")
        return make_result("human_escalation", "Cause attribution is disputed and requires officer review.", "human_review", "Cause dispute", citations)
    if claim.get("cause") and re.search(r"(?i)(air traffic control|weather|meteorolog|security|natural disaster|political instability|force majeure)", claim["cause"]):
        if "CAR-1.4-1.5" in byid:
            cite("CAR-1.4-1.5")
            return make_result("human_escalation", "The reported cause may fall within the extraordinary-circumstances exception. Evidence and causation require officer review.", "human_review", "Cause attribution needs evidence", citations)
    kind = claim.get("disruption")
    if kind == "cancellation":
        for cid in ("CAR-3.3.1", "CAR-3.3.2", "CAR-3.3.3", "CAR-3.3.4", "CAR-3.8.1"):
            cite(cid)

        def decline(deciding_clause, explanation, basis):
            # A decline must rest on a provision that was actually retrieved; otherwise escalate.
            if deciding_clause not in byid:
                return make_result("human_escalation", f"The provision needed to decline ({deciding_clause}) was not retrieved, so the decline cannot be grounded. Officer review required.", "human_review", "Deciding provision not retrieved", citations)
            return make_result("decline_with_cited_provision", explanation, "decline_with_cited_provision", basis, citations)

        if claim.get("passenger_reported_at_airport") is True:
            facility_text = "The passenger had reported at the airport, so meals and refreshments in relation to the waiting time are still owed under CAR 3.3.2 and 3.8.1(a); confirm they were provided."
        elif claim.get("passenger_reported_at_airport") is False:
            facility_text = "The passenger did not report at the airport, so the airport waiting-facilities condition in CAR 3.3.2 is not established on these facts."
        else:
            facility_text = "If the passenger reported and waited at the airport, meals and refreshments in relation to the waiting time are owed under CAR 3.3.2 and 3.8.1(a); confirm the report time."
        alt = claim.get("alternate_flight_provided") is True and claim.get("alternate_accepted") is True
        if alt:
            return decline("CAR-3.3.2",
                "Decline cancellation compensation. CAR 3.3.2 requires the airline to provide either an acceptable alternate flight or compensation with a full refund; the passenger accepted and took the alternate flight, so no compensation is payable. " + facility_text,
                "CAR 3.3.2: accepted alternate flight replaces compensation")
        if claim.get("contact_details_provided") is False:
            return decline("CAR-3.3.3",
                "Decline financial compensation. CAR 3.3.3 states no financial compensation is payable where the passenger did not provide adequate contact information at booking. The airline must still refund the ticket or arrange alternate travel as the passenger chooses, and provide waiting facilities under 3.8.1(a) if the passenger travels on an alternate flight.",
                "CAR 3.3.3: no contact information at booking")
        if claim.get("alternate_flight_provided") is None or claim.get("notice_minutes") is None or claim.get("contact_details_provided") is None:
            return make_result("request_further_evidence", "The claim is incomplete. Notice, booking contact details, and alternate-flight facts affect the applicable cancellation conditions; obtain them before reaching an entitlement position.", "request_further_evidence", "Missing entitlement facts", citations)
        if claim["notice_minutes"] >= 14 * 1440:
            return decline("CAR-3.3.1",
                "Decline cancellation compensation. The passenger was informed at least two weeks before departure, which meets CAR 3.3.1; compensation under 3.3.2 applies only to passengers not informed as 3.3.1 provides. The passenger is owed an alternate flight or refund as acceptable to them.",
                "CAR 3.3.1: informed at least two weeks before departure")
        if claim["notice_minutes"] >= 1440:
            if claim.get("alternate_flight_provided") is True:
                # CAR 3.3.1 requires an alternate flight or refund "as acceptable to the passenger".
                # An offered alternate the passenger did not accept does not show the airline met
                # that, so do not decline: ask whether an acceptable refund was offered.
                return make_result("request_further_evidence",
                    "Notice was between 24 hours and two weeks and an alternate flight was offered, but the passenger did not accept it. CAR 3.3.1 requires an alternate flight or refund as acceptable to the passenger. Confirm whether a full refund was offered. If one was, compensation under 3.3.2 does not apply; if not, refer to an officer.",
                    "request_further_evidence", "CAR 3.3.1: acceptable alternate or refund not established", citations)
            return make_result("human_escalation", "Notice was between 24 hours and two weeks, but no alternate flight was recorded. Whether the airline offered the refund CAR 3.3.1 requires decides whether 3.3.2 applies; an officer must check.", "human_review", "CAR 3.3.1 compliance unclear", citations)
        if claim.get("notice_minutes", 0) < 1440:
            if claim.get("alternate_flight_provided") is False and claim.get("cause") and claim.get("booked_one_way_basic_fare_inr") is not None and not claim.get("cause_disputed"):
                if re.search(r"(?i)(air traffic control|weather|meteorolog|security|natural disaster|political instability|force majeure|strike|flood|government order)", claim.get("cause", "")):
                    return make_result("human_escalation", "The recorded cause may be an extraordinary circumstance under CAR 1.4/1.5. An officer must verify the cause and reasonable measures before any entitlement recommendation.", "human_review", "Potential extraordinary-circumstances exception", citations)
                cap = 5000 if claim["block_time_minutes"] <= 60 else 7500 if claim["block_time_minutes"] <= 120 else 10000
                amount = min(cap, claim["booked_one_way_basic_fare_inr"] + claim["airline_fuel_charge_inr"])
                if "CAR-3.3.2" in byid:
                    result = make_result("pay-as-computed", f"Recommend INR {amount:.2f} compensation and the applicable full ticket refund under CAR 3.3.2, subject to officer verification of the booking fare, notice, contact details, and cause. This is not payment authorization.", "pay-as-computed", "Less of block-time cap and booked one-way basic fare plus airline fuel charge", citations)
                    result["calculation"] = {"currency": "INR", "block_time_cap": cap, "basic_fare": claim["booked_one_way_basic_fare_inr"], "fuel_charge": claim["airline_fuel_charge_inr"], "recommended_compensation": amount}
                    return result
            if claim.get("alternate_flight_provided") is False and claim.get("booked_one_way_basic_fare_inr") is None:
                return make_result("request_further_evidence", "The claim appears to meet the late-notice conditions, but the booked one-way basic fare and fuel charge are needed to compute the CAR 3.3.2 amount. Verify the cause and contact details as well.", "request_further_evidence", "Fare inputs missing for calculation", citations)
            return make_result("human_escalation", "Notice was under 24 hours. The claim must be assessed against the complete notice and contact-information conditions in CAR 3.3.1–3.3.4.", "human_review", "Conditional cancellation provisions require review", citations)
        return make_result("human_escalation", "The effect of notice and contact details requires review against the complete cancellation conditions.", "human_review", "Conditional cancellation provisions require review", citations)
    clause = {"delay": "CAR-3.4.1", "denied_boarding": "CAR-3.2.2", "downgrading": "CAR-3.5.1"}.get(kind)
    if clause:
        cite(clause)
        return make_result("human_escalation", "The relevant clause was retrieved, but this release does not encode all conditional tests for this disruption category. An officer should review the claim.", "human_review", "Rule coverage incomplete", citations)
    return make_result("human_escalation", "Disruption category is outside the supported corpus.", "human_review", "Unsupported category", citations)


def verify(result, retrieved):
    retrieved_ids = {p["id"] for p in retrieved}
    for c in result.get("citations", []):
        if c.get("source_id") not in retrieved_ids:
            return False, "citation_not_retrieved"
        passage = next(p for p in retrieved if p["id"] == c["source_id"])
        if c.get("quote") != passage["text"] or c.get("clause") != passage["clause"]:
            return False, "citation_text_mismatch"
    allowed = {"pay-as-computed", "request_further_evidence", "decline_with_cited_provision", "human_escalation"}
    if result.get("position") not in allowed:
        return False, "invalid_position"
    if result["position"] in {"decline_with_cited_provision", "pay-as-computed"} and not result.get("citations"):
        return False, "unsupported_determination"
    return True, "verified"
