"""Department-specific retrieval neighbors and a third historical SOP conflict."""
from datetime import timedelta

from .common import START

# Shared work items have different adjudicators, evidence and clocks in each department.
TOPICS = ('Late handoff', 'Duplicate request', 'Missing release', 'Timing dispute',
          'Unmatched reference', 'Transfer amendment', 'Unplanned diversion',
          'Disputed quantity', 'Reopened case', 'Unsigned return')
DESKS = {
    'OPS': ('watch supervisor', 'dispatch log', 'radio timestamp', 'dispatch release'),
    'HR': ('duty coordinator', 'shift roster', 'relief timestamp', 'crew duty entry'),
    'FINANCE': ('base controller', 'monthly ledger', 'posting timestamp', 'journal adjustment'),
    'BILLING': ('claims lead', 'claim file', 'submission timestamp', 'claim correction'),
}


def generate(d):
    for dept, (owner, record, stamp, action) in DESKS.items():
        for i, topic in enumerate(TOPICS, 1):
            sections = {
                'Sec 1.1': f'When reviewing a {topic.lower()}, open a case in the {record}.',
                'Sec 2.1': f'For a {topic.lower()}, use the {stamp} to establish the sequence.',
                'Sec 2.2': f'Escalate a {topic.lower()} to the {owner} within {i+2} working hours.',
                'Sec 3.1': f'The {owner} closes the {topic.lower()} review by countersigning the {action}.',
            }
            d.document(f'{dept}-REVIEW-{i:02}', dept, topic + ' review', 'ACTIVE', START, sections)
    for doc, status, day, value, signoff in (
        ('SOP-PAD-A', 'SUPERSEDED', START-timedelta(days=80), '4 minutes', 'A lamp flash counts as clearance.'),
        ('SOP-PAD-B', 'ACTIVE', START, '9 minutes', 'Obtain a radio readback.'),
    ):
        d.document(doc, 'OPS', 'Pad reopening', status, day, {
            'Sec 5.1': f'After debris removal, observe the pad for {value}. {signoff}'})
    d.add('truth_sop_traps', 'How long is pad observation after debris removal?',
          'Which current pad procedure accepts a lamp flash as clearance?',
          'SOP-PAD-B', 'SOP-PAD-A', 'Sec 5.1', '9 minutes', '4 minutes', False)
