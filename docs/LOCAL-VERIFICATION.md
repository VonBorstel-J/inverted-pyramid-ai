# Local prototype verification

The November 24, 2026 checks used synthetic data and the Python package versions in requirements.txt.

| Check | Observed result |
| --- | --- |
| Automated tests | 100 passed, including a regression for contextual identifiers |
| Console navigation | All seven screens loaded in Streamlit AppTest. The four department screens and the cross-department screen each returned a response for their example question. |
| Billing lookup | CL-2026-000598 returned its policy section. An unknown ID returned an empty-result message. |
| Cross-department calculation | Event MX-2026-000001 returned $22,200 estimated lost revenue plus $1,092 in overtime, totaling $23,292. |
| Fresh warehouse build | Generated the hard split with seed 73, ran the rules pipeline, and queried the five department and executive views. All twelve processing steps completed with zero rejected rows. |
| Browser delivery | The local console health endpoint returned success. The public guide returned its HTML to an unauthenticated request. |

## Redaction after the benchmark

The phone comparison used the original redactor. After examining the synthetic narrative formats, the updated rules added contextual names, spoken phone numbers, phone extensions, written dates and ages, partial identifiers and multiline wristband numbers. The checks below compare the text visible through the billing view against the generator’s character spans.

| Dataset | Charts present in billing view | Planted identifier occurrences | Occurrences left in text |
| --- | ---: | ---: | ---: |
| Default, seed 42 | 2,000 | 12,000 | 0 |
| Hard, seed 42 | 2,000 | 26,772 | 0 |
| Hard, seed 73 | 2,000 | 26,757 | 0 |

The second hard seed varies the generated records within the same template families. Unfamiliar chart formats and real records would require separate evidence. The role selector and loopback binding are appropriate for this single-user synthetic demonstration. A multi-user deployment would also require authenticated identities and server-enforced role assignment.

To repeat the checks, run `.venv/bin/python -m unittest discover -q` and `.venv/bin/python -m evals.audit_redaction warehouse.duckdb` from the repository root. The latter command also accepts a rebuilt hard warehouse.
