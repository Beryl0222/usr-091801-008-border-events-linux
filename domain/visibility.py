"""可见范围与角色。

三类资料的可见边界：

* ``PUBLIC``（公开信息）：任何游客可读，是缓存与搜索的主要对象；
* ``ORGANIZER_CONTACTS``（承办方联系人）：仅该活动承办方账号与运营中心可见；
* ``INTERNAL_DISPATCH``（联动部门处置记录）：仅运营中心与联动部门可读，
  对游客与承办方不可见。
"""

from __future__ import annotations

from enum import IntEnum


class Scope(IntEnum):
    PUBLIC = 0
    ORGANIZER_CONTACTS = 1
    INTERNAL_DISPATCH = 2


# 角色 -> 能读的最高可见级别。
_ROLE_MAX_SCOPE = {
    "visitor": Scope.PUBLIC,
    "organizer": Scope.ORGANIZER_CONTACTS,
    "reviewer": Scope.INTERNAL_DISPATCH,
    "ops": Scope.INTERNAL_DISPATCH,
}

# 角色 -> 该角色负责的活动（用于联系人级别的行级隔离）。
# 实际账号体系外由请求上下文提供，这里定义常量供测试与种子数据使用。


def can_read(role: str, scope: Scope, organizer_id: str | None = None,
             record_organizer_id: str | None = None) -> bool:
    """判定访问。``record_organizer`` 字段做行级隔离：联系人只能被本活动承办方看到。"""
    max_scope = _ROLE_MAX_SCOPE.get(role, -1)
    if scope > max_scope:
        return False
    if scope == Scope.ORGANIZER_CONTACTS and role == "organizer":
        return record_organizer_id == organizer_id
    return True


def public_view(record: dict, role: str, organizer_id: str | None = None) -> dict | None:
    """按角色裁剪单条记录字段。不可见时返回 ``None``。"""
    try:
        dict_scope = Scope(record.get("scope", Scope.PUBLIC))
    except ValueError:
        dict_scope = Scope.PUBLIC
    if not can_read(role, dict_scope, organizer_id, record.get("organizer_id")):
        return None
    if role == "visitor":
        return {k: v for k, v in record.items() if k not in ("scope", "organizer_id")}
    return dict(record)
