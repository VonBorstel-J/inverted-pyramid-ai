"""Held-out narrative families. Template fields mark exact identifier occurrences."""
from string import Formatter

CLINICAL = (
    ("STEMI", "I21.3", "wide runs recurred between tracings"),
    ("head injury", "S06.9XAA", "eye opening ceased during packaging"),
    ("respiratory failure", "J96.01", "saturation stalled at 83% on assisted breaths"),
    ("shock", "R57.9", "radial pulses faded between boluses"),
)
HISTORY = (
    'Spoke with {family} about her husband’s meds. {patient} goes by “{nickname}” on the unit.',
    '{patient} squeezed my hand when {family} used the old nickname {nickname}.',
    'Between breaths {patient} asked for {family}. We used {nickname} per their wish.',
)
ROAD = (
    'Ground was considered. The pass was closed and ALS had no other way through.',
    'Family requested road transport; declined due to the washed-out hwy.',
    'CCT called enrt for a road intercept. ETA put definitive care outside the window.',
    'Update from county: brdge iced over; rig turned back.',
    'Ambulnce at the door already. Its only pump was out of service.',
)
MISSING = (
    'No contraindication to ground noted.',
    'Correction from dispatch: pass open, road rig cleared.',
    'ALS enrt by hwy. Nobody recorded why the cot went to the pad.',
)
CERT = (
    ('Physician signed the flight necessity certificate before departure.', True),
    ('The necessity form came back from the attending bearing her signature.', True),
    ('The flight necessity page came back unsigned.', False),
    ('Only the transfer inventory accompanied the cot.', False),
)
ONES = 'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen'.split()
TENS = 'zero ten twenty thirty forty fifty sixty seventy eighty ninety'.split()


def words(n):
    if n < 20:
        return ONES[n]
    if n < 100:
        return TENS[n // 10] + ('-' + ONES[n % 10] if n % 10 else '')
    return ONES[n // 100] + ' hundred' + (' ' + words(n % 100) if n % 100 else '')


def distance(r, miles):
    return r.choice((
        f'With the cot occupied we logged {words(miles)} statute miles.',
        f'Crew estimated {miles-3}–{miles+3} mi with patient aboard; billing uses the midpoint.',
        f'Patient-on segment {miles/1.150779448:.2f} NM; round converted statute miles to nearest whole.',
        f'Cot leg pencilled in as {miles+12} mi. Postflight amendment: {miles} mi with patient.',
    ))


def chart(d, n, transport, doc, case, miles, explicit):
    r = d.rng
    diagnosis, _, finding = case
    cert, signed = r.choice(CERT)
    older = r.random() < 0.4
    values = dict(patient=r.choice(('Reed', 'Vale', 'Sable')), family=r.choice(('Marisol', 'Wren', 'Briar')),
                  nickname=r.choice(('Sunny', 'Pip', 'Tansy')), provider=r.choice(('Ilex Venn', 'Neri Brume')),
                  dob="the third of May, ’33" if older else "the third of May, ’58", age='ninety-three',
                  address=f'{n+18} Larkglass Mews', ssn=f'{n:04}', mrn=f'KX-{n//100:02}\n{n%100:02}-R',
                  phone=r.choice((f'202 555 0142 ext {n+10}', 'two zero two five five five zero one four two')),
                  email=f'cot{n}@example.invalid', plate=f'QZ-{n:04}-V', serial=f'VX/{n:05}/Q',
                  url=f'https://example.invalid/cot/{n}')
    blocks = [r.choice(HISTORY), '{provider} received the handoff from our crew.',
              'From {address}, born {dob}' + (' and now {age}.' if older else '.'),
              r.choice(('SS card ends {ssn}; band copied as {mrn}.', 'Band {mrn} agrees with intake, SS suffix {ssn}.')),
              r.choice(('Reach the home line at {phone}.', 'Relay calls through {phone}.')),
              r.choice(('{email} was left for follow-up. Parking slip shows {plate}.',
                        'Send the receipt to {email}; car at the gate wore {plate}.')),
              'Monitor {serial} uploaded its strip to {url}.',
              f'Transfer for {diagnosis}; {finding}.', '{road}', cert, distance(r, miles)]
    values['road'] = r.choice(ROAD if explicit else MISSING)
    r.shuffle(blocks)
    # Place the preliminary call before the chart, leaving only the final evidence in truth.
    if values['road'].startswith('Update from county'):
        blocks.insert(0, 'Dispatch first cleared the road rig.')
    elif values['road'].startswith('Correction from dispatch'):
        blocks.insert(0, 'County initially reported the pass shut.')
    raw = ''
    contra = (None, None, None)
    for block in blocks:
        if raw:
            raw += r.choice((' ', '\n'))
        for literal, field, _, _ in Formatter().parse(block):
            raw += literal
            if field:
                start = len(raw)
                raw += values[field]
                span = (values[field], start, len(raw))
                if field == 'road':
                    if explicit:
                        contra = span
                else:
                    kind = 'name' if field in ('patient', 'family', 'nickname', 'provider') else field
                    d.add('truth_phi', doc, kind, *span)
    d.add('epcrs', doc, transport, raw)
    d.add('truth_epcrs', doc, transport, diagnosis, finding, signed, miles, explicit, *contra)
    return signed


ISSUES = {
    'hydraulic': 'Needle bled down twice after the controls were cycled. Held off line.',
    'gearbox': 'Metal fuzz came back on the transmission plug; no release.',
    'starting': 'No crank with power at the coil; contacts stayed apart. Pulled from rotation.',
    'fuel': 'Tank feed would not build head with boost selected. Ship stayed put.',
}


def rewrite_duty(d):
    events = {row['mx_id']: row for row in d.tables['maintenance_events']}
    for truth in d.tables['truth_maintenance']:
        event = events[truth['mx_id']]
        event['issue_text'] = f"Shop got the {event['tail_number'][1:4]} bird. " + ISSUES[truth['system']]
    for shift in d.tables['shifts']:
        if shift['mx_id']:
            event = events[shift['mx_id']]
            alias = ('our H135 at DEN' if event['tail_number'] == 'N412AM' else
                     f"the {event['tail_number'][1:4]} bird")
            shift['reason_text'] = d.rng.choice((f'Stayed past relief until {alias} cleared shop.',
                                                f'Covered the desk while they chased parts for {alias}.'))
        else:
            shift['reason_text'] = d.rng.choice(('Handed over on the rostered bell.', 'Relief met us on time.'))
