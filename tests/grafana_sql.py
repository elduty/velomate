"""Dashboard SQL as Grafana's PostgreSQL data source sends it."""

import re

# A `$` alone is not a token: `value ~ '^[0-9]+$'` is a regular expression.
UNEXPANDED_TOKEN = re.compile(r"\$__\w+|\$\{\w+|\$[A-Za-z_]\w*|\[\[\w+")


def expand(sql, variables=None, time_from=None, time_to=None):
    """Fill in a target's template variables and time macros.

    A single-value variable goes in as bare text, so any quoting belongs to the
    query. Raises ValueError on a token nothing filled in: PostgreSQL could accept
    one by accident, or the query could end up testing something else.
    """
    for name, value in (variables or {}).items():
        sql = sql.replace("${" + name + "}", str(value))
    if time_from is not None:
        sql = re.sub(
            r"\$__timeFilter\(([^)]+)\)",
            rf"\1 BETWEEN '{time_from}' AND '{time_to}'",
            sql,
        )
        sql = sql.replace("$__timeFrom()", f"'{time_from}'")
        sql = sql.replace("$__timeTo()", f"'{time_to}'")
    leftover = UNEXPANDED_TOKEN.findall(sql)
    if leftover:
        raise ValueError(f"unexpanded Grafana token {leftover} in: {sql}")
    return sql
