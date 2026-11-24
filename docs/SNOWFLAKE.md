# Local architecture and the planned Snowflake port

The prototype runs in DuckDB. Its database layers and application responsibilities provide the following starting points for a separate Snowflake implementation.

| Local component | Responsibility | Planned Snowflake counterpart |
| --- | --- | --- |
| Bronze, silver and gold schemas | Preserve original records, validate extracted fields, then publish joined metrics | Separate warehouse schemas |
| Billing wave | Extract fields from charts and remove identifiers | AI_EXTRACT and AI_REDACT, scored against the same synthetic fixtures |
| Local document index | Find supporting sections and return document IDs and effective dates | Cortex Search |
| Metric catalog and SQL views | Calculate amounts and rates with repeatable definitions | Semantic Views and Cortex Analyst |
| Role-specific views | Limit which rows and columns each application view reads | Roles, masking policies and row access policies |
| Local Streamlit console | Present department records, citations and evaluation results | Streamlit in Snowflake |

A port would need its own access configuration, evaluation and cost measurements. All measurements in the local walkthrough come from the synthetic phone prototype.
