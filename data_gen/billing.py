from datetime import date, timedelta

from .common import END, START, key

PAYERS = ("Cairnvale Mutual", "Fenmere Benefit Trust", "Quill Basin Plan")
# Each scenario couples the clinical finding, diagnosis and reason road travel was unsafe.
CASES = (
    ("STEMI", "I21.3", "recurrent VT on monitor", (
        "Repeat VT made the unmonitored stretch of the road transfer unsafe.",
        "ALS unit ETA plus drive time exceeded the accepting cath team's safe interval.",
        "Could not tolerate another hour by road with runs of VT despite treatment.")),
    ("head injury", "S06.9XAA", "GCS fell from 12 to 8", (
        "Road time to neurosurgery was unsafe given the falling GCS.",
        "Ground crew lacked the ventilator needed for controlled transfer.",
        "With the airway deteriorating, the longer ambulance trip was contraindicated.")),
    ("respiratory failure", "J96.01", "SpO2 84% despite mask oxygen", (
        "Available road unit could not maintain the required ventilator settings.",
        "The pt desaturated during brief circuit changes; ground transfer on the available circuit was unsafe.",
        "No ground crew could sustain the required airway support en route.")),
    ("shock", "R57.9", "MAP 52 on repeat cuff", (
        "The road team's pump could not deliver both continuous pressors.",
        "Persistent hypotension ruled out the prolonged trip by ambulance.",
        "ALS unit ETA was 70 min; waiting that long with refractory shock was unsafe.")),
)
MISSING = ("Ground transfer field left blank.", "No comparison of transport modes dictated.",
           "ALS unit ETA requested; receiving bed confirmed.", "Highway route discussed at bedside.",
           "Family asked whether pt could travel by road. Crew acknowledged.", "")


def identifiers(r, n):
    given = r.choice(("Talviq", "Embril", "Orvessa", "Nimvian", "Zelquinn"))
    family = r.choice(("Drelwick", "Vossquill", "Fernivex", "Tarnivell", "Quenvoss"))
    birth = date(1940, 1, 1) + timedelta(days=r.randrange(23000))
    suffix = f"{n%100:02}"
    return {
        "name": r.choice((f"{given} {family}", f"{family}, {given}", f"{given} Q. {family}")),
        "dob": birth.strftime(("%Y-%m-%d", "%m/%d/%Y", "%d %b %Y")[n % 3]),
        "ssn": f"9{n%100:02}-00-{n:04}",
        "mrn": r.choice((f"QV/{n:06}", f"MR-{n:04}-X", f"ZQ{n:07}")),
        "phone": r.choice((f"(202) 555-01{suffix}", f"202.555.01{suffix}", f"202-555-01{suffix}")),
        "address": f"{n+10} " + r.choice(("Quillglass Spur", "Vexmere Ct", "Drelfen Rise")),
    }


def chart(d, n, transport, doc, case, miles, explicit, certified):
    r = d.rng
    diagnosis, _, finding, options = case
    statement = r.choice(options) if explicit else r.choice(MISSING)
    clinical = [
        r.choice(("Working dx: ", "Referral impression — ", "Handoff lists ")) + diagnosis + ".",
        r.choice(("Recheck: ", "On loading, ", "Per sending RN: ")) + finding + ".",
        statement,
        r.choice(("Physician certification signed.", "Sending MD attested to necessity.")) if certified
        else r.choice(("No physician signature in transfer packet.", "Certification pending at handoff.")),
        f"Loaded leg {miles} mi.",
        r.choice(("Rec'd pt at pad.", "Pt transfered to cot.", "Lines rechecked enrt.", "Uneventful offload.")),
    ]
    # Keep provenance on individual tokens until the final rendering, even if text repeats.
    fragments = [("", s, "contra" if explicit and i == 2 else None, "")
                 for i, s in enumerate(clinical) if s]
    labels = {"name": ("Wristband reads ", "Report for "), "dob": ("Born ", "DOB on face sheet: "),
              "ssn": ("SSN ", "Registration ID (SS) "), "mrn": ("Chart #", "Sending record "),
              "phone": ("Call back ", "Family contact "), "address": ("Residence: ", "Home listed as ")}
    for kind, value in identifiers(r, n).items():
        fragments.append((r.choice(labels[kind]), value, kind, "."))
    r.shuffle(fragments)
    raw = ""
    span = (None, None, None)
    for prefix, value, kind, suffix in fragments:
        raw += (r.choice((" ", "\n", "  ")) if raw else "") + prefix
        start = len(raw)
        raw += value
        if kind == "contra":
            span = (value, start, len(raw))
        elif kind:
            d.add("truth_phi", doc, kind, value, start, len(raw))
        raw += suffix
    d.add("epcrs", doc, transport, raw)
    d.add("truth_epcrs", doc, transport, diagnosis, finding, certified, miles, explicit, *span)


def default_documents(d):
    for i, payer in enumerate(PAYERS):
        d.document(f"POL-{i}", "BILLING", payer + " transport policy", "ACTIVE", START, {
            "Sec 4.2": "Document why the available ground transport was clinically unsafe.",
            "Sec 4.3": "Include signed physician certification in the chart."})


def generate(d, hard=False):
    if hard:
        from . import hard_policy, hard_templates
        hard_policy.documents(d)
        cases = hard_templates.CLINICAL
    else:
        default_documents(d)
        cases = CASES
    missing = set(d.rng.sample(range(1, 2001), 500))
    fleet = {a["tail_number"]: a["type"] for a in d.tables["aircraft"]}
    accepted = (row for row in d.tables["dispatch_log"] if row["outcome"] == "accepted")
    for n, row in enumerate(accepted, 1):
        tr, tail, day = row["transport_id"], row["tail_number"], row["ts"].date()
        doc, claim = key("EP", n), key("CL", n)
        p = d.rng.randrange(len(PAYERS))
        case = d.rng.choice(cases)
        miles = d.rng.randint(27, 203)
        explicit = n not in missing
        if hard:
            certified = hard_templates.chart(d, n, tr, doc, case, miles, explicit)
            risk = hard_policy.reconcile(d, claim, p, day, explicit, certified, miles)
        else:
            certified = d.rng.random() < 0.96
            chart(d, n, tr, doc, case, miles, explicit, certified)
            risk = not (explicit and certified)
        status = d.rng.choices(("paid", "pending", "denied"), (4, 2, 5) if risk else (24, 2, 1))[0]
        billed = 8500 + 52*miles
        paid = billed * (71 + p*3) // 100 if status == "paid" else 0
        denial = ("CO-50" if risk else "CO-16") if status == "denied" else None
        d.add("transports", tr, row["base_id"], tail, day, PAYERS[p],
              "A0430" if fleet[tail] == "fixed-wing" else "A0431", miles, case[1], billed, paid, status)
        d.add("claims", claim, tr, PAYERS[p], min(END, day + timedelta(days=d.rng.randint(0, 3))),
              status, denial, paid)
        if not hard:
            reason = "missing ground contraindication" if not explicit else ("missing certification" if not certified else "meets policy")
            d.add("truth_claim_risk", claim, risk, reason, f"POL-{p}", "Sec 4.3" if explicit and not certified else "Sec 4.2")
