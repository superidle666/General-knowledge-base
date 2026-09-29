# -*- coding: utf-8 -*-
"""
系统配置注册表与加密工具。
@author: 项目维护者
@date: 2026-09-02
@desc: 声明可在线维护的配置项，并提供加密、脱敏和格式校验能力。
@business: 配置键由代码注册表维护；数据库只保存加密值，敏感内容不得写入日志或响应。
"""

from __future__ import annotations

import os
from urllib.parse import urlparse
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from backend.app.core.config import settings


# 配置项白名单：未在此注册的环境变量或配置键不能通过管理端修改。
# 2026-09-29 范围收敛：只保留“模型服务”。Milvus/MinIO 等基础设施连接参数走 .env；
# 切片参数不再在线管理（库级 chunking_config 已覆盖真实需求，P0-2 取消）；
# embedding.* 四项 API 假配置删除（实际向量化是本地 BGE-M3，替换为真实路径/设备/精度三项）。
SETTING_SPECS = (
    {'key':'model.chat_api_key','name':'聊天模型 API Key','category':'model','sensitive':True,'description':'聊天模型访问密钥，同时用于视觉理解与重排序（同一 DashScope 账号）；保存即生效。','default':lambda: os.getenv('OPENAI_API_KEY','')},
    {'key':'model.chat_base_url','name':'聊天模型地址','category':'model','sensitive':False,'description':'聊天模型 API 基础地址。','kind':'url','default':lambda: os.getenv('OPENAI_API_BASE','https://dashscope.aliyuncs.com/compatible-mode/v1')},
    {'key':'model.chat_model','name':'聊天模型名称','category':'model','sensitive':False,'description':'默认聊天模型。','default':lambda: os.getenv('LLM_DEFAULT_MODEL','qwen-flash')},
    {'key':'model.vl_model','name':'视觉模型名称','category':'model','sensitive':False,'description':'图片摘要使用的多模态模型（与聊天模型共用地址和密钥）。','default':lambda: os.getenv('VL_MODEL','qwen3-vl-flash')},
    {'key':'rerank.text_model','name':'重排序模型名称','category':'model','sensitive':False,'description':'DashScope TextReRank 精排模型；每次检索按需调用，保存即生效。','default':lambda: os.getenv('TEXT_RERANK_MODEL','qwen3-rerank')},
    {'key':'rerank.text_instruct','name':'重排序指令','category':'model','sensitive':False,'description':'重排指令文本，影响排序口径；留空则沿用环境变量默认。','default':lambda: os.getenv('TEXT_RERANK_INSTRUCT','')},
    {'key':'embedding.model_path','name':'向量化模型路径','category':'model','sensitive':False,'description':'本地 BGE-M3 模型路径；模型常驻内存，重启后首次向量化时才按新值加载。','default':lambda: os.getenv('BGE_M3_PATH','')},
    {'key':'embedding.device','name':'向量化计算设备','category':'model','sensitive':False,'description':'BGE-M3 加载设备，如 cuda:0 / cpu；重启后首次加载生效。','default':lambda: os.getenv('BGE_DEVICE','cpu')},
    {'key':'embedding.fp16','name':'向量化半精度','category':'model','sensitive':False,'description':'1=开启、0=关闭；重启后首次加载生效。','default':lambda: '1' if os.getenv('BGE_FP16','') in ('1','True','true') else '0'},
)
_BY_KEY={item['key']:item for item in SETTING_SPECS}
def setting_spec(key: str) -> dict:
    if key not in _BY_KEY: raise HTTPException(404,'该系统配置不存在或不允许在线维护')
    return _BY_KEY[key]
def default_value(spec: dict) -> str: return str(spec['default']() or '').strip()
def _fernet() -> Fernet:
    if not settings.settings_encryption_key: raise RuntimeError('SETTINGS_ENCRYPTION_KEY must be configured before starting the API')
    try: return Fernet(settings.settings_encryption_key.encode('ascii'))
    except (ValueError,TypeError) as exc: raise RuntimeError('SETTINGS_ENCRYPTION_KEY is invalid') from exc
def encrypt_value(value: str) -> str: return _fernet().encrypt(value.encode('utf-8')).decode('ascii')
def decrypt_value(value: str | None) -> str | None:
    if value is None: return None
    try: return _fernet().decrypt(value.encode('ascii')).decode('utf-8')
    except InvalidToken as exc: raise RuntimeError('system setting cannot be decrypted with SETTINGS_ENCRYPTION_KEY') from exc

# NOTE: 脱敏展示仅用于提示配置是否存在，不能用于还原或替代实际密钥。
def mask_value(value: str | None) -> str | None:
    if not value: return None
    return '*' * len(value) if len(value)<=4 else value[:3]+'****'+value[-3:]
def validate_value(spec: dict,value: str) -> str:
    value=value.strip()
    if not value: raise HTTPException(422,'配置值不能为空')
    if spec.get('kind')=='integer':
        try: number=int(value)
        except ValueError as exc: raise HTTPException(422,'该配置必须为整数') from exc
        if number<spec.get('minimum',number) or number>spec.get('maximum',number): raise HTTPException(422,'配置值超出允许范围')
        return str(number)
    if spec.get('kind')=='url':
        parsed=urlparse(value)
        if parsed.scheme not in {'http','https'} or not parsed.netloc: raise HTTPException(422,'该配置必须是有效的 HTTP(S) 地址')
    return value

async def sync_system_settings(session) -> None:
    """Create missing registered settings without replacing saved values."""
    from sqlalchemy import select
    from backend.app.db.models import SystemSetting
    rows = (await session.execute(select(SystemSetting))).scalars()
    existing = {row.setting_key: row for row in rows}
    for spec in SETTING_SPECS:
        item = existing.get(spec["key"])
        if item is None:
            session.add(SystemSetting(setting_key=spec["key"], display_name=spec["name"], category=spec["category"], encrypted_value=encrypt_value(default_value(spec)), is_sensitive=spec["sensitive"], description=spec["description"]))
        else:
            item.display_name = spec["name"]
            item.category = spec["category"]
            item.is_sensitive = spec["sensitive"]
            item.description = spec["description"]
    # 注册表收缩后清理孤儿行（如已下线的 chunk/milvus/site 项）：这些值无任何运行时消费方，
    # 留着只会在列表接口里继续展示一个 PATCH 必 404 的假入口；审计历史在独立表，不受影响。
    for key, item in existing.items():
        if key not in _BY_KEY:
            session.delete(item)
    await session.commit()
