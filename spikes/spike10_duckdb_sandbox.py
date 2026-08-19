"""Spike #10: can DuckDB be locked down hard enough to preserve the grant model?

The grant model in design.md D2 holds only as long as a worker has no way
around it. `duckdb_query` hands the worker a SQL engine, and SQL engines read
files. This spike asks the only question that matters: after we ingest the
granted blobs, can the worker's SQL reach ANYTHING we did not grant?

Run: python spikes/spike10_duckdb_sandbox.py
"""

import tempfile, pathlib, sys
import duckdb

SECRET = pathlib.Path(tempfile.mkdtemp()) / "not-granted.csv"
SECRET.write_text("a,b\n1,2\n")
GRANTED = pathlib.Path(tempfile.mkdtemp()) / "granted.csv"
GRANTED.write_text("x,y\n10,20\n30,40\n")

SANDBOX = (
    "SET enable_external_access=false",
    "SET allow_community_extensions=false",
    "SET autoinstall_known_extensions=false",
    "SET autoload_known_extensions=false",
    "SET lock_configuration=true",   # must be LAST: freezes everything above
)

def fresh():
    conn = duckdb.connect(":memory:")
    # Ingest happens BEFORE the sandbox closes -- views are lazy, tables are not.
    conn.execute(f"CREATE TABLE t AS SELECT * FROM read_csv_auto('{GRANTED}')")
    for stmt in SANDBOX:
        conn.execute(stmt)
    return conn

ESCAPES = {
    "read granted table (must WORK)":  f"SELECT sum(x) FROM t",
    "read ungranted csv":              f"SELECT * FROM read_csv_auto('{SECRET}')",
    "read ungranted via glob":         f"SELECT * FROM read_csv_auto('{SECRET.parent}/*.csv')",
    "attach another duckdb file":      f"ATTACH '{SECRET.parent}/x.db' AS other",
    "attach sqlite job index":         f"ATTACH '{SECRET.parent}/x.db' (TYPE sqlite)",
    "COPY table out to disk":          f"COPY t TO '{SECRET.parent}/leak.csv'",
    "re-enable external access":       "SET enable_external_access=true",
    "unlock configuration":            "SET lock_configuration=false",
    "install httpfs":                  "INSTALL httpfs",
    "load an extension":               "LOAD httpfs",
    "http read":                       "SELECT * FROM read_csv_auto('https://example.com/a.csv')",
    "read local file() fn":            f"SELECT * FROM read_text('{SECRET}')",
    # Accepted: exposes only the sandbox's own config (temp dir, extension dir).
    # No granted-data bypass, so it is not worth a parser-level ban.
    "duckdb_settings introspection (ok)": "SELECT count(*) FROM duckdb_settings()",
    "getenv":                          "SELECT getenv('OPENROUTER_KEY2')",
}

print(f"duckdb {duckdb.__version__}\n")
leaks = []
for label, sql in ESCAPES.items():
    conn = fresh()
    try:
        rows = conn.execute(sql).fetchall()
        verdict = f"ALLOWED -> {str(rows)[:60]}"
        if "must WORK" not in label and "(ok)" not in label:
            leaks.append(label)
    except Exception as exc:
        verdict = f"blocked  ({type(exc).__name__}: {str(exc).splitlines()[0][:70]})"
        if "must WORK" in label:
            leaks.append(label + "  <-- BROKE THE LEGITIMATE PATH")
    finally:
        conn.close()
    print(f"  {label:34s} {verdict}")

# Multi-statement smuggling: does one execute() run two statements?
print("\nmulti-statement handling:")
conn = fresh()
rows = conn.execute("CREATE TEMP TABLE z AS SELECT 1 AS a; SELECT * FROM z").fetchall()
print(f"  execute() ran BOTH statements -> {rows}")
print("  => the tool MUST reject multi-statement input itself; duckdb will not.")
stmts = duckdb.extract_statements("SELECT 1; SELECT 2")
print(f"  extract_statements() sees {len(stmts)} statements, and types:")
for sql in ("SELECT 1", "CREATE TEMP TABLE q AS SELECT 1", "DROP TABLE t", "UPDATE t SET x=9"):
    print(f"    {sql!r:34s} {duckdb.extract_statements(sql)[0].type}")
print("  => guard = exactly one statement, type SELECT.")

# Interruptibility: a runaway query must be killable.
print("\ninterrupt:")
import threading, time
conn = fresh()
threading.Timer(0.5, conn.interrupt).start()
t0 = time.monotonic()
try:
    conn.execute("SELECT count(*) FROM range(100000000000) a, range(100000) b").fetchall()
    print("  query finished before interrupt (inconclusive)")
except Exception as exc:
    print(f"  interrupted after {time.monotonic()-t0:.2f}s ({type(exc).__name__})")

# Which pragma is actually the fence? The table above does not say -- it only
# shows the full set holding. Answered here so the design cannot overclaim.
print("\nwhich pragma is load-bearing? (external access off, NOTHING else):")
solo = duckdb.connect(":memory:")
solo.execute(f"CREATE TABLE t AS SELECT * FROM read_csv_auto('{GRANTED}')")
solo.execute("SET enable_external_access=false")
for label, sql in (
    ("re-enable it",      "SET enable_external_access=true"),
    ("widen allowed_paths", f"SET allowed_paths=['{SECRET}']"),
    ("read ungranted",    f"SELECT * FROM read_csv_auto('{SECRET}')"),
    ("LOAD httpfs",       "LOAD httpfs"),
    ("autoload on",       "SET autoload_known_extensions=true"),
):
    try:
        solo.execute(sql).fetchall()
        print(f"  {label:20s} ALLOWED")
    except Exception as exc:
        print(f"  {label:20s} blocked ({type(exc).__name__})")
solo.close()
print("  => enable_external_access=false defends itself. lock_configuration is")
print("     NOT a second fence -- it pins autoload/autoinstall, which stay")
print("     mutable without it. Keep it, but do not call it the fence.")

# The rejected alternative. `allowed_paths` looks like a per-file allowlist and
# would have let us query blobs lazily -- no memory cap, no ingest cost. It is
# not a fence: it only ADDS allowances on top of external access being on.
print("\nrejected alternative -- allowed_paths without enable_external_access=false:")
alt = duckdb.connect(":memory:")
alt.execute(f"SET allowed_paths=['{GRANTED}']")
alt.execute("SET lock_configuration=true")
for label, sql in (
    ("granted file",  f"SELECT count(*) FROM read_csv_auto('{GRANTED}')"),
    ("UNGRANTED file", f"SELECT count(*) FROM read_csv_auto('{SECRET}')"),
    ("/etc/hosts",    "SELECT length(content) FROM read_text('/etc/hosts')"),
):
    try:
        print(f"  {label:16s} ALLOWED {alt.execute(sql).fetchall()}")
        if "UNGRANTED" in label or "hosts" in label:
            leaks.append(f"allowed_paths did not fence {label}")
    except Exception as exc:
        print(f"  {label:16s} blocked ({type(exc).__name__})")
alt.close()
print("  => allowed_paths is additive, not restrictive. Ingest-then-lock is the")
print("     only sound shape, and its price is a byte cap on the blob.")

if leaks and all("allowed_paths did not fence" in x for x in leaks):
    print("\n(the allowed_paths leaks above are the EXPECTED negative result)")
    leaks = []

print("\n" + ("LEAKS: " + ", ".join(leaks) if leaks else "no leaks: sandbox holds"))
sys.exit(1 if leaks else 0)
