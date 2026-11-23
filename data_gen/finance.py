from collections import Counter

from .common import BASES, FLEET, MONTHS, START, month
from .hr import OT_RATE
from .ops import LOST_TRIP_VALUE


def generate(d):
    revenue, losses, labor, overtime, repairs = (Counter() for _ in range(5))
    leases = {}
    for a in d.tables["aircraft"]:
        tail = a["tail_number"]
        leases[tail] = d.rng.randrange(13500, 21600, 100)
        d.add("leases", tail, leases[tail])
    for t in d.tables["transports"]:
        revenue[t["base_id"], month(t["date"])] += t["collected_amount"]
    for r in d.tables["dispatch_log"]:
        if r["outcome"] == "declined_aog":
            losses[r["base_id"], month(r["ts"].date())] += LOST_TRIP_VALUE
    crew_base = {r["crew_id"]: r["base_id"] for r in d.tables["crew"]}
    for s in d.tables["shifts"]:
        idx = (crew_base[s["crew_id"]], month(s["date"]))
        labor[idx] += (s["hours"]-s["overtime_hours"])*52
        overtime[idx] += s["overtime_hours"]*OT_RATE
    for e in d.tables["maintenance_events"]:
        repairs[e["base_id"], month(e["start"].date())] += 325*e["aog_hours"]
    for base in BASES:
        for m in MONTHS:
            idx = (base, m)
            fuel = d.rng.randrange(18000, 24000, 100)
            actual = dict(revenue=revenue[idx], fuel=fuel, maintenance_cost=12000+repairs[idx],
                          crew_cost=labor[idx]+overtime[idx], lease_cost=sum(leases[t] for t in FLEET[base]))
            drivers = {"revenue": (-losses[idx], "AOG missed requests"), "crew_cost": (overtime[idx], "AOG recovery overtime"),
                       "maintenance_cost": (repairs[idx], "AOG repairs")}
            d.add("base_ledger", *idx, *actual.values())
            for line, value in actual.items():
                variance, cause = drivers.get(line, (0, ""))
                planned = value - variance
                d.add("budget", *idx, line, planned)
                if variance:
                    d.add("truth_ledger_variances", *idx, line, planned, value, variance, cause)
    d.document("FIN-REV", "FINANCE", "Base revenue reconciliation", "ACTIVE", START, {
        "Sec 1.1": "Recognize collections in the service month. Model the no-outage budget using actual collections plus "
                   f"${LOST_TRIP_VALUE} in expected net receipts for each missed AOG request."})
    d.document("FIN-LAB", "FINANCE", "Recovery expense allocation", "ACTIVE", START, {
        "Sec 2.3": f"Charge recovery overtime at ${OT_RATE} per hour and repair labor and parts at $325 per outage hour."})
