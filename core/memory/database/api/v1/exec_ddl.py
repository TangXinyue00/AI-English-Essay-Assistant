"""API endpoints for executing DDL (Data Definition Language) statements."""

import re
from typing import Any, List, Union

import asyncpg
import asyncpg.exceptions
import sqlglot
from common.otlp.trace.span import Span
from common.service import get_otlp_metric_service, get_otlp_span_service
from fastapi import APIRouter, Depends
from memory.database.api.schemas.exec_ddl_types import ExecDDLInput
from memory.database.api.v1.common import (
    check_database_exists_by_did_uid,
    check_space_id_and_get_uid,
)
from memory.database.domain.entity.general import exec_sql_statement
from memory.database.domain.entity.schema import set_search_path_by_schema
from memory.database.domain.entity.views.http_resp import format_response
from memory.database.exceptions.error_code import CodeEnum
from memory.database.repository.middleware.getters import get_session
from memory.database.utils.exception_util import unwrap_cause
from sqlglot.errors import ParseError
from sqlglot.expressions import Alter, Command, Create, Drop
from sqlmodel.ext.asyncio.session import AsyncSession
from starlette.responses import JSONResponse

exec_ddl_router = APIRouter(tags=["EXEC_DDL"])

ALLOWED_DDL_STATEMENTS = {
    "CREATE TABLE",
    "ALTER TABLE",
    "DROP TABLE",
    "DROP DATABASE",
    "COMMENT",
    "RENAME",
}


def is_ddl_allowed(sql: str, span_context: Span) -> bool:
    """
    Check if the DDL statement is allowed.

    Args:
        sql: SQL statement to check
        span_context: Span context for tracing

    Returns:
        bool: True if DDL is allowed, False otherwise
    """
    try:
        span_context.add_info_event(f"sql: {sql}")
        parsed = sqlglot.parse_one(sql, error_level="raise")
        statement_type = parsed.key.upper() if parsed.key else ""

        if isinstance(parsed, Drop):
            object_type = parsed.args.get("kind", "").upper()
            full_type = f"DROP {object_type}"
        elif isinstance(parsed, Create):
            object_type = parsed.args.get("kind", "").upper()
            full_type = f"CREATE {object_type}"
        elif isinstance(parsed, Alter):
            object_type = parsed.args.get("kind", "").upper()
            full_type = f"ALTER {object_type}"
        elif isinstance(parsed, Command):
            match = re.search(r"\bALTER\s+TABLE\b", sql, re.IGNORECASE)
            if match:
                full_type = match.group(0).upper()
            else:
                full_type = statement_type
        else:
            full_type = statement_type

        return full_type in ALLOWED_DDL_STATEMENTS

    except ParseError as parse_error:
        span_context.record_exception(parse_error)
        return False


def _reconstruct_safe_ddl_statement(sql: str, span_context: Span) -> str:
    """
    Reconstruct a safe DDL statement using PostgreSQL official parsing to prevent SQL injection.
    This function should only be called after is_ddl_allowed() returns True.

    Args:
        sql: Original SQL statement (already validated by is_ddl_allowed)
        span_context: Span context for tracing

    Returns:
        str: Safe reconstructed SQL statement or empty string if reconstruction fails
    """
    try:
        span_context.add_info_event(f"reconstructing_sql: {sql}")

        # Parse using PostgreSQL dialect for accurate parsing
        parsed = sqlglot.parse_one(sql, dialect="postgres", error_level="raise")

        if not parsed:
            span_context.add_error_event("Failed to parse SQL for reconstruction")
            return ""

        # Extract statement information using AST structure
        statement_info = _extract_ddl_statement_info(parsed)
        if not statement_info:
            span_context.add_error_event("Unknown statement type during reconstruction")
            return ""

        statement_type, object_type = statement_info

        # Reconstruct safe SQL using AST components
        safe_sql = _rebuild_ddl_from_ast(parsed, statement_type, object_type)

        if not safe_sql:
            span_context.add_error_event("Failed to reconstruct safe SQL")
            return ""

        # Validate the reconstructed SQL structure
        if not _validate_reconstructed_ddl(safe_sql, statement_type, object_type):
            span_context.add_error_event("Reconstructed SQL failed validation")
            return ""

        span_context.add_info_event(f"safe_reconstructed_sql: {safe_sql}")
        return safe_sql

    except Exception as error:
        span_context.record_exception(error)
        span_context.add_error_event(f"DDL reconstruction failed: {str(error)}")
        return ""


def _extract_drop_info(parsed_ast: Any) -> tuple[str, str]:
    """Extract info from DROP statement."""
    from sqlglot.expressions import Table

    if hasattr(parsed_ast, "kind") and parsed_ast.kind:
        return "DROP", parsed_ast.kind.upper()
    if parsed_ast.find(Table):
        return "DROP", "TABLE"
    return "DROP", "DATABASE"


def _extract_create_info(parsed_ast: Any) -> tuple[str, str]:
    """Extract info from CREATE statement."""
    from sqlglot.expressions import Table

    if hasattr(parsed_ast, "kind") and parsed_ast.kind:
        return "CREATE", parsed_ast.kind.upper()
    if parsed_ast.find(Table):
        return "CREATE", "TABLE"
    return "CREATE", ""


def _extract_alter_info(parsed_ast: Any) -> tuple[str, str]:
    """Extract info from ALTER statement."""
    from sqlglot.expressions import Table

    if hasattr(parsed_ast, "kind") and parsed_ast.kind:
        return "ALTER", parsed_ast.kind.upper()
    if parsed_ast.find(Table):
        return "ALTER", "TABLE"
    return "ALTER", ""


def _extract_ddl_statement_info(parsed_ast: Any) -> Union[tuple[str, str], None]:
    """
    Extract statement type and object type from parsed AST using official SQLGlot methods.

    Args:
        parsed_ast: Parsed SQLGlot AST

    Returns:
        tuple: (statement_type, object_type) or None if extraction fails
    """
    from sqlglot.expressions import Comment

    if isinstance(parsed_ast, Drop):
        return _extract_drop_info(parsed_ast)
    elif isinstance(parsed_ast, Create):
        return _extract_create_info(parsed_ast)
    elif isinstance(parsed_ast, Alter):
        return _extract_alter_info(parsed_ast)
    elif isinstance(parsed_ast, Comment):
        return "COMMENT", ""

    return None


def _rebuild_ddl_from_ast(
    parsed_ast: Any, statement_type: str, object_type: str
) -> str:
    """
    Rebuild DDL statement from AST components using PostgreSQL dialect.

    Args:
        parsed_ast: Parsed SQLGlot AST
        statement_type: Type of statement (CREATE, DROP, ALTER, etc.)
        object_type: Type of object (TABLE, DATABASE, etc.)

    Returns:
        str: Safe reconstructed SQL or empty string if reconstruction fails
    """
    try:
        # Use PostgreSQL dialect for reconstruction to ensure compatibility
        safe_sql = parsed_ast.sql(dialect="postgres", pretty=True)

        # Ensure the SQL is properly formatted and contains only expected elements
        if not safe_sql or not safe_sql.strip():
            return ""

        # Basic sanity check: ensure the reconstructed SQL starts with expected statement type
        sql_upper = safe_sql.strip().upper()
        if not sql_upper.startswith(statement_type):
            return ""

        # Additional validation for object type if specified
        if object_type and object_type not in sql_upper:
            return ""

        return safe_sql.strip()

    except Exception:
        return ""


def _validate_reconstructed_ddl(
    safe_sql: str, statement_type: str, object_type: str
) -> bool:
    """
    Validate that the reconstructed DDL is safe and contains expected structure.

    Args:
        safe_sql: Reconstructed SQL string
        statement_type: Expected statement type
        object_type: Expected object type

    Returns:
        bool: True if SQL is valid and safe, False otherwise
    """
    try:
        # Re-parse the reconstructed SQL to ensure it's valid
        reparsed = sqlglot.parse_one(safe_sql, dialect="postgres", error_level="raise")

        if not reparsed:
            return False

        # Verify the structure matches expectations
        reextracted = _extract_ddl_statement_info(reparsed)
        if not reextracted:
            return False

        restatement_type, reobject_type = reextracted

        # Ensure the statement type and object type match
        if restatement_type != statement_type:
            return False

        if object_type and reobject_type != object_type:
            return False

        # Additional PostgreSQL-specific validations
        return _validate_postgresql_ddl_syntax(reparsed, statement_type, object_type)

    except Exception:
        return False


def _validate_identifiers(parsed_ast: Any) -> bool:
    """Validate all identifiers in the AST."""
    from sqlglot.expressions import Identifier

    for identifier in parsed_ast.find_all(Identifier):
        if identifier.this and not _is_valid_postgresql_identifier(identifier.this):
            return False
    return True


def _validate_table_names(parsed_ast: Any) -> bool:
    """Validate table names in the AST."""
    from sqlglot.expressions import Table

    tables = list(parsed_ast.find_all(Table))
    if not tables:
        return False
    for table in tables:
        if not _is_valid_postgresql_table_name(table):
            return False
    return True


def _validate_postgresql_ddl_syntax(
    parsed_ast: Any, statement_type: str, object_type: str
) -> bool:
    """
    Perform PostgreSQL-specific syntax validation on the parsed DDL AST.

    Args:
        parsed_ast: Parsed SQLGlot AST
        statement_type: Statement type
        object_type: Object type

    Returns:
        bool: True if syntax is valid for PostgreSQL
    """
    try:
        if not _validate_identifiers(parsed_ast):
            return False

        if object_type == "TABLE" and not _validate_table_names(parsed_ast):
            return False

        if _contains_multiple_ddl_statements(parsed_ast):
            return False

        return True

    except Exception:
        return False


def _is_valid_postgresql_identifier(identifier: str) -> bool:
    """Check if identifier is valid for PostgreSQL."""
    if not identifier:
        return False
    # PostgreSQL identifiers can contain letters, digits, underscores, and dollar signs
    # and must start with a letter or underscore
    import string

    valid_chars = string.ascii_letters + string.digits + "_$"
    return identifier[0] in string.ascii_letters + "_" and all(
        c in valid_chars for c in identifier
    )


def _is_valid_postgresql_table_name(table: Any) -> bool:
    """Check if table name is valid for PostgreSQL."""
    if not hasattr(table, "name") or not table.name:
        return False
    return _is_valid_postgresql_identifier(str(table.name))


def _contains_multiple_ddl_statements(parsed_ast: Any) -> bool:
    """Check if AST contains multiple DDL statements (potential injection)."""
    from sqlglot.expressions import Delete, Insert, Select, Update

    # Count statement nodes - should only have one top-level statement
    statement_types = (Select, Insert, Update, Delete, Drop, Create, Alter)
    statement_count = sum(1 for _ in parsed_ast.find_all(*statement_types))

    return statement_count > 1


async def _execute_ddl_statements(
    db: Any, schema_list: List[Any], ddls: List[str], span_context: Any
) -> None:
    """Execute DDL statements across all schemas."""
    for schema in schema_list:
        span_context.add_info_event(f"set search path: SET search_path = '{schema[0]}'")
        await set_search_path_by_schema(db, schema[0])
        for statement in ddls:
            try:
                await exec_sql_statement(db, statement)
                span_context.add_info_event(f"exec ddl: {statement}")
            except Exception as exec_error:
                span_context.add_error_event(f"Unsupported syntax, {statement}")
                raise exec_error


async def _handle_ddl_error(
    ddl_error: Exception, db: Any, m: Any, uid: str, span_context: Any
) -> Any:
    """Handle DDL execution errors."""
    span_context.record_exception(ddl_error)
    await db.rollback()
    m.in_error_count(
        CodeEnum.DDLExecutionError.code, lables={"uid": uid}, span=span_context
    )
    root_exc = unwrap_cause(ddl_error)
    if isinstance(root_exc, asyncpg.exceptions.DatatypeMismatchError):
        return format_response(  # type: ignore[no-any-return]
            code=CodeEnum.DDLExecutionError.code,
            message=f"Data type mismatch error, reason: {str(root_exc)}",
            sid=span_context.sid,
        )
    return format_response(
        code=CodeEnum.DDLExecutionError.code,
        message=f"DDL statement execution failed, reason: {str(root_exc)}",
        sid=span_context.sid,
    )


@exec_ddl_router.post("/exec_ddl", response_class=JSONResponse)
async def exec_ddl(
    ddl_input: ExecDDLInput, db: AsyncSession = Depends(get_session)
) -> JSONResponse:
    """
    Execute DDL statements on specified database.

    Args:
        ddl_input: Input containing DDL statements and metadata
        db: Database session

    Returns:
        JSONResponse: Result of DDL execution
    """
    uid = ddl_input.uid
    database_id = ddl_input.database_id
    metric_service = get_otlp_metric_service()
    m = metric_service.get_meter()(func="exec_ddl")
    span_service = get_otlp_span_service()
    span = span_service.get_span()(uid=uid)

    with span.start(
        func_name="exec_ddl",
        add_source_function_name=True,
        attributes={"uid": uid, "database_id": database_id},
    ) as span_context:
        ddl = ddl_input.ddl
        space_id = ddl_input.space_id
        need_check = {
            "database_id": database_id,
            "uid": uid,
            "ddl": ddl,
            "space_id": space_id,
        }
        span_context.add_info_events(need_check)
        span_context.add_info_event(f"database_id: {database_id}")
        span_context.add_info_event(f"uid: {uid}")

        uid, error_reset = await _reset_uid(
            db, database_id, space_id, uid, span_context, m
        )
        if error_reset:
            return error_reset  # type: ignore[no-any-return]

        schema_list, error_resp = await check_database_exists_by_did_uid(
            db, database_id, uid, span_context, m
        )
        if error_resp:
            return error_resp  # type: ignore[no-any-return]

        ddls, error_split = await _ddl_split(ddl, uid, span_context, m)
        if error_split:
            return error_split  # type: ignore[no-any-return]

        try:
            await _execute_ddl_statements(db, schema_list, ddls, span_context)  # type: ignore[arg-type]
            await db.commit()
            m.in_success_count(lables={"uid": uid})
            return format_response(  # type: ignore[no-any-return]
                CodeEnum.Successes.code,
                message=CodeEnum.Successes.msg,
                sid=span_context.sid,
            )
        except Exception as ddl_error:  # pylint: disable=broad-except
            return await _handle_ddl_error(ddl_error, db, m, uid, span_context)  # type: ignore[no-any-return]


async def _reset_uid(
    db: Any, database_id: int, space_id: str, uid: str, span_context: Any, m: Any
) -> Any:
    """Reset UID based on space ID if provided."""
    new_uid = uid

    if space_id:
        create_uid_res, error = await check_space_id_and_get_uid(
            db, database_id, space_id, span_context, m
        )
        if error:
            return None, error

        cur = create_uid_res[0][0]
        if not isinstance(cur, str):
            cur = str(cur)
        new_uid = cur

    return new_uid, None


async def _ddl_split(ddl: str, uid: str, span_context: Any, m: Any) -> Any:
    """Split DDL statements, validate them, and reconstruct safe versions."""
    ddl = ddl.strip()
    original_ddls = [
        statement.strip() for statement in ddl.split(";") if statement.strip()
    ]
    span_context.add_info_event(f"Split DDL statements: {original_ddls}")

    safe_ddls = []
    for statement in original_ddls:
        # First, use the original validation logic
        if not is_ddl_allowed(statement, span_context):
            span_context.add_error_event(f"invalid ddl: {statement}")
            m.in_error_count(
                CodeEnum.DDLNotAllowed.code, lables={"uid": uid}, span=span_context
            )
            return None, format_response(
                CodeEnum.DDLNotAllowed.code,
                message=f"DDL statement is invalid, illegal statement: {statement}",
                sid=span_context.sid,
            )

        # After validation passes, reconstruct safe DDL statement
        safe_statement = _reconstruct_safe_ddl_statement(statement, span_context)

        # If reconstruction fails, reject the statement for security
        if not safe_statement:
            span_context.add_error_event(
                f"DDL reconstruction failed for security: {statement}"
            )
            m.in_error_count(
                CodeEnum.DDLNotAllowed.code, lables={"uid": uid}, span=span_context
            )
            return None, format_response(
                CodeEnum.DDLNotAllowed.code,
                message=f"DDL statement failed security reconstruction: {statement}",
                sid=span_context.sid,
            )

        # Use the safe reconstructed statement
        safe_ddls.append(safe_statement)

    span_context.add_info_event(f"Safe reconstructed DDL statements: {safe_ddls}")
    return safe_ddls, None
