"""Role-based access: one DuckDB schema per role (governance/views.sql), and a
query entry point that only lets a role read its own schema."""
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROLES = ("billing_phi", "billing", "ops", "finance", "hr", "exec")
PHI_ROLES = frozenset({"billing_phi"})
_OTHER = re.compile(r"\b(bronze|silver|gold|main|information_schema|duckdb_\w+|pg_catalog|role_\w+)\s*\.", re.I)
_FORBIDDEN = re.compile(r"\b(attach|copy|export|import|install|load|pragma|set|create|drop|alter|insert|update|delete)\b"
                        r"|read_\w+\s*\(|\bglob\s*\(", re.I)


class AccessDenied(PermissionError):
    pass


def schema_for(role):
    if role not in ROLES:
        raise AccessDenied(f"unknown role {role!r}")
    return f"role_{role}"


def apply(con):
    """(Re)build every role schema. Needs bronze, silver and gold in place."""
    con.execute((HERE / "views.sql").read_text())


def tables(con, role):
    return [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = ? ORDER BY 1",
        [schema_for(role)]).fetchall()]


def query(con, role, sql, params=None):
    """Run a read-only SELECT as `role`: unqualified names resolve to the role's
    schema; references to any other schema are refused."""
    own = schema_for(role)
    s = sql.strip().rstrip(";")
    if ";" in s or not re.match(r"^\s*(select|with)\b", s, re.I):
        raise AccessDenied("one SELECT statement only")
    if _FORBIDDEN.search(s):
        raise AccessDenied("statement uses a forbidden keyword or table function")
    for m in _OTHER.finditer(s):
        if m.group(1).lower() != own:
            raise AccessDenied(f"role {role} may not read {m.group(1)}")
    s = re.sub(rf"\b{own}\s*\.", "", s, flags=re.I)
    cur = con.cursor()
    try:
        cur.execute(f"SET search_path = '{own}'")
        res = cur.execute(s, params or [])
        cols = [d[0] for d in res.description]
        return [dict(zip(cols, r)) for r in res.fetchall()]
    finally:
        cur.close()
