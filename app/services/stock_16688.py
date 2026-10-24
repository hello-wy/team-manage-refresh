"""16688 卡密补货：凭据只在服务端组装和发送。"""
import asyncio
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlparse
from weakref import WeakValueDictionary

import httpx
from sqlalchemy import func, select

from app.models import AccountPoolEntry, Team
from app.services.account_pool_rotation import attach_rotation_status
from app.services.encryption import encryption_service
from app.services.settings import settings_service

DEFAULT_GATEWAY = 'https://www.16688.com.cn/openApi'
STOCK_PATH = '/stockSecretBatch/add'
_locks = WeakValueDictionary()


class Stock16688Error(ValueError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class Stock16688Config:
    gateway: str
    app_id: str
    secret: str
    goods_no: str


def normalize_gateway(value):
    value = (value or '').strip().rstrip('/')
    try:
        parsed = urlparse(value)
        parsed.port
    except ValueError:
        raise Stock16688Error('16688 网关地址无效') from None
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or any(c.isspace() for c in value) or parsed.path.lower() not in ('', '/openapi')):
        raise Stock16688Error('16688 网关必须是 HTTPS 站点根地址或 /openApi 地址')
    return f'{parsed.scheme}://{parsed.netloc}/openApi'


def canonical_value(value):
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return str(value)


def sign_params(params, secret):
    parts = '&'.join(f'{key}={canonical_value(params[key])}' for key in sorted(params) if key != 'sign')
    plain = 'POST\nstocksecretbatch\nadd\n' + parts
    return hmac.new(secret.encode(), plain.encode(), hashlib.sha256).hexdigest()


async def public_settings(db):
    return {
        'stock_16688_gateway': await settings_service.get_setting(db, 'stock_16688_gateway', DEFAULT_GATEWAY, use_cache=False),
        'stock_16688_app_id': await settings_service.get_setting(db, 'stock_16688_app_id', '', use_cache=False),
        'stock_16688_goods_no': await settings_service.get_setting(db, 'stock_16688_goods_no', '', use_cache=False),
        'stock_16688_has_secret': bool(await settings_service.get_setting(db, 'stock_16688_secret_encrypted', '', use_cache=False)),
    }


async def save_settings(db, *, gateway, app_id, goods_no, secret):
    gateway = normalize_gateway(gateway)
    app_id, goods_no = app_id.strip(), goods_no.strip()
    if not app_id or not goods_no:
        raise Stock16688Error('请填写 16688 App ID 和商品编号')
    existing = await settings_service.get_setting(db, 'stock_16688_secret_encrypted', '', use_cache=False)
    if not secret.strip() and not existing:
        raise Stock16688Error('请填写 16688 Secret')
    values = {'stock_16688_gateway': gateway, 'stock_16688_app_id': app_id, 'stock_16688_goods_no': goods_no}
    if secret.strip():
        values['stock_16688_secret_encrypted'] = encryption_service.encrypt_token(secret.strip())
    if not await settings_service.update_settings(db, values):
        raise Stock16688Error('16688 配置保存失败', 500)
    return {'success': True, 'message': '16688 补货配置已保存', 'gateway': gateway}


async def load_config(db):
    values = {}
    for key, default in (('gateway', DEFAULT_GATEWAY), ('app_id', ''), ('goods_no', ''), ('secret_encrypted', '')):
        values[key] = await settings_service.get_setting(db, 'stock_16688_' + key, default, use_cache=False)
    if not all(values.values()):
        raise Stock16688Error('请先在系统设置中配置 16688 网关、App ID、Secret 和商品编号')
    try:
        secret = encryption_service.decrypt_token(values['secret_encrypted'])
    except Exception:
        raise Stock16688Error('16688 Secret 无法读取，请在系统设置中重新保存') from None
    if not secret.strip():
        raise Stock16688Error('请在系统设置中重新保存 16688 Secret')
    return Stock16688Config(normalize_gateway(values['gateway']), values['app_id'], secret, values['goods_no'])


class Stock16688Service:
    def __init__(self, client_factory=httpx.AsyncClient):
        self.client_factory = client_factory

    async def import_entry(self, db, entry_id):
        # 同一进程防双击；跨进程和重试由接口 remove_repeat 去重。
        lock = _locks.setdefault(entry_id, asyncio.Lock())
        async with lock:
            return await self._import_entry(db, entry_id)

    async def _import_entry(self, db, entry_id):
        entry = await db.get(AccountPoolEntry, entry_id, populate_existing=True)
        if not entry or entry.deleted_at is not None:
            raise Stock16688Error('账号不存在或已删除', 404)
        owner = await db.scalar(select(Team.id).where(
            func.lower(func.trim(Team.email)) == entry.email.strip().lower(),
        ).limit(1))
        rows = await attach_rotation_status(db, [{'id': entry.id, 'email': entry.email, 'is_owner': owner is not None}])
        if not rows[0]['rotation'].get('can_import_16688'):
            raise Stock16688Error('仅轮转状态为“待接受邀请”的账户可导入 16688，请刷新号池', 409)
        config = await load_config(db)
        try:
            password = encryption_service.decrypt_token(entry.password_encrypted) if entry.password_encrypted else ''
            two_factor = encryption_service.decrypt_token(entry.two_factor_secret_encrypted) if entry.two_factor_secret_encrypted else ''
        except Exception:
            raise Stock16688Error('账号密码或 2FA 无法读取，请先重新保存账号凭据') from None
        if not password.strip() or not two_factor.strip():
            raise Stock16688Error('该账号缺少密码或 2FA，请先在账号详情中补充凭据')
        fields = [entry.email, password, two_factor]
        if any('\n' in field or '\r' in field or '---' in field for field in fields):
            raise Stock16688Error('账号凭据包含换行或 --- 分隔符，无法按单条卡密格式补货')
        params = {
            'app_id': config.app_id, 'timestamp': str(int(time.time())), 'nonce': secrets.token_hex(16),
            'goods_no': config.goods_no, 'content': '---'.join(fields),
            'first': 0, 'remove_repeat': 1, 'split_type': 1, 'expire_time': 0,
        }
        params['sign'] = sign_params(params, config.secret)
        uncertain = '16688 补货结果未确认，可能已入库，请先到 16688 核对库存；本系统不会自动重试'
        try:
            async with self.client_factory(timeout=20.0, follow_redirects=False) as client:
                response = await client.post(config.gateway + STOCK_PATH, json=params)
        except httpx.RequestError:
            raise Stock16688Error(uncertain, 502) from None
        if not response.is_success:
            raise Stock16688Error(uncertain if response.status_code >= 500 else '16688 请求失败，请检查网关与接入权限', 502)
        try:
            body = response.json()
        except ValueError:
            raise Stock16688Error(uncertain, 502) from None
        # 上游可能回显卡密，不能把原始响应或错误信息透传到前端/日志。
        if not isinstance(body, dict) or type(body.get('code')) is not int:
            raise Stock16688Error(uncertain, 502)
        if body['code'] != 1:
            raise Stock16688Error('16688 拒绝补货，请检查 App ID、Secret、商品编号及自营卡密商品权限', 502)
        data = body.get('data')
        if not isinstance(data, dict) or data.get('goods_no') != config.goods_no:
            raise Stock16688Error(uncertain, 502)
        imported, duplicated = data.get('import_count'), data.get('stock_dup_count')
        if (type(imported) is not int or type(duplicated) is not int
                or imported not in (0, 1) or duplicated not in (0, 1) or imported + duplicated != 1):
            raise Stock16688Error(uncertain, 502)
        return {'success': True, 'import_count': imported, 'stock_dup_count': duplicated,
                'message': '已成功补货至 16688' if imported else '16688 已存在相同卡密，无需重复补货'}


stock_16688_service = Stock16688Service()
