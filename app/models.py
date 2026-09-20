"""
数据库模型定义
定义所有数据库表的 SQLAlchemy 模型
"""
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, ForeignKey, Index
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base
from app.utils.time_utils import get_now


class Team(Base):
    """Team 信息表"""
    __tablename__ = "teams"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(255), nullable=False, comment="Team 管理员邮箱")
    access_token_encrypted = Column(Text, nullable=False, comment="加密存储的 AT")
    id_token_encrypted = Column(Text, comment="加密存储的 ID Token")
    refresh_token_encrypted = Column(Text, comment="加密存储的 RT")
    session_token_encrypted = Column(Text, comment="加密存储的 Session Token")
    client_id = Column(String(100), comment="OAuth Client ID")
    encryption_key_id = Column(String(50), comment="加密密钥 ID")
    account_id = Column(String(100), comment="当前使用的 account-id")
    team_name = Column(String(255), comment="Team 名称")
    plan_type = Column(String(50), comment="计划类型")
    subscription_plan = Column(String(100), comment="订阅计划")
    expires_at = Column(DateTime, comment="订阅到期时间")
    current_members = Column(Integer, default=0, comment="当前成员数")
    max_members = Column(Integer, default=6, comment="最大成员数")
    joined_members = Column(Integer, nullable=True, comment="上游已加入成员数，不含邀请")
    total_seats = Column(Integer, nullable=True, comment="上游已购标准与高级席位合计")
    status = Column(String(20), default="active", comment="状态: active/full/expired/error/banned")
    account_role = Column(String(50), comment="账号角色: account-owner/standard-user 等")
    device_code_auth_enabled = Column(Boolean, default=False, comment="是否开启设备代码身份验证")
    warranty_seat_enabled = Column(Boolean, default=False, comment="是否作为质保兑换码分流目标 Team")
    member_auto_kick_hours = Column(Integer, default=2, nullable=False, comment="成员加入后自动踢出小时数")
    pending_replacements = Column(Integer, default=0, nullable=False, comment="自动踢人后待补账号数")
    error_count = Column(Integer, default=0, comment="连续报错次数")
    last_sync = Column(DateTime, comment="最后同步时间")
    created_at = Column(DateTime, default=get_now, comment="创建时间")
    pool_type = Column(String(20), default="normal", comment="池类型: normal/welfare")

    # 关系
    team_accounts = relationship("TeamAccount", back_populates="team", cascade="all, delete-orphan")
    redemption_records = relationship("RedemptionRecord", back_populates="team", cascade="all, delete-orphan")
    email_mappings = relationship("TeamEmailMapping", back_populates="team", cascade="all, delete-orphan")
    member_authorizations = relationship("MemberAuthorization", back_populates="team", cascade="all, delete-orphan")

    # 索引
    __table_args__ = (
        Index("idx_status", "status"),
    )


class MemberAuthorization(Base):
    """每个 Team 成员独立的 OAuth 凭证与一次性授权会话。"""
    __tablename__ = "member_authorizations"

    id = Column(Integer, primary_key=True)
    team_id = Column(Integer, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    account_id = Column(String(100), nullable=False)
    email = Column(String(255), nullable=False)
    credentials_encrypted = Column(Text)
    authorized_at = Column(DateTime)
    export_json_encrypted = Column(Text, comment="授权后生成的加密 sub2api JSON 快照")
    export_json_updated_at = Column(DateTime, comment="JSON 快照最近更新时间")
    sub2api_account_id = Column(Integer, comment="最近一次导入的 sub2api 账户 ID")
    sub2api_exported_at = Column(DateTime, comment="最近一次成功导入 sub2api 的时间")
    oauth_state = Column(String(100))
    verifier_encrypted = Column(Text)
    oauth_expires_at = Column(DateTime)
    team = relationship("Team", back_populates="member_authorizations")
    __table_args__ = (Index("idx_member_authorization", "team_id", "email", unique=True),)


class TeamSeatHold(Base):
    """尚未在上游列表中确认的席位操作，重启后仍保留预留。"""
    __tablename__ = "team_seat_holds"

    id = Column(Integer, primary_key=True)
    account_id = Column(String(100), nullable=False, index=True)
    operation = Column(String(20), nullable=False)
    target = Column(String(255), nullable=False)
    seat_type = Column(String(20), nullable=False)
    created_at = Column(DateTime, default=get_now, nullable=False)
    __table_args__ = (Index("idx_seat_hold_target", "account_id", "operation", "target", unique=True),)


class TeamAccount(Base):
    """Team Account 关联表"""
    __tablename__ = "team_accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    team_id = Column(Integer, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    account_id = Column(String(100), nullable=False, comment="Account ID")
    account_name = Column(String(255), comment="Account 名称")
    is_primary = Column(Boolean, default=False, comment="是否为主 Account")
    created_at = Column(DateTime, default=get_now, comment="创建时间")

    # 关系
    team = relationship("Team", back_populates="team_accounts")

    # 唯一约束
    __table_args__ = (
        Index("idx_team_account", "team_id", "account_id", unique=True),
    )


class TeamEmailMapping(Base):
    """Team 与邮箱关系映射表"""
    __tablename__ = "team_email_mappings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    team_id = Column(Integer, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    email = Column(String(255), nullable=False, comment="成员邮箱(统一存小写)")
    status = Column(String(20), default="invited", nullable=False, comment="状态: invited/joined/removed")
    source = Column(String(20), default="sync", nullable=False, comment="来源: redeem/admin_add/sync/api")
    is_admin_invited = Column(
        Boolean,
        default=False,
        nullable=False,
        comment="是否由后台管理员手工邀请（永久标记，自动同步流程不会覆盖）",
    )
    upstream_user_id = Column(String(255), comment="上游成员用户 ID")
    member_role = Column(String(50), comment="上游成员角色")
    joined_at = Column(DateTime, comment="成员实际加入 Team 的时间")
    auto_kick_at = Column(DateTime, comment="成员计划自动踢出时间")
    last_invited_at = Column(DateTime, comment="最近一次向该 Team 发送邀请的时间")
    last_seen_at = Column(DateTime, default=get_now, comment="最后一次确认该状态的时间")
    missing_sync_count = Column(Integer, default=0, nullable=False, comment="连续同步缺失次数")
    created_at = Column(DateTime, default=get_now, comment="创建时间")
    updated_at = Column(DateTime, default=get_now, onupdate=get_now, comment="更新时间")

    # 关系
    team = relationship("Team", back_populates="email_mappings")

    # 索引
    __table_args__ = (
        Index("idx_team_email_unique", "team_id", "email", unique=True),
        Index("idx_team_email_email", "email"),
        Index("idx_team_email_status", "team_id", "status"),
        Index("idx_team_email_auto_kick", "status", "auto_kick_at"),
    )


class AccountPoolEntry(Base):
    """后台维护的成员邮箱号池。"""
    __tablename__ = "account_pool_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(255), nullable=False, comment="成员邮箱(统一存小写)")
    seat_type = Column(
        String(20),
        default="default",
        nullable=False,
        comment="邀请席位类型: default/premium",
    )
    password_encrypted = Column(Text, comment="加密存储的 ChatGPT 登录密码")
    two_factor_secret_encrypted = Column(Text, comment="加密存储的 2FA 密钥")
    created_at = Column(DateTime, default=get_now, nullable=False)
    updated_at = Column(DateTime, default=get_now, onupdate=get_now, nullable=False)

    histories = relationship(
        "AccountPoolHistory",
        back_populates="account",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("idx_account_pool_email", "email", unique=True),
    )


class AccountPoolHistory(Base):
    """账号邮箱加入过 Team 的历史记录。"""
    __tablename__ = "account_pool_histories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_pool_id = Column(
        Integer,
        ForeignKey("account_pool_entries.id", ondelete="CASCADE"),
        nullable=False,
    )
    team_id = Column(Integer, ForeignKey("teams.id", ondelete="SET NULL"), nullable=True)
    team_name = Column(String(255), comment="历史 Team 名称快照")
    team_email = Column(String(255), comment="历史 Team 管理员邮箱快照")
    joined_at = Column(DateTime, nullable=False, comment="本次加入时间")
    left_at = Column(DateTime, comment="本次离开时间")
    created_at = Column(DateTime, default=get_now, nullable=False)

    account = relationship("AccountPoolEntry", back_populates="histories")

    __table_args__ = (
        Index("idx_account_pool_history_account", "account_pool_id", "joined_at"),
        Index("idx_account_pool_history_team", "team_id", "joined_at"),
    )


class RedemptionCode(Base):
    """兑换码表"""
    __tablename__ = "redemption_codes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(32), unique=True, nullable=False, comment="兑换码")
    status = Column(String(20), default="unused", comment="状态: unused/used/expired/warranty_active")
    created_at = Column(DateTime, default=get_now, comment="创建时间")
    expires_at = Column(DateTime, comment="过期时间")
    used_by_email = Column(String(255), comment="使用者邮箱")
    used_team_id = Column(Integer, ForeignKey("teams.id"), comment="使用的 Team ID")
    used_at = Column(DateTime, comment="使用时间")
    has_warranty = Column(Boolean, default=False, comment="是否为质保兑换码")
    warranty_days = Column(Integer, default=30, comment="质保时长(天)")
    extension_days = Column(Integer, default=0, comment="人工续期累计天数")
    warranty_expires_at = Column(DateTime, comment="质保到期时间(首次使用后根据质保时长计算)")
    pool_type = Column(String(20), default="normal", comment="兑换池类型: normal/welfare")
    reusable_by_seat = Column(Boolean, default=False, comment="是否可按席位重复使用")

    # 关系
    redemption_records = relationship("RedemptionRecord", back_populates="redemption_code")
    renewal_requests = relationship("RenewalRequest", back_populates="redemption_code")

    # 索引
    __table_args__ = (
        Index("idx_code_status", "code", "status"),
    )


class RedemptionRecord(Base):
    """使用记录表"""
    __tablename__ = "redemption_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(255), nullable=False, comment="用户邮箱")
    code = Column(String(32), ForeignKey("redemption_codes.code"), nullable=False, comment="兑换码")
    team_id = Column(Integer, ForeignKey("teams.id"), nullable=False, comment="Team ID")
    account_id = Column(String(100), nullable=False, comment="Account ID")
    redeemed_at = Column(DateTime, default=get_now, comment="兑换时间")
    is_warranty_redemption = Column(Boolean, default=False, comment="是否为质保兑换")

    # 关系
    team = relationship("Team", back_populates="redemption_records")
    redemption_code = relationship("RedemptionCode", back_populates="redemption_records")

    # 索引
    __table_args__ = (
        Index("idx_email", "email"),
    )


class RenewalRequest(Base):
    """兑换码续期请求表"""
    __tablename__ = "renewal_requests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(255), nullable=False, comment="申请续期的用户邮箱")
    # code 允许为空：当兑换码被销毁时，extended/ignored 历史记录保留作为审计证据，
    # 此时 FK 指向已不存在的码会失败，因此销毁兑换码会把这些行的 code 置 NULL，
    # 原始码值会同步追加到 admin_note 中保证可追溯。
    code = Column(String(32), ForeignKey("redemption_codes.code"), nullable=True, comment="兑换码")
    team_id = Column(Integer, ForeignKey("teams.id"), comment="申请时关联的 Team ID")
    status = Column(String(20), default="pending", nullable=False, comment="状态: pending/extended/ignored")
    requested_at = Column(DateTime, default=get_now, comment="申请时间")
    handled_at = Column(DateTime, comment="处理时间")
    extension_days = Column(Integer, comment="管理员批准的续期天数")
    admin_note = Column(Text, comment="管理员备注")

    # 关系
    redemption_code = relationship("RedemptionCode", back_populates="renewal_requests")

    __table_args__ = (
        Index("idx_renewal_request_status", "status"),
        Index("idx_renewal_request_email", "email"),
        Index("idx_renewal_request_code", "code"),
    )


class Setting(Base):
    """系统设置表"""
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(100), unique=True, nullable=False, comment="配置项名称")
    value = Column(Text, comment="配置项值")
    description = Column(String(255), comment="配置项描述")
    created_at = Column(DateTime, default=get_now, comment="创建时间")
    updated_at = Column(DateTime, default=get_now, onupdate=get_now, comment="更新时间")

    # 索引
    __table_args__ = (
        Index("idx_key", "key"),
    )
