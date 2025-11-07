"""API endpoints for executing DML (Data Manipulation Language) statements."""

import datetime
import decimal
import re
import time
import uuid
from typing import Any, List, Optional

import sqlparse
from common.otlp.trace.span import Span
from common.service import get_otlp_metric_service, get_otlp_span_service
from common.utils.snowfake import get_id
from fastapi import APIRouter, Depends
from memory.database.api.schemas.exec_dml_types import ExecDMLInput
from memory.database.api.v1.common import (
    check_database_exists_by_did,
    check_space_id_and_get_uid,
)
from memory.database.domain.entity.general import exec_sql_statement, parse_and_exec_sql
from memory.database.domain.entity.schema import set_search_path_by_schema
from memory.database.domain.entity.views.http_resp import format_response
from memory.database.exceptions.e import CustomException
from memory.database.exceptions.error_code import CodeEnum
from memory.database.repository.middleware.getters import get_session
from sqlglot import exp, parse_one
from sqlglot.expressions import (
    Delete,
    Func,
    Identifier,
    Insert,
    Select,
    Subquery,
    Table,
    Update,
)
from sqlmodel.ext.asyncio.session import AsyncSession
from starlette.responses import JSONResponse

exec_dml_router = APIRouter(tags=["EXEC_DML"])

INSERT_EXTRA_COLUMNS = ["id", "uid", "create_time", "update_time"]


def rewrite_dml_with_uid_and_limit(
    dml: str,
    app_id: str,
    uid: str,
    limit_num: int,
    env: str,  # pylint: disable=unused-argument
    span_context: Span,  # pylint: disable=unused-argument
) -> tuple[str, list]:
    """
    Rewrite DML with UID and limit expressions.

    Args:
        dml: Original DML statement
        app_id: Application ID
        uid: User ID
        limit_num: Limit number for SELECT queries
        env: Environment (prod/test)
        span_context: Span context for tracing

    Returns:
        tuple: (rewritten_sql, insert_ids)
    """
    parsed = parse_one(dml)
    insert_ids: List[int] = []

    tables = [table.alias_or_name for table in parsed.find_all(exp.Table)]

    if isinstance(parsed, (exp.Update, exp.Delete, exp.Select)):
        _dml_add_where(parsed, tables, app_id, uid)

    if isinstance(parsed, exp.Select):
        limit = parsed.args.get("limit")
        if not limit:
            parsed.set("limit", exp.Limit(expression=exp.Literal.number(limit_num)))

    if isinstance(parsed, exp.Insert):
        _dml_insert_add_params(parsed, insert_ids, app_id, uid)

    return parsed.sql(dialect="postgres"), insert_ids


def _dml_add_where(parsed: Any, tables: List[str], app_id: str, uid: str) -> None:
    """Add WHERE conditions to DML statements."""
    where_expr = parsed.args.get("where")
    uid_conditions = []

    for table in tables:
        uid_col = exp.Column(this="uid", table=table)
        condition = exp.In(
            this=uid_col,
            expressions=[
                exp.Literal.string(f"{uid}"),
                exp.Literal.string(f"{app_id}:{uid}"),
            ],
        )
        uid_conditions.append(condition)

    final_condition = uid_conditions[0]
    for cond in uid_conditions[1:]:
        final_condition = exp.and_(final_condition, cond)  # type: ignore[assignment]

    if where_expr:
        grouped_where = exp.Paren(this=where_expr.this)
        new_where = exp.and_(grouped_where, final_condition)
    else:
        new_where = final_condition

    parsed.set("where", exp.Where(this=new_where))


def _dml_insert_add_params(
    parsed: Any, insert_ids: List[int], app_id: str, uid: str
) -> None:
    """Add parameters to INSERT statements."""
    existing_columns = parsed.args["this"].expressions or []
    insert_exprs = parsed.args["expression"]
    rows = insert_exprs.expressions

    extra_fields = ["id", "uid"]

    need_del_index = []
    for index, column in enumerate(existing_columns):
        if column.this in INSERT_EXTRA_COLUMNS:
            need_del_index.append(index)

    need_del_index.reverse()
    for index in need_del_index:
        existing_columns.pop(index)
        for row in rows:
            row.expressions.pop(index)

    for name in extra_fields:
        existing_columns.append(exp.to_identifier(name))

    for i, row in enumerate(rows):
        row_id = get_id()
        insert_ids.append(row_id)
        extra_values = [
            exp.Literal.number(row_id),
            exp.Literal.string(f"{app_id}:{uid}"),
        ]
        new_exprs = list(row.expressions) + [val.copy() for val in extra_values]
        rows[i] = exp.Tuple(expressions=new_exprs)

    parsed.set("columns", exp.Tuple(this=existing_columns))
    parsed.set("expression", insert_exprs)


def to_jsonable(obj: Any) -> Any:
    """Convert object to JSON-serializable format."""
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(item) for item in obj]
    if isinstance(obj, datetime.datetime):
        return obj.isoformat(sep=" ", timespec="seconds")
    if isinstance(obj, datetime.date):
        return obj.isoformat()
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    if isinstance(obj, uuid.UUID):
        return str(obj)
    return obj


def _validate_sql_injection(dml: str, span_context: Any) -> Any:
    """
    Validate DML statement for SQL injection risks using PostgreSQL official AST parsing.

    This function follows the same approach as DDL validation, using sqlglot's
    PostgreSQL parser to construct and validate the AST, then reconstruct
    a safe version of the statement.

    Args:
        dml: The DML statement to validate
        span_context: Span context for logging

    Returns:
        JSONResponse with error if injection detected, None if safe
    """
    try:
        span_context.add_info_event(f"validating_dml_sql: {dml}")

        # Parse using PostgreSQL dialect for accurate parsing
        parsed = parse_one(dml.strip(), dialect="postgres", error_level="raise")

        if not parsed:
            span_context.add_error_event("Failed to parse DML statement")
            return format_response(
                code=CodeEnum.SQLParseError.code,
                message="Invalid DML statement structure",
                sid=span_context.sid,
            )

        # Extract and validate DML statement information using AST
        statement_info = _extract_dml_statement_info(parsed)
        if not statement_info:
            span_context.add_error_event("Unknown or invalid DML statement type")
            return format_response(
                code=CodeEnum.DMLNotAllowed.code,
                message="Only SELECT, INSERT, UPDATE, DELETE statements are allowed",
                sid=span_context.sid,
            )

        statement_type = statement_info

        # Validate AST structure for security
        if not _validate_dml_ast_security(parsed, span_context):
            return format_response(
                code=CodeEnum.DMLNotAllowed.code,
                message="DML statement contains unsafe elements",
                sid=span_context.sid,
            )

        # Reconstruct safe SQL using AST components
        safe_sql = _reconstruct_safe_dml_statement(parsed, statement_type, span_context)
        if not safe_sql:
            span_context.add_error_event("Failed to reconstruct safe DML statement")
            return format_response(
                code=CodeEnum.DMLNotAllowed.code,
                message="DML statement failed security reconstruction",
                sid=span_context.sid,
            )

        # Validate the reconstructed SQL structure
        if not _validate_reconstructed_dml(safe_sql, statement_type, span_context):
            span_context.add_error_event("Reconstructed DML failed validation")
            return format_response(
                code=CodeEnum.DMLNotAllowed.code,
                message="DML statement security validation failed",
                sid=span_context.sid,
            )

        span_context.add_info_event(f"safe_reconstructed_dml: {safe_sql}")
        span_context.add_info_event("DML SQL injection validation passed")
        return None

    except Exception as validation_error:
        span_context.add_error_event(f"DML validation error: {str(validation_error)}")
        return format_response(
            code=CodeEnum.SQLParseError.code,
            message="DML statement parsing failed",
            sid=span_context.sid,
        )


def _extract_dml_statement_info(parsed_ast: Any) -> Optional[str]:
    """
    Extract DML statement type from parsed AST using official SQLGlot methods.

    Args:
        parsed_ast: Parsed SQLGlot AST

    Returns:
        str: Statement type (SELECT, INSERT, UPDATE, DELETE) or None if not DML
    """
    if isinstance(parsed_ast, Select):
        return "SELECT"
    elif isinstance(parsed_ast, Insert):
        return "INSERT"
    elif isinstance(parsed_ast, Update):
        return "UPDATE"
    elif isinstance(parsed_ast, Delete):
        return "DELETE"
    return None


def _validate_dml_ast_security(parsed_ast: Any, span_context: Any) -> bool:
    """
    Validate DML AST structure for security using PostgreSQL official parsing.

    Args:
        parsed_ast: Parsed SQLGlot AST
        span_context: Span context for logging

    Returns:
        bool: True if AST is secure, False otherwise
    """
    try:
        span_context.add_info_event("Starting DML AST security validation")

        # Check for multiple statement injection
        if _contains_multiple_dml_statements(parsed_ast):
            span_context.add_error_event(
                "Multiple statements detected (potential injection)"
            )
            return False
        span_context.add_info_event("✓ Multiple statement check passed")

        # Validate all identifiers in the AST
        if not _validate_dml_identifiers(parsed_ast):
            span_context.add_error_event("Invalid identifiers detected")
            return False
        span_context.add_info_event("✓ Identifier validation passed")

        # Check for dangerous functions or expressions
        if _contains_dangerous_expressions(parsed_ast, span_context):
            span_context.add_error_event("Dangerous expressions detected")
            return False
        span_context.add_info_event("✓ Dangerous expression check passed")

        # Validate table references
        if not _validate_table_references(parsed_ast):
            span_context.add_error_event("Invalid table references")
            return False
        span_context.add_info_event("✓ Table reference validation passed")

        span_context.add_info_event("All DML AST security checks passed")
        return True

    except Exception as e:
        span_context.add_error_event(
            f"Exception in DML AST security validation: {str(e)}"
        )
        return False


def _contains_multiple_dml_statements(parsed_ast: Any) -> bool:
    """
    Check if AST contains multiple DML statements using SQLGlot's official traversal.

    This function uses SQLGlot's find_all method to accurately count top-level
    DML statements, which is more reliable than regex-based approaches.
    """
    # Count top-level DML statement nodes using SQLGlot's AST traversal
    dml_types = (Select, Insert, Update, Delete)

    # Count all DML statements in the entire AST
    all_statements = list(parsed_ast.find_all(*dml_types))

    # For security, we need to ensure there's only one top-level statement
    # Subqueries are allowed (they're part of the single statement)
    # But multiple independent statements indicate potential injection

    # The root node should be one of our DML types
    if not isinstance(parsed_ast, dml_types):
        return True  # Root is not a DML statement

    # Count statements at the same level as the root
    # This is more complex but necessary for accurate detection
    return len(all_statements) > _count_expected_statements(parsed_ast)


def _count_expected_statements(parsed_ast: Any) -> int:
    """
    Count the expected number of statements for a given AST structure.

    This helps distinguish between legitimate subqueries and injection attacks.
    """
    expected_count = 1  # The main statement

    # Add expected counts for legitimate subqueries
    if isinstance(parsed_ast, Select):
        # SELECT statements can have subqueries in various places
        subqueries = list(parsed_ast.find_all(Subquery))
        expected_count += len(subqueries)

    elif isinstance(parsed_ast, Insert):
        # INSERT can have SELECT subqueries
        subqueries = list(parsed_ast.find_all(Subquery))
        expected_count += len(subqueries)

    elif isinstance(parsed_ast, (Update, Delete)):
        # UPDATE/DELETE can have subqueries in WHERE clauses
        subqueries = list(parsed_ast.find_all(Subquery))
        expected_count += len(subqueries)

    return expected_count


def _validate_dml_identifiers(parsed_ast: Any) -> bool:
    """
    Validate all identifiers in the DML AST using PostgreSQL standards.

    This function checks identifiers using SQLGlot's AST structure and PostgreSQL's
    official identifier rules, while properly handling special SQL elements.
    """
    # Use SQLGlot's find_all method to get all identifier nodes
    identifiers = list(parsed_ast.find_all(Identifier))

    for identifier in identifiers:
        if not identifier.this:
            continue

        identifier_str = str(identifier.this)

        # Skip validation for SQL keywords and special tokens that SQLGlot
        # might parse as identifiers in certain contexts
        sql_keywords_and_specials = {
            "*",
            "null",
            "true",
            "false",
            "default",
            "current_timestamp",
            "current_date",
            "current_time",
            "current_user",
            "session_user",
            "user",
            "now",
            "today",
            "yesterday",
            "tomorrow",
        }

        if identifier_str.lower() in sql_keywords_and_specials:
            continue

        # Additional security check: detect suspicious identifier patterns
        # that are commonly used in SQL injection attacks
        if _is_suspicious_identifier_pattern(identifier_str):
            return False

        # Validate the identifier according to PostgreSQL rules
        if not _is_valid_postgresql_identifier(identifier_str):
            return False

    return True


def _is_suspicious_identifier_pattern(identifier: str) -> bool:
    """
    Detect suspicious identifier patterns commonly used in SQL injection.

    This function uses string analysis based on PostgreSQL security best practices
    to identify patterns often used in injection attacks but rarely in legitimate queries.
    Uses direct string matching rather than regex for better performance and clarity.
    """
    # Convert to lowercase for pattern matching
    lower_id = identifier.lower()

    # Suspicious patterns commonly used in SQL injection (using direct string matching)
    suspicious_keywords = [
        # Information schema access patterns
        "information_schema",
        "pg_catalog",
        "pg_class",
        "pg_tables",
        "pg_database",
        # System function patterns
        "@@version",
        "@@servername",
        # SQL injection payloads
        "union_select",
        "union select",
    ]

    # Check against suspicious keywords using direct string matching
    for keyword in suspicious_keywords:
        if keyword in lower_id:
            return True

    # Check for hexadecimal patterns (0x followed by hex digits)
    if lower_id.startswith("0x") and len(lower_id) > 2:
        hex_part = lower_id[2:]
        # Check if all characters after 0x are valid hex digits
        if all(c in "0123456789abcdef" for c in hex_part):
            return True

    # Check for function call patterns in identifiers (suspicious)
    function_patterns = [
        "char(",
        "concat(",
        "unhex(",
        "convert(",
        "user()",
        "database()",
    ]

    for pattern in function_patterns:
        if pattern in lower_id:
            return True

    # Check for boolean injection patterns
    boolean_patterns = [
        "1=1",
        "0=0",
        "true=true",
        "'1'='1'",
        '"1"="1"',
    ]

    for pattern in boolean_patterns:
        if pattern in lower_id:
            return True

    return False


def _contains_dangerous_expressions(parsed_ast: Any, span_context: Any) -> bool:
    """
    Check for dangerous expressions based on PostgreSQL official documentation.

    This function follows PostgreSQL security best practices by implementing
    context-aware detection rather than blanket function blocking. Based on:
    - https://www.postgresql.org/docs/current/functions-info.html
    - https://www.postgresql.org/docs/current/functions-string.html
    - https://www.postgresql.org/docs/current/sql-expressions.html

    The approach focuses on detecting injection patterns rather than blocking
    legitimate PostgreSQL functionality.
    """

    # Truly dangerous functions that should NEVER be allowed in user queries
    # These pose direct security risks regardless of context
    absolutely_dangerous_functions = {
        # File system access functions
        "pg_read_file",
        "pg_read_binary_file",
        "pg_write_file",
        "pg_ls_dir",
        "pg_stat_file",
        # Process control functions
        "pg_sleep",
        "pg_cancel_backend",
        "pg_terminate_backend",
        # System administration functions
        "pg_reload_conf",
        "pg_rotate_logfile",
        # System commands
        "copy_from_program",
        "copy_to_program",
        # Configuration modification functions
        "set_config",
    }

    # Information disclosure functions - dangerous in WHERE clauses but may be OK in SELECT
    # These are commonly used in SQL injection for information gathering
    info_disclosure_functions = {
        "current_catalog",
        "current_database",
        "current_role",
        "current_schema",
        "current_schemas",
        "current_user",
        "session_user",
        "user",
        "version",
        "pg_backend_pid",
        "pg_postmaster_start_time",
        "pg_conf_load_time",
        "current_setting",  # Read-only current_setting may be OK in some contexts
    }

    # String functions commonly used in blind SQL injection attacks
    # These are dangerous when used with information disclosure functions
    blind_injection_functions = {
        "length",
        "char_length",
        "character_length",
        "octet_length",
        "bit_length",
        "ascii",
        "chr",
        "substring",
        "substr",
        "left",
        "right",
        "position",
        "strpos",
    }

    # Check for absolutely dangerous functions (always block)
    for func in parsed_ast.find_all(Func):
        if hasattr(func, "this") and func.this:
            func_name = str(func.this).lower()
            if func_name in absolutely_dangerous_functions:
                span_context.add_error_event(
                    f"Absolutely dangerous function detected: {func_name}"
                )
                return True

    # Context-aware detection for information disclosure and blind injection patterns
    return _detect_injection_patterns(
        parsed_ast, info_disclosure_functions, blind_injection_functions, span_context
    )


def _extract_all_functions(parsed_ast: Any) -> list:
    """Extract all functions from the parsed AST."""
    all_functions = []
    for func in parsed_ast.find_all(Func):
        if hasattr(func, "this") and func.this:
            func_name = str(func.this).lower()
            all_functions.append((func_name, func))
    return all_functions


def _check_info_disclosure_in_where(
    parsed_ast: Any, info_functions: set, span_context: Any
) -> bool:
    """Check for information disclosure functions in WHERE clauses."""
    if not isinstance(parsed_ast, Select) or not parsed_ast.args.get("where"):
        return False

    where_functions = []
    for func in parsed_ast.args["where"].find_all(Func):
        if hasattr(func, "this") and func.this:
            where_functions.append(str(func.this).lower())

    info_in_where = [f for f in where_functions if f in info_functions]
    if info_in_where:
        span_context.add_error_event(
            f"Information disclosure in WHERE clause: {info_in_where}"
        )
        return True
    return False


def _check_blind_injection_pattern(
    info_funcs_present: list, blind_funcs_present: list, span_context: Any
) -> bool:
    """Check for blind injection patterns."""
    if info_funcs_present and blind_funcs_present:
        span_context.add_error_event(
            f"Blind injection pattern detected: {info_funcs_present} + {blind_funcs_present}"
        )
        return True
    return False


def _check_multiple_info_disclosure(
    info_funcs_present: list, span_context: Any
) -> bool:
    """Check for multiple information disclosure functions."""
    if len(info_funcs_present) > 1:
        span_context.add_error_event(
            f"Multiple info disclosure functions: {info_funcs_present}"
        )
        return True
    return False


def _detect_injection_patterns(
    parsed_ast: Any, info_functions: set, blind_functions: set, span_context: Any
) -> bool:
    """
    Detect SQL injection patterns using context-aware analysis.

    This function looks for suspicious combinations and contexts rather than
    blocking individual functions, following PostgreSQL best practices.
    """
    # Get all functions in the query
    all_functions = _extract_all_functions(parsed_ast)

    # Pattern 1: Information disclosure functions in WHERE clauses (high risk)
    if _check_info_disclosure_in_where(parsed_ast, info_functions, span_context):
        return True

    # Extract function lists for subsequent checks
    info_funcs_present = [f for f, _ in all_functions if f in info_functions]
    blind_funcs_present = [f for f, _ in all_functions if f in blind_functions]

    # Pattern 2: Blind injection patterns (info + string functions together)
    if _check_blind_injection_pattern(
        info_funcs_present, blind_funcs_present, span_context
    ):
        return True

    # Pattern 3: Multiple information disclosure functions (enumeration attack)
    if _check_multiple_info_disclosure(info_funcs_present, span_context):
        return True

    # Pattern 4: Suspicious equality comparisons with blind functions using AST analysis
    if blind_funcs_present:
        if _detect_suspicious_function_comparisons(
            parsed_ast, blind_functions, span_context
        ):
            return True

    return False


def _detect_suspicious_function_comparisons(
    parsed_ast: Any, blind_functions: set, span_context: Any
) -> bool:
    """
    Detect suspicious function comparisons using SQLGlot's AST analysis.

    This function uses PostgreSQL's official AST parsing approach to identify
    patterns commonly used in SQL injection attacks, such as:
    - length(function()) = numeric_literal
    - ascii(function()) = numeric_literal
    - function() = literal AND '1' = '1'
    """
    from sqlglot.expressions import EQ, And, Literal

    # Find all equality expressions in the AST
    for eq_expr in parsed_ast.find_all(EQ):
        left_side = eq_expr.this
        right_side = eq_expr.expression

        # Check if left side contains blind injection functions
        left_functions = []
        for func in left_side.find_all(Func):
            if hasattr(func, "this") and func.this:
                func_name = str(func.this).lower()
                if func_name in blind_functions:
                    left_functions.append(func_name)

        # Pattern: blind_function() = numeric_literal
        if left_functions and isinstance(right_side, Literal):
            if right_side.is_number:
                span_context.add_error_event(
                    f"Suspicious function-to-number comparison: {left_functions} = {right_side}"
                )
                return True

    # Check for AND chains with suspicious patterns
    for and_expr in parsed_ast.find_all(And):
        # Look for patterns like: condition AND '1' = '1'
        if _is_suspicious_and_chain(and_expr, blind_functions):
            span_context.add_error_event(
                "Suspicious AND chain with blind function and tautology"
            )
            return True

    return False


def _collect_and_expression_parts(and_expr: Any) -> list:
    """Collect all parts of an AND expression chain recursively."""
    and_parts = []

    def collect_parts(expr: Any) -> None:
        if hasattr(expr, "this") and hasattr(expr, "expression"):
            left = expr.this
            right = expr.expression

            # If left is another AND, recursively collect its parts
            if str(type(left).__name__) == "And":
                collect_parts(left)
            else:
                and_parts.append(left)

            # If right is another AND, recursively collect its parts
            if str(type(right).__name__) == "And":
                collect_parts(right)
            else:
                and_parts.append(right)

    collect_parts(and_expr)
    return and_parts


def _has_blind_function_in_equality(part: Any, blind_functions: set) -> bool:
    """Check if an equality expression contains blind functions."""
    if str(type(part).__name__) != "EQ":
        return False

    left = part.this
    for func in left.find_all(Func):
        if hasattr(func, "this") and func.this:
            func_name = str(func.this).lower()
            if func_name in blind_functions:
                return True
    return False


def _is_tautology_equality(part: Any) -> bool:
    """Check if an expression is a tautology like '1' = '1'."""
    from sqlglot.expressions import Literal

    if str(type(part).__name__) != "EQ":
        return False

    left = part.this
    right = part.expression

    return (
        isinstance(left, Literal)
        and isinstance(right, Literal)
        and str(left) == str(right)
    )


def _is_suspicious_and_chain(and_expr: Any, blind_functions: set) -> bool:
    """
    Check if an AND expression contains suspicious patterns using AST analysis.

    Detects patterns like: blind_function() = value AND '1' = '1'
    """
    # Get all parts of the AND chain
    and_parts = _collect_and_expression_parts(and_expr)

    has_blind_function_comparison = False
    has_tautology = False

    for part in and_parts:
        if _has_blind_function_in_equality(part, blind_functions):
            has_blind_function_comparison = True

        if _is_tautology_equality(part):
            has_tautology = True

    return has_blind_function_comparison and has_tautology


def _validate_table_references(parsed_ast: Any) -> bool:
    """Validate table references in the DML AST."""
    tables = list(parsed_ast.find_all(Table))

    # For SELECT statements, tables are optional (e.g., SELECT 1, SELECT NOW())
    # For INSERT, UPDATE, DELETE statements, at least one table is required
    if isinstance(parsed_ast, Select):
        # SELECT can work without tables
        pass
    elif isinstance(parsed_ast, (Insert, Update, Delete)):
        # These statements require at least one table
        if not tables:
            return False

    # Validate all table names that are present
    for table in tables:
        if not _is_valid_postgresql_table_name(table):
            return False

    return True


def _is_valid_postgresql_identifier(identifier: str) -> bool:
    """
    Check if identifier is valid for PostgreSQL based on official documentation.

    PostgreSQL identifiers:
    - Must begin with a letter (a-z, A-Z), underscore (_), or non-ASCII letter
    - Subsequent characters can be letters, underscores, digits (0-9), or dollar signs ($)
    - Maximum length is 63 bytes (NAMEDATALEN-1)
    - Case-insensitive unless quoted
    - Quoted identifiers can contain any character except null byte
    """
    if not identifier:
        return False

    import string

    # Allow quoted identifiers (they can contain almost any character)
    if identifier.startswith('"') and identifier.endswith('"'):
        # Must have content between quotes and not exceed PostgreSQL limits
        content = identifier[1:-1]
        if not content or len(identifier) > 65:  # 63 + 2 quotes
            return False
        # Quoted identifiers cannot contain null bytes but allow everything else
        return "\x00" not in content

    # For unquoted identifiers, follow PostgreSQL standard
    if len(identifier) > 63:
        return False

    # Must start with letter or underscore (PostgreSQL standard)
    valid_start_chars = string.ascii_letters + "_"
    if identifier[0] not in valid_start_chars:
        return False

    # Subsequent characters can be letters, digits, underscores, or dollar signs
    valid_chars = string.ascii_letters + string.digits + "_$"
    return all(c in valid_chars for c in identifier)


def _is_valid_postgresql_table_name(table: Any) -> bool:
    """
    Check if table name is valid for PostgreSQL with practical considerations.

    While PostgreSQL standard requires identifiers to start with letters,
    many systems use numeric prefixes in table names. This function provides
    a balanced approach between security and practicality.
    """
    if not hasattr(table, "name") or not table.name:
        return False

    table_name = str(table.name)

    # For quoted table names, use strict PostgreSQL rules
    if table_name.startswith('"') and table_name.endswith('"'):
        return _is_valid_postgresql_identifier(table_name)

    # For unquoted table names, be more permissive to support existing systems
    # that may use table names like 'table001', 'user_2024', etc.
    return _is_valid_practical_table_identifier(table_name)


def _is_valid_practical_table_identifier(identifier: str) -> bool:
    """
    Validate table identifiers with practical considerations for existing systems.

    This allows table names that start with numbers (common in many systems)
    while maintaining security by validating character composition.
    """
    if not identifier or len(identifier) > 63:
        return False

    import string

    # Allow letters, digits, underscores for practical table naming
    valid_chars = string.ascii_letters + string.digits + "_"

    # First character can be letter, digit, or underscore (more permissive)
    # This allows common patterns like: table001, user_data, 2024_logs
    if identifier[0] not in valid_chars:
        return False

    # All characters must be valid
    if not all(c in valid_chars for c in identifier):
        return False

    # Reject identifiers that are only numbers (potential security risk)
    if identifier.isdigit():
        return False

    # Reject identifiers that start and end with underscores (suspicious pattern)
    if identifier.startswith("_") and identifier.endswith("_"):
        return False

    return True


def _reconstruct_safe_dml_statement(
    parsed_ast: Any, statement_type: str, span_context: Any
) -> str:
    """
    Reconstruct safe DML statement from AST using PostgreSQL dialect.

    Args:
        parsed_ast: Parsed SQLGlot AST
        statement_type: Type of DML statement
        span_context: Span context for logging

    Returns:
        str: Safe reconstructed SQL or empty string if reconstruction fails
    """
    try:
        # Use PostgreSQL dialect for reconstruction
        safe_sql = parsed_ast.sql(dialect="postgres", pretty=True)

        if not safe_sql or not safe_sql.strip():
            return ""

        # Ensure the SQL starts with expected statement type
        sql_upper = safe_sql.strip().upper()
        if not sql_upper.startswith(statement_type):
            return ""

        span_context.add_info_event(f"original_dml: {statement_type}")
        return safe_sql.strip()

    except Exception:
        return ""


def _validate_reconstructed_dml(
    safe_sql: str, statement_type: str, span_context: Any
) -> bool:
    """
    Validate that the reconstructed DML is safe and contains expected structure.

    Args:
        safe_sql: Reconstructed SQL string
        statement_type: Expected statement type
        span_context: Span context for logging

    Returns:
        bool: True if SQL is valid and safe, False otherwise
    """
    try:
        # Re-parse the reconstructed SQL to ensure it's valid
        reparsed = parse_one(safe_sql, dialect="postgres", error_level="raise")

        if not reparsed:
            return False

        # Verify the statement type matches
        restatement_type = _extract_dml_statement_info(reparsed)
        if restatement_type != statement_type:
            return False

        # Ensure no additional security issues were introduced
        if not _validate_dml_ast_security(reparsed, span_context):
            return False

        return True

    except Exception:
        return False


async def _validate_and_prepare_dml(
    db: Any, dml_input: Any, span_context: Any, m: Any
) -> Any:
    """Validate input and prepare DML execution."""
    app_id = dml_input.app_id
    uid = dml_input.uid
    database_id = dml_input.database_id
    dml = dml_input.dml
    env = dml_input.env
    space_id = dml_input.space_id

    # Validate SQL injection risks
    injection_error = _validate_sql_injection(dml, span_context)
    if injection_error:
        return None, injection_error

    need_check = {
        "app_id": app_id,
        "database_id": database_id,
        "uid": uid,
        "dml": dml,
        "env": env,
        "space_id": space_id,
    }
    span_context.add_info_events(need_check)
    span_context.add_info_event(f"app_id: {app_id}")
    span_context.add_info_event(f"database_id: {database_id}")
    span_context.add_info_event(f"uid: {uid}")

    if space_id:
        _, error_spaceid = await check_space_id_and_get_uid(
            db, database_id, space_id, span_context, m
        )
        if error_spaceid:
            return None, error_spaceid

    schema_list, error_resp = await check_database_exists_by_did(
        db, database_id, uid, span_context, m
    )
    if error_resp:
        return None, error_resp

    return (app_id, uid, database_id, dml, env, schema_list), None


async def _process_dml_statements(
    dmls: List[str], app_id: str, uid: str, env: str, span_context: Any
) -> Any:
    """Process and rewrite DML statements."""
    rewrite_dmls = []
    for statement in dmls:
        rewrite_dml, insert_ids = rewrite_dml_with_uid_and_limit(
            dml=statement,
            app_id=app_id,
            uid=uid,
            limit_num=100,
            env=env,
            span_context=span_context,
        )
        span_context.add_info_event(f"rewrite dml: {rewrite_dml}")
        rewrite_dmls.append(
            {
                "rewrite_dml": rewrite_dml,
                "insert_ids": insert_ids,
            }
        )
    return rewrite_dmls


@exec_dml_router.post("/exec_dml", response_class=JSONResponse)
async def exec_dml(
    dml_input: ExecDMLInput, db: AsyncSession = Depends(get_session)
) -> JSONResponse:
    """
    Execute DML statements on specified database.

    Args:
        dml_input: Input containing DML statements and metadata
        db: Database session

    Returns:
        JSONResponse: Result of DML execution
    """
    uid = dml_input.uid
    database_id = dml_input.database_id
    metric_service = get_otlp_metric_service()
    m = metric_service.get_meter()(func="exec_dml")
    span_service = get_otlp_span_service()
    span = span_service.get_span()(uid=uid)

    with span.start(
        func_name="exec_dml",
        add_source_function_name=True,
        attributes={"uid": uid, "database_id": database_id},
    ) as span_context:
        try:
            validated_data, error = await _validate_and_prepare_dml(
                db, dml_input, span_context, m
            )
            if error:
                return error  # type: ignore[no-any-return]

            app_id, uid, database_id, dml, env, schema_list = validated_data

            schema, error_search = await _set_search_path(
                db, schema_list, env, uid, span_context, m
            )
            if error_search:
                return error_search  # type: ignore[no-any-return]

            dmls, error_split = await _dml_split(dml, db, schema, uid, span_context, m)
            if error_split:
                return error_split  # type: ignore[no-any-return]

            rewrite_dmls = await _process_dml_statements(
                dmls, app_id, uid, env, span_context
            )

            final_exec_success_res, exec_time, error_exec = await _exec_dml_sql(
                db, rewrite_dmls, uid, span_context, m
            )
            if error_exec:
                return error_exec  # type: ignore[no-any-return]

            return format_response(  # type: ignore[no-any-return]
                CodeEnum.Successes.code,
                message=CodeEnum.Successes.msg,
                sid=span_context.sid,
                data={
                    "exec_success": final_exec_success_res,
                    "exec_failure": [],
                    "exec_time": exec_time,
                },
            )
        except CustomException as custom_error:
            span_context.record_exception(custom_error)
            m.in_error_count(custom_error.code, lables={"uid": uid}, span=span_context)
            return format_response(  # type: ignore[no-any-return]
                code=custom_error.code,
                message="Database execution failed",
                sid=span_context.sid,
            )
        except Exception as unexpected_error:  # pylint: disable=broad-except
            m.in_error_count(
                CodeEnum.DMLExecutionError.code, lables={"uid": uid}, span=span_context
            )
            span_context.record_exception(unexpected_error)
            return format_response(  # type: ignore[no-any-return]
                code=CodeEnum.DMLExecutionError.code,
                message="Database execution failed",
                sid=span_context.sid,
            )


async def _exec_dml_sql(
    db: Any, rewrite_dmls: List[Any], uid: str, span_context: Any, m: Any
) -> Any:
    """Execute rewritten DML SQL statements."""
    final_exec_success_res = []
    start_time = time.time()

    try:
        for dml_info in rewrite_dmls:
            rewrite_dml = dml_info["rewrite_dml"]
            insert_ids = dml_info["insert_ids"]

            result = await exec_sql_statement(db, rewrite_dml)
            try:
                exec_result = result.mappings().all()
                exec_result_dicts = [dict(row) for row in exec_result]
                exec_result_dicts = to_jsonable(exec_result_dicts)
            except Exception as mapping_error:
                span_context.add_info_event(f"{str(mapping_error)}")
                exec_result_dicts = []

            span_context.add_info_event(f"exec result: {exec_result_dicts}")

            if exec_result_dicts:
                final_exec_success_res.extend(exec_result_dicts)
            elif insert_ids:
                final_exec_success_res.extend([{"id": v} for v in insert_ids])

            await db.commit()

        m.in_success_count(lables={"uid": uid})
        exec_time = time.time() - start_time
        return final_exec_success_res, exec_time, None

    except Exception as exec_error:  # pylint: disable=broad-except
        span_context.record_exception(exec_error)
        await db.rollback()
        m.in_error_count(
            CodeEnum.DatabaseExecutionError.code, lables={"uid": uid}, span=span_context
        )
        return (
            None,
            None,
            format_response(
                code=CodeEnum.DatabaseExecutionError.code,
                message="Database execution failed",
                sid=span_context.sid,
            ),
        )


async def _set_search_path(
    db: Any, schema_list: List[Any], env: str, uid: str, span_context: Any, m: Any
) -> Any:
    """Set search path for database operations."""
    schema = next((one[0] for one in schema_list if env in one[0]), "")
    if not schema:
        span_context.add_error_event("Corresponding schema not found")
        m.in_error_count(
            CodeEnum.NoSchemaError.code, lables={"uid": uid}, span=span_context
        )
        return None, format_response(
            code=CodeEnum.NoSchemaError.code,
            message=f"Corresponding schema not found: {schema}",
            sid=span_context.sid,
        )

    span_context.add_info_event(f"schema: {schema}")
    try:
        await set_search_path_by_schema(db, schema)
        return schema, None
    except Exception as schema_error:  # pylint: disable=broad-except
        span_context.record_exception(schema_error)
        m.in_error_count(
            CodeEnum.NoSchemaError.code, lables={"uid": uid}, span=span_context
        )
        return None, format_response(
            code=CodeEnum.NoSchemaError.code,
            message=f"Invalid schema: {schema}",
            sid=span_context.sid,
        )


async def _dml_split(
    dml: str, db: Any, schema: str, uid: str, span_context: Any, m: Any
) -> Any:
    """Split and validate DML statements."""
    dml = dml.strip()
    dmls = sqlparse.split(dml)
    span_context.add_info_event(f"Split DML statements: {dmls}")

    for statement in dmls:
        try:
            parsed = parse_one(statement)
            tables = {table.name for table in parsed.find_all(exp.Table)}
        except Exception as parse_error:  # pylint: disable=broad-except
            span_context.record_exception(parse_error)
            m.in_error_count(
                CodeEnum.SQLParseError.code,
                lables={"uid": uid},
                span=span_context,
            )
            return None, format_response(
                code=CodeEnum.SQLParseError.code,
                message="SQL parsing failed",
                sid=span_context.sid,
            )

        result = await parse_and_exec_sql(
            db,
            "SELECT tablename FROM pg_tables WHERE schemaname = :schema",
            {"schema": schema},
        )
        valid_tables = {row[0] for row in result.fetchall()}
        not_found = tables - valid_tables

        if not_found:
            span_context.add_error_event(
                f"Table does not exist or no permission: {', '.join(not_found)}"
            )
            m.in_error_count(
                CodeEnum.NoAuthorityError.code,
                lables={"uid": uid},
                span=span_context,
            )
            return None, format_response(
                code=CodeEnum.NoAuthorityError.code,
                message=f"Table does not exist or no permission: "
                f"{', '.join(not_found)}",
                sid=span_context.sid,
            )

        allowed_sql = re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE)\s+", re.IGNORECASE)
        if not allowed_sql.match(statement):
            span_context.add_error_events({"invalid dml": statement})
            m.in_error_count(
                CodeEnum.DMLNotAllowed.code,
                lables={"uid": uid},
                span=span_context,
            )
            return None, format_response(
                code=CodeEnum.DMLNotAllowed.code,
                message="Unsupported SQL type, only "
                "SELECT/INSERT/UPDATE/DELETE allowed",
                sid=span_context.sid,
            )

    return dmls, None
