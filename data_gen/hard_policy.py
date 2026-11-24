"""Policies apply on the date of service, including the revision's first day."""
from datetime import date

from .billing import PAYERS
from .common import START

REVISION = date(2026, 8, 15)
CAP = 100


def documents(d):
    for p, payer in enumerate(PAYERS):
        revised = p < 2
        for version in ((1, 2) if revised else (1,)):
            requires_cert = p == 2 or (p == 0 and version == 2)
            capped = p == 1 and version == 2
            sections = {
                'Sec 4.2': 'Record the reason surface transfer could not safely serve this patient.',
                'Sec 4.3': ('An attending must sign the medical necessity form.' if requires_cert else
                            'An attending signature is optional for coverage.'),
                'Sec 4.4': (f'Coverage ends at {CAP} patient-loaded statute miles per trip.' if capped else
                            'Patient-loaded distance carries no coverage ceiling.'),
            }
            d.document(f'POL-{p}-V{version}', 'BILLING', payer + ' transport policy',
                       'SUPERSEDED' if revised and version == 1 else 'ACTIVE',
                       REVISION if version == 2 else START, sections)


def reconcile(d, claim, p, day, ground, signed, miles):
    v2 = p < 2 and day >= REVISION
    version = 2 if v2 else 1
    doc = f'POL-{p}-V{version}'
    if not ground:
        reason, section = 'missing ground contraindication', 'Sec 4.2'
    elif (p == 2 or (p == 0 and v2)) and not signed:
        reason, section = 'missing certification', 'Sec 4.3'
    elif p == 1 and v2 and miles > CAP:
        reason, section = 'exceeds mileage cap', 'Sec 4.4'
    else:
        reason, section = 'meets policy', 'Sec 4.2'
    risk = reason != 'meets policy'
    d.add('truth_policy_version', claim, PAYERS[p], day, version, doc, REVISION if v2 else START)
    d.add('truth_claim_risk', claim, risk, reason, doc, section)
    return risk
