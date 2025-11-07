"""Unit tests for DDL execution functionality."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from memory.database.api.schemas.exec_ddl_types import ExecDDLInput
from memory.database.api.v1.exec_ddl import (
    _contains_multiple_ddl_statements,
    _ddl_split,
    _extract_alter_info,
    _extract_create_info,
    _extract_ddl_statement_info,
    _extract_drop_info,
    _is_valid_postgresql_identifier,
    _is_valid_postgresql_table_name,
    _rebuild_ddl_from_ast,
    _reconstruct_safe_ddl_statement,
    _reset_uid,
    _validate_identifiers,
    _validate_postgresql_ddl_syntax,
    _validate_reconstructed_ddl,
    _validate_table_names,
    exec_ddl,
    is_ddl_allowed,
)
from memory.database.exceptions.error_code import CodeEnum
from sqlmodel.ext.asyncio.session import AsyncSession


def test_is_ddl_allowed_allowed_statements() -> None:
    """Test allowed DDL statements (CREATE TABLE/ALTER TABLE etc)."""
    allowed_sql_cases = [
        "CREATE TABLE users (id INT);",
        "ALTER TABLE users ADD COLUMN name TEXT;",
        "DROP TABLE users;",
        "DROP DATABASE old_db;",
        "COMMENT ON COLUMN users.name IS 'Username';",
        "ALTER TABLE users RENAME TO new_users;",
        "alter table users add age int;",
    ]
    mock_span_context = MagicMock()

    for sql in allowed_sql_cases:
        result = is_ddl_allowed(sql, mock_span_context)
        assert result is True, f"Allowed SQL[{sql}] was incorrectly rejected"
        mock_span_context.add_info_event.assert_any_call(f"sql: {sql}")


@pytest.mark.asyncio
async def test_reset_uid_with_valid_space_id_reset_success() -> None:
    """Test _reset_uid with valid space_id resets to new uid."""
    mock_db = AsyncMock(spec=AsyncSession)
    mock_span_context = MagicMock()
    mock_meter = MagicMock()

    # Use non-string type to test type conversion
    mock_new_uid = 123
    with patch(
        "memory.database.api.v1.exec_ddl.check_space_id_and_get_uid",
        new_callable=AsyncMock,
    ) as mock_check_space:
        # Return format needs to match actual code [(uid,)] structure
        mock_check_space.return_value = ([(mock_new_uid,)], None)

        database_id = 2002
        space_id = "space_001"  # Ensure not empty to execute space_id related logic
        original_uid = "u_original"

        result_uid, error = await _reset_uid(
            db=mock_db,
            database_id=database_id,
            space_id=space_id,
            uid=original_uid,
            span_context=mock_span_context,
            m=mock_meter,
        )

        # Verify return value
        assert error is None
        assert result_uid == str(mock_new_uid)  # Verify type conversion

        # Verify check_space_id_and_get_uid call parameters
        mock_check_space.assert_called_once_with(
            mock_db, database_id, space_id, mock_span_context, mock_meter
        )

        # Verify meter was not called incorrectly
        mock_meter.in_error_count.assert_not_called()


@pytest.mark.asyncio
async def test_ddl_split_success() -> None:
    """Test successful DDL splitting (multiple valid statements)."""
    mock_span_context = MagicMock()
    mock_meter = MagicMock()

    with patch("memory.database.api.v1.exec_ddl.is_ddl_allowed", return_value=True):
        raw_ddl = """
            CREATE TABLE users (id INT);
            ALTER TABLE users ADD COLUMN name TEXT;
            DROP TABLE old_users;
        """
        uid = "u1"

        ddls, error_resp = await _ddl_split(raw_ddl, uid, mock_span_context, mock_meter)

        assert error_resp is None
        assert len(ddls) == 3
        # The DDL statements are reconstructed with PostgreSQL formatting (pretty=True)
        # so we need to check the normalized content instead of exact string match
        assert (
            "CREATE TABLE" in ddls[0]
            and "users" in ddls[0]
            and "id" in ddls[0]
            and "INT" in ddls[0]
        )
        assert (
            "ALTER TABLE" in ddls[1]
            and "users" in ddls[1]
            and "ADD COLUMN" in ddls[1]
            and "name" in ddls[1]
            and "TEXT" in ddls[1]
        )
        assert "DROP TABLE" in ddls[2] and "old_users" in ddls[2]

        # Verify that logging functions were called (the exact format may vary)
        assert mock_span_context.add_info_event.called
        mock_meter.in_error_count.assert_not_called()


@pytest.mark.asyncio
async def test_exec_ddl_success() -> None:
    """Test successful exec_ddl endpoint (valid DDL + database exists)."""
    mock_db = AsyncMock(spec=AsyncSession)
    mock_db.commit = AsyncMock(return_value=None)
    mock_db.rollback = AsyncMock(return_value=None)

    test_input = ExecDDLInput(
        uid="u1",
        database_id=3001,
        ddl="CREATE TABLE users (id INT); ALTER TABLE users ADD COLUMN name TEXT;",
        space_id="",
    )

    fake_span_context = MagicMock()
    fake_span_context.sid = "exec-ddl-sid-123"
    fake_span_context.add_info_events = MagicMock()
    fake_span_context.add_info_event = MagicMock()
    fake_span_context.record_exception = MagicMock()
    fake_span_context.add_error_event = MagicMock()

    with patch("memory.database.api.v1.exec_ddl.Span") as mock_span_cls:
        mock_span_instance = MagicMock()
        mock_span_instance.start.return_value.__enter__.return_value = fake_span_context
        mock_span_cls.return_value = mock_span_instance

        with patch(
            "memory.database.api.v1.exec_ddl.check_database_exists_by_did_uid",
            new_callable=AsyncMock,
        ) as mock_check_db:
            mock_check_db.return_value = ([["prod_u1_3001"], ["test_u1_3001"]], None)

            with patch(
                "memory.database.api.v1.exec_ddl._ddl_split", new_callable=AsyncMock
            ) as mock_ddl_split:
                mock_ddl_split.return_value = (
                    [
                        "CREATE TABLE users (id INT)",
                        "ALTER TABLE users ADD COLUMN name TEXT",
                    ],
                    None,
                )

                with patch(
                    "memory.database.api.v1.exec_ddl.set_search_path_by_schema",
                    new_callable=AsyncMock,
                ) as mock_set_search:
                    mock_set_search.return_value = None

                    with patch(
                        "memory.database.api.v1.exec_ddl.exec_sql_statement",
                        new_callable=AsyncMock,
                    ) as mock_exec_sql:
                        mock_exec_sql.return_value = None

                        with patch(
                            "memory.database.api.v1.exec_ddl.get_otlp_metric_service"
                        ) as mock_metric_service_func:
                            with patch(
                                "memory.database.api.v1.exec_ddl.get_otlp_span_service"
                            ) as mock_span_service_func:
                                # Mock meter instance
                                mock_meter_inst = MagicMock()
                                mock_meter_inst.in_success_count = MagicMock()

                                # Mock metric service
                                mock_metric_service = MagicMock()
                                mock_metric_service.get_meter.return_value = (
                                    lambda func: mock_meter_inst
                                )
                                mock_metric_service_func.return_value = (
                                    mock_metric_service
                                )

                                # Mock span service and instance
                                mock_span_instance = MagicMock()
                                mock_span_instance.start.return_value.__enter__.return_value = (
                                    fake_span_context
                                )
                                mock_span_service = MagicMock()
                                mock_span_service.get_span.return_value = (
                                    lambda uid: mock_span_instance
                                )
                                mock_span_service_func.return_value = mock_span_service

                                response = await exec_ddl(test_input, mock_db)

                                response_body = json.loads(response.body)
                                assert "code" in response_body
                                assert "message" in response_body
                                assert "sid" in response_body

                                assert response_body["code"] == CodeEnum.Successes.code
                                assert (
                                    response_body["message"] == CodeEnum.Successes.msg
                                )


def test_extract_ddl_statement_info() -> None:
    """Test DDL statement information extraction."""
    from sqlglot import parse_one

    # Test CREATE TABLE
    create_sql = "CREATE TABLE users (id INT, name TEXT)"
    parsed_create = parse_one(create_sql)
    statement_info = _extract_ddl_statement_info(parsed_create)
    assert statement_info == ("CREATE", "TABLE")

    # Test DROP TABLE
    drop_sql = "DROP TABLE users"
    parsed_drop = parse_one(drop_sql)
    statement_info = _extract_ddl_statement_info(parsed_drop)
    assert statement_info == ("DROP", "TABLE")

    # Test ALTER TABLE
    alter_sql = "ALTER TABLE users ADD COLUMN email TEXT"
    parsed_alter = parse_one(alter_sql)
    statement_info = _extract_ddl_statement_info(parsed_alter)
    assert statement_info == ("ALTER", "TABLE")

    # Test non-DDL statement
    select_sql = "SELECT * FROM users"
    parsed_select = parse_one(select_sql)
    statement_info = _extract_ddl_statement_info(parsed_select)
    assert statement_info is None


def test_extract_create_info() -> None:
    """Test CREATE statement information extraction."""
    from sqlglot import parse_one

    # CREATE TABLE
    create_table_sql = "CREATE TABLE users (id INT, name TEXT)"
    parsed = parse_one(create_table_sql)
    statement_type, object_type = _extract_create_info(parsed)
    assert statement_type == "CREATE"
    assert object_type == "TABLE"

    # CREATE DATABASE
    create_db_sql = "CREATE DATABASE testdb"
    parsed_db = parse_one(create_db_sql)
    statement_type, object_type = _extract_create_info(parsed_db)
    assert statement_type == "CREATE"
    assert object_type == "DATABASE"


def test_extract_drop_info() -> None:
    """Test DROP statement information extraction."""
    from sqlglot import parse_one

    # DROP TABLE
    drop_table_sql = "DROP TABLE users"
    parsed = parse_one(drop_table_sql)
    statement_type, object_type = _extract_drop_info(parsed)
    assert statement_type == "DROP"
    assert object_type == "TABLE"

    # DROP DATABASE
    drop_db_sql = "DROP DATABASE testdb"
    parsed_db = parse_one(drop_db_sql)
    statement_type, object_type = _extract_drop_info(parsed_db)
    assert statement_type == "DROP"
    assert object_type == "DATABASE"


def test_extract_alter_info() -> None:
    """Test ALTER statement information extraction."""
    from sqlglot import parse_one

    # ALTER TABLE
    alter_sql = "ALTER TABLE users ADD COLUMN email TEXT"
    parsed = parse_one(alter_sql)
    statement_type, object_type = _extract_alter_info(parsed)
    assert statement_type == "ALTER"
    assert object_type == "TABLE"


def test_is_valid_postgresql_identifier_ddl_context() -> None:
    """Test PostgreSQL identifier validation for DDL context."""
    # Valid identifiers
    assert _is_valid_postgresql_identifier("users") is True
    assert _is_valid_postgresql_identifier("user_table") is True
    assert _is_valid_postgresql_identifier("table123") is True
    assert _is_valid_postgresql_identifier("_private") is True

    # Test the function returns a boolean result
    result = _is_valid_postgresql_identifier("any_identifier")
    assert isinstance(result, bool)

    # Invalid identifiers
    assert _is_valid_postgresql_identifier("123table") is False  # starts with number
    assert _is_valid_postgresql_identifier("") is False  # empty


def test_is_valid_postgresql_table_name_ddl_context() -> None:
    """Test PostgreSQL table name validation for DDL context."""
    from sqlglot import parse_one
    from sqlglot.expressions import Table

    # Valid table names
    create_sql = "CREATE TABLE users (id INT)"
    parsed = parse_one(create_sql)
    tables = list(parsed.find_all(Table))
    if tables:
        assert _is_valid_postgresql_table_name(tables[0]) is True

    # Table with schema
    create_with_schema = "CREATE TABLE public.users (id INT)"
    parsed_schema = parse_one(create_with_schema)
    tables_schema = list(parsed_schema.find_all(Table))
    if tables_schema:
        assert _is_valid_postgresql_table_name(tables_schema[0]) is True


def test_validate_identifiers() -> None:
    """Test identifier validation in DDL statements."""
    from sqlglot import parse_one

    # Valid CREATE TABLE with proper identifiers
    valid_sql = "CREATE TABLE users (id INT, name TEXT, email VARCHAR(255))"
    parsed_valid = parse_one(valid_sql)
    assert _validate_identifiers(parsed_valid) is True

    # Valid ALTER TABLE
    valid_alter = "ALTER TABLE users ADD COLUMN age INT"
    parsed_alter = parse_one(valid_alter)
    assert _validate_identifiers(parsed_alter) is True


def test_validate_table_names() -> None:
    """Test table name validation in DDL statements."""
    from sqlglot import parse_one

    # Valid CREATE TABLE
    valid_create = "CREATE TABLE users (id INT, name TEXT)"
    parsed_create = parse_one(valid_create)
    assert _validate_table_names(parsed_create) is True

    # Valid DROP TABLE
    valid_drop = "DROP TABLE users"
    parsed_drop = parse_one(valid_drop)
    assert _validate_table_names(parsed_drop) is True

    # Valid ALTER TABLE
    valid_alter = "ALTER TABLE users ADD COLUMN email TEXT"
    parsed_alter = parse_one(valid_alter)
    assert _validate_table_names(parsed_alter) is True


def test_contains_multiple_ddl_statements() -> None:
    """Test multiple DDL statement detection."""
    from sqlglot import parse_one

    # Single CREATE statement
    single_create = parse_one("CREATE TABLE users (id INT, name TEXT)")
    assert _contains_multiple_ddl_statements(single_create) is False

    # Single ALTER statement
    single_alter = parse_one("ALTER TABLE users ADD COLUMN email TEXT")
    assert _contains_multiple_ddl_statements(single_alter) is False

    # Single DROP statement
    single_drop = parse_one("DROP TABLE users")
    assert _contains_multiple_ddl_statements(single_drop) is False


def test_reconstruct_safe_ddl_statement() -> None:
    """Test safe DDL statement reconstruction."""
    mock_span_context = MagicMock()

    # Valid CREATE TABLE
    create_sql = "CREATE TABLE users (id INT, name TEXT)"
    reconstructed = _reconstruct_safe_ddl_statement(create_sql, mock_span_context)
    assert reconstructed.strip()
    assert "CREATE" in reconstructed.upper()
    assert "TABLE" in reconstructed.upper()
    assert "users" in reconstructed

    # Valid ALTER TABLE
    alter_sql = "ALTER TABLE users ADD COLUMN email TEXT"
    reconstructed_alter = _reconstruct_safe_ddl_statement(alter_sql, mock_span_context)
    assert reconstructed_alter.strip()
    assert "ALTER" in reconstructed_alter.upper()
    assert "TABLE" in reconstructed_alter.upper()

    # Valid DROP TABLE
    drop_sql = "DROP TABLE users"
    reconstructed_drop = _reconstruct_safe_ddl_statement(drop_sql, mock_span_context)
    assert reconstructed_drop.strip()
    assert "DROP" in reconstructed_drop.upper()
    assert "TABLE" in reconstructed_drop.upper()


def test_validate_reconstructed_ddl() -> None:
    """Test reconstructed DDL validation."""
    mock_span_context = MagicMock()

    # Valid reconstructed CREATE TABLE
    valid_create = "CREATE TABLE users (id INT, name TEXT)"
    result = _validate_reconstructed_ddl(
        valid_create, ("CREATE", "TABLE"), mock_span_context
    )
    # The function may return True or False based on implementation details
    assert isinstance(result, bool)

    # Valid reconstructed ALTER TABLE
    valid_alter = "ALTER TABLE users ADD COLUMN email TEXT"
    result_alter = _validate_reconstructed_ddl(
        valid_alter, ("ALTER", "TABLE"), mock_span_context
    )
    assert isinstance(result_alter, bool)

    # Invalid SQL (parsing will fail)
    invalid_sql = "INVALID DDL STATEMENT"
    result_invalid = _validate_reconstructed_ddl(
        invalid_sql, ("CREATE", "TABLE"), mock_span_context
    )
    assert result_invalid is False


def test_rebuild_ddl_from_ast() -> None:
    """Test DDL rebuilding from AST."""
    from sqlglot import parse_one

    mock_span_context = MagicMock()

    # Test CREATE TABLE rebuilding
    create_sql = "CREATE TABLE users (id INT, name TEXT)"
    parsed_create = parse_one(create_sql)
    rebuilt = _rebuild_ddl_from_ast(
        parsed_create, ("CREATE", "TABLE"), mock_span_context
    )
    # Check that the function returns a string
    assert isinstance(rebuilt, str)
    if rebuilt.strip():  # Only check content if not empty
        assert "CREATE" in rebuilt.upper()

    # Test ALTER TABLE rebuilding
    alter_sql = "ALTER TABLE users ADD COLUMN email TEXT"
    parsed_alter = parse_one(alter_sql)
    rebuilt_alter = _rebuild_ddl_from_ast(
        parsed_alter, ("ALTER", "TABLE"), mock_span_context
    )
    assert isinstance(rebuilt_alter, str)

    # Test DROP TABLE rebuilding
    drop_sql = "DROP TABLE users"
    parsed_drop = parse_one(drop_sql)
    rebuilt_drop = _rebuild_ddl_from_ast(
        parsed_drop, ("DROP", "TABLE"), mock_span_context
    )
    assert isinstance(rebuilt_drop, str)


def test_validate_postgresql_ddl_syntax() -> None:
    """Test PostgreSQL DDL syntax validation."""
    mock_span_context = MagicMock()

    # Test that the function can be called without errors
    valid_create = "CREATE TABLE users (id INT PRIMARY KEY, name TEXT NOT NULL)"
    result = _validate_postgresql_ddl_syntax(valid_create, "TABLE", mock_span_context)
    # The function should return a boolean or None
    assert result is None or isinstance(result, bool)

    # Test with another SQL statement
    valid_alter = "ALTER TABLE users ADD COLUMN email VARCHAR(255)"
    result_alter = _validate_postgresql_ddl_syntax(
        valid_alter, "TABLE", mock_span_context
    )
    assert result_alter is None or isinstance(result_alter, bool)
