from datetime import datetime, time, timedelta

from .common import BASES, DAYS, FLEET, START, key, month

LOST_TRIP_VALUE = 7400


def generate(d):
    outages = {}
    for base, tails in FLEET.items():
        for i, tail in enumerate(tails):
            d.add("aircraft", tail, base, "fixed-wing" if i else "rotary", True)
    issues = (("Hydraulic pressure decayed on run-up; pump removed.", "pressure loss", "hydraulic"),
              ("Gearbox chip indication repeated on ground check.", "chip indication", "gearbox"),
              ("Starter relay stuck open during preflight.", "relay failure", "starting"),
              ("Fuel boost pump failed pressure test.", "pump failure", "fuel"))
    for i, base in enumerate(BASES[:7]):
        tail = FLEET[base][0]
        mx = key("MX", i + 1)
        day = START + timedelta(days=105 if i == 0 else d.rng.randrange(3, DAYS - 3))
        start = datetime.combine(day, time(6))
        hours = d.rng.randint(9, 16)
        end = start + timedelta(hours=hours)
        outages[tail] = (start, end)
        missed = d.rng.randint(3, 7)
        note, category, system = issues[i % len(issues)]
        d.add("truth_maintenance", mx, category, system, "AOG")
        d.add("maintenance_events", mx, tail, base, start, end, hours, note, missed)
        for j in range(missed):
            request = key("RQ", 10000 + 10*i + j)
            d.add("dispatch_log", request, base, start + timedelta(hours=j+1), "declined_aog", None, tail, mx)
            d.add("truth_aog_impacts", mx, request, base, tail, month(day), 1, LOST_TRIP_VALUE)

    for n in range(1, 2151):
        while True:
            base = d.rng.choice(BASES)
            tail = d.rng.choice(FLEET[base])
            ts = datetime.combine(START, time()) + timedelta(minutes=d.rng.randrange(DAYS * 1440))
            if tail not in outages or not outages[tail][0] <= ts < outages[tail][1]:
                break
        outcome = "accepted" if n <= 2000 else ("declined_weather" if n <= 2100 else "declined_crew")
        request = key("RQ", n)
        d.add("dispatch_log", request, base, ts, outcome, key("TR", n) if n <= 2000 else None, tail, None)
        if outcome == "declined_weather":
            d.add("weather_declines", request, base, ts, d.rng.choice(("Low ceiling", "Icing forecast", "Fog at pad")))

    for i, (title, subject, old, new) in enumerate((
        ("Rotor restart inspection", "minimum observation time", "15 minutes", "30 minutes"),
        ("Reserve crew activation", "maximum callback interval", "45 minutes", "20 minutes"),
    )):
        prior, active = f"SOP-{i}-A", f"SOP-{i}-B"
        retired = f"Legacy checklist {i+1} allowed verbal signoff."
        for doc, status, value, day in ((prior, "SUPERSEDED", old, START-timedelta(days=60)),
                                        (active, "ACTIVE", new, START)):
            body = f"The {subject} is {value}. " + (retired if doc == prior else "Record signoff in the duty log.")
            d.document(doc, "OPS", title, status, day, {"Sec 4.2": body})
        d.add("truth_sop_traps", f"For {title.lower()}, what is the current {subject}?",
              f"Which current SOP authorizes verbal signoff under legacy checklist {i+1}?",
              active, prior, "Sec 4.2", new, old, False)
