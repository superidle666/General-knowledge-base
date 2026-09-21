# -*- coding: utf-8 -*-
"""
系统配置管理接口。
@author: 项目维护者
@date: 2026-09-02
@desc: 仅允许系统管理员读取和更新已注册的系统配置。
@business: 敏感配置只写不读，接口响应不返回密钥明文。
"""
"""Administrator-only encrypted system-setting endpoints."""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from backend.app.api.schemas import SystemSettingOut, SystemSettingUpdate
from backend.app.core.audit import bind_audit_context, log_operation
from backend.app.core.auth import AuthenticatedUser, get_current_user
from backend.app.core.system_settings import decrypt_value, encrypt_value, mask_value, setting_spec, validate_value
from backend.app.db.models import SystemSetting
from backend.app.db.session import get_db

router = APIRouter(prefix='/api/v1/settings', tags=['system-settings'])


# NOTE: 系统设置独立于通用管理权限，仅 admin 角色可访问，避免普通管理员修改基础服务配置。
async def require_system_admin(request: Request, current_user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
    if not current_user.is_system_admin:
        raise HTTPException(403, '仅系统管理员可以管理系统设置')
    bind_audit_context(current_user, request)
    return current_user

def serialize(item: SystemSetting) -> dict:
    value = decrypt_value(item.encrypted_value)
    return {
        'key': item.setting_key, 'name': item.display_name, 'category': item.category,
        'description': item.description, 'is_sensitive': item.is_sensitive,
        'value': None if item.is_sensitive else value,
        'masked_value': mask_value(value) if item.is_sensitive else None,
        'has_value': bool(value), 'updated_at': item.updated_at, 'updated_by': item.updated_by,
    }

@router.get('', response_model=list[SystemSettingOut])
async def list_settings(session: AsyncSession = Depends(get_db), _: AuthenticatedUser = Depends(require_system_admin)):
    rows = (await session.execute(select(SystemSetting).order_by(SystemSetting.category, SystemSetting.id))).scalars()
    return [serialize(item) for item in rows]

@router.patch('/{setting_key}', response_model=SystemSettingOut)
async def update_setting(setting_key: str, payload: SystemSettingUpdate, session: AsyncSession = Depends(get_db), current_user: AuthenticatedUser = Depends(require_system_admin)):
    spec = setting_spec(setting_key)
    item = await session.scalar(select(SystemSetting).where(SystemSetting.setting_key == setting_key))
    if item is None:
        raise HTTPException(404, '系统配置尚未初始化')
    # NOTE: 敏感配置空提交代表保持原值，避免前端未回显明文时意外覆盖已生效的密钥。
    # Sensitive values are write-only. An empty value deliberately preserves the existing secret.
    if spec['sensitive'] and not (payload.value or '').strip():
        return serialize(item)
    if payload.value is None:
        raise HTTPException(422, '请填写配置值')
    item.encrypted_value = encrypt_value(validate_value(spec, payload.value))
    item.updated_by = current_user.id
    await log_operation(session, 'update', 'system_setting', setting_key, {'key': setting_key, 'sensitive': spec['sensitive']})
    await session.commit()
    await session.refresh(item)
    return serialize(item)