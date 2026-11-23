"""Versioned prompts for the model tasks. Bump the version when the text
changes; it is stored in prov_prompt_version on every row."""

BILLING_NAMES = ("billing_names.v1", """You find person names in a synthetic air-medical chart.
List every person name (patient, relatives, clinicians) exactly as written, including
"Family, Given" and middle-initial forms. Do not list places, drugs, diagnoses or units.

Chart:
<<<
{text}
>>>""")

BILLING_EXTRACT = ("billing_extract.v1", """Extract fields from this de-identified air-medical transport chart.
Rules:
- diagnosis: the working diagnosis as written, or null.
- instability_evidence: the clinical finding showing instability (vitals, rhythm, GCS...), as written, or null.
- ground_contraindication: copy VERBATIM the sentence that says why ground (road/ambulance) transport
  was clinically unsafe or impossible; null if the chart does not say so. Never paraphrase.
- physician_certification: true only if the chart says a physician signed or attested; pending or missing is false.
- loaded_miles: the loaded mileage number as written, or null. Do not compute anything.

Chart:
<<<
{text}
>>>""")

OPS_MX = ("ops_mx_classify.v1", """Classify this synthetic aircraft maintenance note.
- issue_category: a short lowercase phrase for the failure mode (for example "pressure loss",
  "chip indication", "relay failure", "pump failure", "leak", "crack", "inspection").
- system: one lowercase word for the aircraft system (hydraulic, gearbox, starting, fuel, avionics,
  electrical, engine, rotor, landing gear, airframe, other).
- severity: "AOG" if the aircraft was grounded / out of service, "deferred" if deferred or monitored,
  else "routine".
Aircraft on ground (from the maintenance system): {aog}

Note:
<<<
{text}
>>>""")

HR_OVERTIME = ("hr_overtime_cause.v1", """Classify why this synthetic crew shift ran overtime.
- cause: one of aog (aircraft on ground / maintenance recovery), weather, staffing (call-outs,
  vacancies, coverage), training, mission (long or late transport), scheduled, other.
- tail_number: the aircraft tail number (format N###XX) if the text names one, else null.
- mentions_aog: true if the text mentions an AOG, grounded aircraft or maintenance outage.

Shift note:
<<<
{text}
>>>""")
