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
SETTING_SPECS = (
    {'key':'model.chat_api_key','name':'聊天模型 API Key','category':'model','sensitive':True,'description':'聊天模型访问密钥。','default':lambda: os.getenv('OPENAI_API_KEY','')},
    {'key':'model.chat_base_url','name':'聊天模型地址','category':'model','sensitive':False,'description':'聊天模型 API 基础地址。','kind':'url','default':lambda: os.getenv('OPENAI_API_BASE','https://dashscope.aliyuncs.com/compatible-mode/v1')},
    {'key':'model.chat_model','name':'聊天模型名称','category':'model','sensitive':False,'description':'默认聊天模型。','default':lambda: os.getenv('LLM_DEFAULT_MODEL','qwen-flash')},
    {'key':'embedding.api_key','name':'Embedding API Key','category':'embedding','sensitive':True,'description':'向量化服务访问密钥。','default':lambda: os.getenv('EMBEDDING_API_KEY','')},
    {'key':'embedding.base_url','name':'Embedding 服务地址','category':'embedding','sensitive':False,'description':'向量化服务 API 基础地址。','kind':'url','default':lambda: os.getenv('EMBEDDING_API_BASE',os.getenv('OPENAI_API_BASE',''))},
    {'key':'embedding.model','name':'Embedding 模型名称','category':'embedding','sensitive':False,'description':'文档向量化模型。','default':lambda: os.getenv('EMBEDDING_MODEL','text-embedding-v4')},
    {'key':'embedding.dimension','name':'向量维度','category':'embedding','sensitive':False,'description':'向量索引维度。','kind':'integer','minimum':1,'default':lambda: os.getenv('EMBEDDING_DIM','1536')},
    {'key':'milvus.url','name':'Milvus 地址','category':'vector','sensitive':False,'description':'Milvus 服务地址。','kind':'url','default':lambda: os.getenv('MILVUS_URL','http://localhost:19530')},
    {'key':'milvus.collection','name':'文档集合名称','category':'vector','sensitive':False,'description':'文档向量集合。','default':lambda: os.getenv('CHUNKS_COLLECTION','kb_chunks')},
    {'key':'document.max_file_size_mb','name':'单文件大小限制 (MB)','category':'document','sensitive':False,'description':'允许上传的最大文件大小。','kind':'integer','minimum':1,'maximum':2048,'default':lambda: '50'},
    {'key':'document.chunk_size','name':'Chunk 大小','category':'document','sensitive':False,'description':'文档切分目标长度。','kind':'integer','minimum':100,'maximum':10000,'default':lambda: '800'},
    {'key':'document.chunk_overlap','name':'Chunk 重叠长度','category':'document','sensitive':False,'description':'相邻 Chunk 重叠长度。','kind':'integer','minimum':0,'maximum':5000,'default':lambda: '120'},
    {'key':'site.name','name':'站点名称','category':'site','sensitive':False,'description':'前后台显示名称。','default':lambda: os.getenv('APP_NAME','工智库')},
    {'key':'site.default_library_code','name':'默认知识库','category':'site','sensitive':False,'description':'新会话默认知识库编码。','default':lambda: ''},
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
    await session.commit()
