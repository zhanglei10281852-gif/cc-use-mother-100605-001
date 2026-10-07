"""状态机与权限常量。

版本生命周期：
    PENDING_REVIEW --复核通过--> APPROVED --被新版本取代--> SUPERSEDED
                                   APPROVED --人工撤回-----> WITHDRAWN
                                   APPROVED --有效期届满---> EXPIRED
    PENDING_REVIEW --复核驳回--> REJECTED
    PENDING_REVIEW --提交人撤回-> WITHDRAWN

只有 APPROVED 且被条目 current 指针引用的版本才出现在可检索目录。
"""

# 版本状态
VERSION_PENDING = "PENDING_REVIEW"
VERSION_APPROVED = "APPROVED"
VERSION_REJECTED = "REJECTED"
VERSION_SUPERSEDED = "SUPERSEDED"
VERSION_WITHDRAWN = "WITHDRAWN"
VERSION_EXPIRED = "EXPIRED"

VERSION_STATUSES = (
    VERSION_PENDING,
    VERSION_APPROVED,
    VERSION_REJECTED,
    VERSION_SUPERSEDED,
    VERSION_WITHDRAWN,
    VERSION_EXPIRED,
)

# 复核任务状态
TASK_PENDING = "PENDING"
TASK_APPROVED = "APPROVED"
TASK_REJECTED = "REJECTED"
TASK_CANCELLED = "CANCELLED"

# 条目状态
ENTRY_ACTIVE = "ACTIVE"
ENTRY_MERGED = "MERGED"  # 已并入其他条目，原始版本仍保留可查

# 合并建议状态
MERGE_OPEN = "OPEN"
MERGE_ACCEPTED = "ACCEPTED"
MERGE_DISMISSED = "DISMISSED"

# 角色
ROLE_ADMIN = "admin"            # 系统管理：用户、角色、术语
ROLE_ENGINEER = "engineer"      # 工程师：提交、修订、导入
ROLE_REVIEWER = "reviewer"      # 复核员：复核、撤回、判定过期、处理合并
ROLE_VIEWER = "viewer"          # 只读：检索目录、查看链路

ROLES = (ROLE_ADMIN, ROLE_ENGINEER, ROLE_REVIEWER, ROLE_VIEWER)

# 权限点 -> 允许的角色集合。角色存于数据库，每次请求实时校验，
# 因此授权/撤权立即生效，不依赖令牌内容。
PERMISSIONS = {
    "entry.submit": {ROLE_ENGINEER, ROLE_ADMIN},
    "entry.revise": {ROLE_ENGINEER, ROLE_ADMIN},
    "entry.import": {ROLE_ENGINEER, ROLE_ADMIN},
    "review.decide": {ROLE_REVIEWER, ROLE_ADMIN},
    "version.withdraw": {ROLE_REVIEWER, ROLE_ADMIN},
    "version.expire": {ROLE_REVIEWER, ROLE_ADMIN},
    "merge.decide": {ROLE_REVIEWER, ROLE_ADMIN},
    "user.manage": {ROLE_ADMIN},
    "term.manage": {ROLE_ADMIN},
    "catalog.read": {ROLE_ADMIN, ROLE_ENGINEER, ROLE_REVIEWER, ROLE_VIEWER},
}
