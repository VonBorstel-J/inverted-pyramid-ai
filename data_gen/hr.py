from datetime import timedelta

from .common import BASES, DAYS, END, START, key

OT_RATE = 78
DUTY_LIMIT = 14


def generate(d):
    flights = {(r["base_id"], r["ts"].date()) for r in d.tables["dispatch_log"] if r["outcome"] == "accepted"}
    outages = {(e["base_id"], e["start"].date()): e for e in d.tables["maintenance_events"]}
    for b, base in enumerate(BASES):
        for k, (role, kind) in enumerate((("pilot", "flight certificate"), ("flight nurse", "RN"),
                                          ("flight paramedic", "paramedic"), ("mechanic", "airframe")) * 2):
            crew = key("CR", 8*b+k+1)
            d.add("crew", crew, base, role, START-timedelta(days=d.rng.randint(65, 3400)))
            expires = (START + timedelta(days=d.rng.randint(30, 110)) if k == 0 else
                       END + timedelta(days=d.rng.randint(30, 700)))
            d.add("licenses", crew, kind, expires)
            if expires <= END:
                d.add("truth_license_expiries", crew, kind, expires)
            for offset in range(DAYS):
                if offset % 2 != k // 4:
                    continue
                day = START + timedelta(days=offset)
                event = outages.get((base, day))
                ot = d.rng.randint(3, 5) if event else 0
                reason = f"Recovery cover for {event['tail_number']} AOG." if event else "Scheduled duty."
                shift = key("SH", (8*b+k)*DAYS+offset+1)
                flown = role != "mechanic" and (base, day) in flights
                d.add("shifts", shift, crew, day, 12+ot, ot, reason, event["mx_id"] if event else None, flown)
                if event:
                    d.add("truth_overtime", shift, event["mx_id"], ot, ot*OT_RATE)
                if 12+ot > DUTY_LIMIT or (flown and day > expires):
                    d.add("truth_shift_flags", shift, 12+ot > DUTY_LIMIT, flown and day > expires)
    d.document("HR-DUTY", "HR", "Duty and relief", "ACTIVE", START, {
        "Sec 2.1": f"Duty may not exceed {DUTY_LIMIT} hours per shift. Record all recovery overtime."})
    d.document("HR-LIC", "HR", "Flight credential checks", "ACTIVE", START, {
        "Sec 3.4": "Credentials remain valid through the expiry date. Remove crew from flight duty the following day."})
