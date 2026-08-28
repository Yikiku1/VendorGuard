from sqlalchemy import CheckConstraint, Enum as SqlEnum
from vendorguard.security import User, UserRole


def test_user_roles_match_mvp_contract() -> None:
    assert {role.value for role in UserRole} == {
        "procurement_specialist",
        "procurement_manager",
        "quality_manager",
        "admin",
    }


def test_user_model_has_mininum_authentication_fields() -> None:
    table = User.__table__
    
    assert table.name == "users"
    assert set(table.c.keys()) == {
        "id",
        "username",
        "password_hash",
        "role",
        "is_active",
        "created_at",
    }
    assert tuple(column.name for column in table.primary_key.columns) == ("id",)
    assert table.c.username.unique is True
    assert table.c.username.nullable is False
    assert table.c.password_hash.nullable is False
    assert "password" not in table.c


def test_user_model_uses_stable_roles_and_database_defaults() -> None:
    table = User.__table__
    role_type = table.c.role.type
    
    assert isinstance(role_type, SqlEnum)
    assert role_type.enums == [role.value for role in UserRole]
    assert role_type.native_enum is False
    assert role_type.create_constraint is False

    role_constraint = next(
        constraint
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
        and constraint.name == "user_role"
    )
    constraint_sql = str(role_constraint.sqltext)
    assert all(role.value in constraint_sql for role in UserRole)
    assert table.c.is_active.server_default is not None
    assert table.c.created_at.server_default is not None