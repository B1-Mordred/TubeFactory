from editorial_core.authorization import Permission, Role, role_allows


def test_only_admin_manages_users() -> None:
    assert role_allows(Role.ADMIN, Permission.MANAGE_USERS)
    for role in (Role.OPERATOR, Role.EDITOR, Role.REVIEWER, Role.VIEWER):
        assert not role_allows(role, Permission.MANAGE_USERS)


def test_publication_authority_is_separate_from_private_upload() -> None:
    assert role_allows(Role.OPERATOR, Permission.UPLOAD_PRIVATE)
    assert not role_allows(Role.OPERATOR, Permission.AUTHORIZE_PUBLICATION)
    assert role_allows(Role.REVIEWER, Permission.AUTHORIZE_PUBLICATION)
    assert not role_allows(Role.REVIEWER, Permission.UPLOAD_PRIVATE)
