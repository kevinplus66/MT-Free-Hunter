"""
MT-Free-Hunter - M-Team 免费种子猎手
自动搜索当前所有 Free / 2xFree 种子
"""

import os
import re
import asyncio
import logging
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Union
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Query, HTTPException, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator

# ============ 日志配置 ============
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


# ============ Pydantic Models for Request Validation ============
class CollectionRequest(BaseModel):
    """Request model for collection toggle"""
    id: str = Field(..., min_length=1, max_length=20)
    make: bool = True

    @validator('id')
    def validate_torrent_id(cls, v):
        """Validate torrent ID is numeric only"""
        if not re.match(r'^\d+$', v):
            raise ValueError('Invalid torrent ID format')
        return v


# ============ Safe Environment Variable Parsing ============
def safe_int(value: str, default: int, min_val: int = 0, max_val: int = 999999999) -> int:
    """Safely parse integer from string with bounds checking"""
    try:
        result = int(value)
        return max(min_val, min(result, max_val))
    except (ValueError, TypeError):
        return default


# ============ 配置 ============
MT_API_BASE = "https://api.m-team.io/api"
MT_SEARCH_URL = f"{MT_API_BASE}/torrent/search"
MT_CATEGORY_URL = f"{MT_API_BASE}/torrent/categoryList"
MT_TOKEN = os.getenv("MT_TOKEN", "")
MT_USER_ID = os.getenv("MT_USER_ID", "")
REFRESH_INTERVAL = safe_int(os.getenv("REFRESH_INTERVAL", "600"), 600, min_val=60, max_val=86400)
MT_SITE_URL = os.getenv("MT_SITE_URL", "https://kp.m-team.cc")
API_DELAY = max(0.5, min(float(os.getenv("API_DELAY", "1") or "1"), 10))  # API请求间隔（秒），限制0.5-10秒

# API URLs
MT_COLLECTION_URL = f"{MT_API_BASE}/torrent/collection"
MT_COLLECTION_LIST_URL = f"{MT_API_BASE}/member/collection"
MT_USER_TORRENT_URL = f"{MT_API_BASE}/member/getUserTorrentList"
MT_PROFILE_URL = f"{MT_API_BASE}/member/profile"

# Rival user ID for comparison (optional)
RIVAL_USER_ID = os.getenv("RIVAL_USER_ID", "")

# PushPlus 微信推送配置
PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN", "")
PUSHPLUS_URL = "http://www.pushplus.plus/send"
ALERT_THRESHOLD_MINUTES = 10  # 免费即将到期报警阈值（分钟）
ALERT_COOLDOWN = 1800  # 30分钟内不重复报警同一种子

# 北京时区 (UTC+8)
BEIJING_TZ = timezone(timedelta(hours=8))

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# ============ 全局状态 ============
cached_data: Dict[str, Any] = {
    "torrents": [],
    "categories": [],
    "last_update": None,
    "error": None
}

user_torrent_status: Dict[str, Dict] = {
    "seeding": {},
    "leeching": {},
}

user_collection_ids: set = set()

user_profile: Dict[str, Any] = {
    "share_ratio": 0,
    "uploaded": 0,
    "downloaded": 0,
    "uploaded_display": "0 B",
    "downloaded_display": "0 B"
}

rival_profile: Dict[str, Any] = {
    "share_ratio": 0,
    "uploaded": 0,
    "downloaded": 0,
    "uploaded_display": "0 B",
    "downloaded_display": "0 B"
}

# 历史免费种子ID追踪（用于检测"变节"- 免费变收费）
known_free_torrent_ids: set = set()

# 已发送报警记录（防止重复报警）
sent_alerts: Dict[str, float] = {}  # {torrent_id_alerttype: timestamp}

# 全局 HTTP 客户端（复用连接池）
http_client: Optional[httpx.AsyncClient] = None

# ============ 模板配置 ============
templates = Jinja2Templates(directory="app/templates")


# ============ HTTP 客户端管理 ============
async def get_http_client() -> httpx.AsyncClient:
    """获取或创建 HTTP 客户端"""
    global http_client
    if http_client is None or http_client.is_closed:
        http_client = httpx.AsyncClient(timeout=30.0)
    return http_client


def get_headers() -> Dict[str, str]:
    """获取 API 请求头"""
    return {
        "User-Agent": USER_AGENT,
        "x-api-key": MT_TOKEN.strip(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


# ============ 工具函数 ============
def parse_datetime(dt_string: Optional[str]) -> Optional[datetime]:
    """解析 API 返回的时间字符串"""
    if not dt_string:
        return None

    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%SZ",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(dt_string, fmt)
        except ValueError:
            continue
    return None


def format_size(size_bytes: int) -> str:
    """将字节数转换为人类可读格式"""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} PB"


def calculate_remaining_time(end_time: Optional[datetime]) -> Dict[str, Any]:
    """计算免费剩余时间"""
    if end_time is None:
        return {
            "display": "永久免费",
            "display_en": "Permanent",
            "status": "permanent",
            "color": "green",
            "hours": float('inf'),
            "timestamp": None
        }

    now = datetime.now(BEIJING_TZ).replace(tzinfo=None)
    total_seconds = (end_time - now).total_seconds()

    if total_seconds <= 0:
        return {
            "display": "已过期",
            "display_en": "Expired",
            "status": "expired",
            "color": "red",
            "hours": 0,
            "timestamp": end_time.isoformat()
        }

    hours = int(total_seconds // 3600)
    minutes = int((total_seconds % 3600) // 60)
    total_hours = hours + minutes / 60

    # 格式化显示
    if hours >= 24:
        days, remaining_hours = divmod(hours, 24)
        display = f"{days}天 {remaining_hours}小时"
        display_en = f"{days}d {remaining_hours}h"
    else:
        display = f"{hours}小时 {minutes}分"
        display_en = f"{hours}h {minutes}m"

    # 确定状态和颜色
    if total_hours >= 6:
        color, status = "green", "safe"
    elif total_hours >= 2:
        color, status = "yellow", "warning"
    elif total_hours >= 1:
        color, status = "orange", "danger"
    else:
        color, status = "red", "critical"

    return {
        "display": display,
        "display_en": display_en,
        "status": status,
        "color": color,
        "hours": total_hours,
        "timestamp": end_time.isoformat()
    }


def get_discount_label(discount: Optional[str]) -> Dict[str, str]:
    """获取优惠标签"""
    labels = {
        "FREE": {"zh": "免费", "en": "Free"},
        "_2X_FREE": {"zh": "2x免费", "en": "2x Free"},
        "PERCENT_50": {"zh": "50%", "en": "50%"},
        "_2X_PERCENT_50": {"zh": "2x50%", "en": "2x50%"},
        "_2X": {"zh": "2x上传", "en": "2x UP"},
        "PERCENT_30": {"zh": "30%", "en": "30%"},
        "PERCENT_70": {"zh": "70%", "en": "70%"},
        "NORMAL": {"zh": "无优惠", "en": "None"}
    }
    return labels.get(discount, {"zh": discount or "未知", "en": discount or "Unknown"})


# ============ API 请求函数 ============
async def fetch_categories() -> List[Dict]:
    """获取种子类别列表"""
    if not MT_TOKEN:
        return []

    try:
        client = await get_http_client()
        response = await client.post(MT_CATEGORY_URL, headers=get_headers())
        data = response.json()
        if data.get("code") == "0":
            return data.get("data", [])
    except Exception as e:
        logger.error(f"获取类别失败: {e}")
    return []


async def search_free_torrents(
    discount_type: str = "FREE",
    mode: str = "normal",
    page: int = 1,
    page_size: int = 100
) -> List[Dict]:
    """搜索免费种子"""
    if not MT_TOKEN:
        return []

    payload = {
        "mode": mode,
        "discount": discount_type,
        "pageNumber": page,
        "pageSize": page_size
    }

    try:
        client = await get_http_client()
        response = await client.post(MT_SEARCH_URL, headers=get_headers(), json=payload)
        data = response.json()

        if data.get("code") == "0":
            return data.get("data", {}).get("data", [])
        else:
            logger.error(f"搜索 {discount_type} (mode={mode}) 失败: {data.get('message')}")
    except Exception as e:
        logger.error(f"搜索 {discount_type} (mode={mode}) 异常: {e}")

    return []


async def fetch_user_torrent_status() -> None:
    """获取用户的做种和下载中的种子状态"""
    global user_torrent_status

    if not MT_TOKEN or not MT_USER_ID:
        return

    try:
        userid = int(MT_USER_ID)
        client = await get_http_client()

        # 获取做种中的种子
        seeding_payload = {"userid": userid, "type": "SEEDING", "pageNumber": 1, "pageSize": 200}
        seeding_response = await client.post(MT_USER_TORRENT_URL, headers=get_headers(), json=seeding_payload)
        seeding_data = seeding_response.json()

        if seeding_data.get("code") == "0":
            seeding_list = seeding_data.get("data", {}).get("data", [])
            user_torrent_status["seeding"] = {
                str(item.get("torrent", {}).get("id", item.get("id", ""))): item
                for item in seeding_list
            }
            logger.info(f"获取到 {len(user_torrent_status['seeding'])} 个做种中种子")

        # 增加延迟避免 API 速率限制
        await asyncio.sleep(max(API_DELAY, 2))

        # 获取下载中的种子
        leeching_payload = {"userid": userid, "type": "LEECHING", "pageNumber": 1, "pageSize": 200}
        leeching_response = await client.post(MT_USER_TORRENT_URL, headers=get_headers(), json=leeching_payload)
        leeching_data = leeching_response.json()
        logger.debug(f"LEECHING API 响应: code={leeching_data.get('code')}, data keys={list(leeching_data.get('data', {}).keys()) if isinstance(leeching_data.get('data'), dict) else type(leeching_data.get('data'))}")

        if leeching_data.get("code") == "0":
            leeching_list = leeching_data.get("data", {}).get("data", [])
            user_torrent_status["leeching"] = {
                str(item.get("torrent", {}).get("id", item.get("id", ""))): item
                for item in leeching_list
            }
            logger.info(f"获取到 {len(user_torrent_status['leeching'])} 个下载中种子")
        else:
            logger.warning(f"获取下载中种子失败: code={leeching_data.get('code')}, message={leeching_data.get('message')}")

    except Exception as e:
        logger.error(f"获取用户种子状态失败: {e}")


async def fetch_user_collection() -> None:
    """获取用户收藏列表"""
    global user_collection_ids

    if not MT_TOKEN:
        return

    try:
        client = await get_http_client()
        payload = {"pageNumber": 1, "pageSize": 200}
        response = await client.post(MT_COLLECTION_LIST_URL, headers=get_headers(), json=payload)
        data = response.json()

        if data.get("code") == "0":
            collection_list = data.get("data", {}).get("data", [])
            user_collection_ids = set()
            for item in collection_list:
                if isinstance(item, dict):
                    torrent_id = str(item.get("torrent", {}).get("id", item.get("id", "")))
                else:
                    torrent_id = str(item)
                if torrent_id:
                    user_collection_ids.add(torrent_id)
            logger.info(f"获取到 {len(user_collection_ids)} 个收藏种子")

    except Exception as e:
        logger.error(f"获取收藏列表失败: {e}")


async def fetch_user_profile() -> None:
    """获取用户资料（分享率、上传、下载）"""
    global user_profile

    if not MT_TOKEN:
        return

    if not MT_USER_ID:
        logger.warning("未配置 MT_USER_ID，无法获取用户资料")
        return

    try:
        profile_data = await _fetch_profile_by_uid(MT_USER_ID)
        if profile_data:
            user_profile = profile_data
            logger.debug(f"获取用户资料: 分享率={profile_data['share_ratio']:.2f}")

    except Exception as e:
        logger.error(f"获取用户资料失败: {e}")


async def fetch_rival_profile() -> None:
    """获取对手用户资料（分享率）"""
    global rival_profile

    if not MT_TOKEN:
        return

    if not RIVAL_USER_ID:
        logger.info("未配置 RIVAL_USER_ID，跳过获取对手资料")
        return

    try:
        profile_data = await _fetch_profile_by_uid(RIVAL_USER_ID)
        if profile_data:
            rival_profile = profile_data
            logger.debug(f"获取对手资料: 分享率={profile_data['share_ratio']:.2f}")

    except Exception as e:
        logger.error(f"获取对手资料失败: {e}")


async def _fetch_profile_by_uid(uid: str) -> Optional[Dict[str, Any]]:
    """通用函数：根据用户ID获取资料"""
    try:
        client = await get_http_client()

        headers = {
            "User-Agent": USER_AGENT,
            "x-api-key": MT_TOKEN.strip(),
            "Accept": "application/json",
        }
        form_data = {"uid": str(uid)}
        response = await client.post(MT_PROFILE_URL, headers=headers, data=form_data)
        data = response.json()

        logger.debug(f"Profile API 响应 (uid={uid}): code={data.get('code')}")

        if data.get("code") == "0":
            member_data = data.get("data", {})

            # 尝试多种数据结构路径
            member_count = member_data.get("memberCount", {})

            # 尝试从 memberCount 获取
            uploaded = _safe_int(member_count.get("uploaded", 0))
            downloaded = _safe_int(member_count.get("downloaded", 0))
            share_ratio_from_api = member_count.get("shareRate")

            # 如果 memberCount 没有数据，尝试从 member_data 直接获取
            if uploaded == 0 and downloaded == 0:
                uploaded = _safe_int(member_data.get("uploaded", 0))
                downloaded = _safe_int(member_data.get("downloaded", 0))
                if share_ratio_from_api is None:
                    share_ratio_from_api = member_data.get("shareRate")

            # 如果还没有，尝试从 member 字段获取
            if uploaded == 0 and downloaded == 0:
                member = member_data.get("member", {})
                uploaded = _safe_int(member.get("uploaded", 0))
                downloaded = _safe_int(member.get("downloaded", 0))
                if share_ratio_from_api is None:
                    share_ratio_from_api = member.get("shareRate")

            # 使用 API 返回的分享率，或者自己计算
            if share_ratio_from_api is not None:
                try:
                    share_ratio = float(share_ratio_from_api)
                except (ValueError, TypeError):
                    share_ratio = 0.0
            elif downloaded > 0:
                share_ratio = uploaded / downloaded
            else:
                share_ratio = 99999.99 if uploaded > 0 else 0.0

            return {
                "share_ratio": share_ratio,
                "uploaded": uploaded,
                "downloaded": downloaded,
                "uploaded_display": format_size(uploaded),
                "downloaded_display": format_size(downloaded)
            }
        else:
            logger.warning(f"获取用户资料失败 (uid={uid}): {data.get('message')}")
            return None

    except Exception as e:
        logger.error(f"获取用户资料异常 (uid={uid}): {e}")
        return None


def _safe_int(value: Any) -> int:
    """Safely convert value to int"""
    try:
        return int(value or 0)
    except (ValueError, TypeError):
        return 0


# ============ PushPlus 推送功能 ============
async def send_pushplus_alert(title: str, content: str) -> bool:
    """
    发送 PushPlus 微信推送通知

    Args:
        title: 通知标题
        content: 通知内容（支持HTML格式）

    Returns:
        bool: 是否发送成功
    """
    if not PUSHPLUS_TOKEN:
        logger.warning("未配置 PUSHPLUS_TOKEN，跳过推送")
        return False

    try:
        client = await get_http_client()
        payload = {
            "token": PUSHPLUS_TOKEN,
            "title": title,
            "content": content,
            "template": "html"
        }

        response = await client.post(
            PUSHPLUS_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10.0
        )
        result = response.json()

        if result.get("code") == 200:
            logger.info(f"PushPlus 推送成功: {title}")
            return True
        else:
            logger.error(f"PushPlus 推送失败: {result.get('msg', '未知错误')}")
            return False

    except Exception as e:
        logger.error(f"PushPlus 推送异常: {e}")
        return False


def can_send_alert(torrent_id: str, alert_type: str) -> bool:
    """
    检查是否可以发送报警（防止重复报警）

    Args:
        torrent_id: 种子ID
        alert_type: 报警类型 ('expiring' 或 'changed')

    Returns:
        bool: 是否可以发送
    """
    global sent_alerts

    alert_key = f"{torrent_id}_{alert_type}"
    now = datetime.now().timestamp()

    # 清理过期的报警记录
    expired_keys = [k for k, v in sent_alerts.items() if now - v > ALERT_COOLDOWN]
    for k in expired_keys:
        del sent_alerts[k]

    # 检查是否在冷却期内
    if alert_key in sent_alerts:
        return False

    # 记录本次报警
    sent_alerts[alert_key] = now
    return True


def is_free_discount(discount: Optional[str]) -> bool:
    """检查是否为免费优惠类型"""
    if not discount:
        return False
    return "FREE" in discount.upper()


async def check_emergency_alerts(torrents: List[Dict]) -> None:
    """
    检查紧急情况并发送报警

    情况 A：免费即将到期且未下载完（剩余时间 < 10 分钟）
    情况 B：免费突然失效且未下载完（变节检测）
    """
    global known_free_torrent_ids

    if not PUSHPLUS_TOKEN:
        return

    alerts_to_send = []

    # 第一步：更新历史免费记录
    for torrent in torrents:
        if is_free_discount(torrent.get("discount")):
            known_free_torrent_ids.add(torrent["id"])

    logger.debug(f"当前追踪的免费种子数量: {len(known_free_torrent_ids)}")

    # 第二步：检查下载中的种子是否有紧急情况
    for torrent_id, leeching_info in user_torrent_status.get("leeching", {}).items():
        # 获取下载进度
        try:
            peer_info = leeching_info.get("peer", {})
            torrent_data = leeching_info.get("torrent", {})
            downloaded = int(peer_info.get("downloaded", 0) or 0)
            total_size = int(torrent_data.get("size", 0) or 0)

            if total_size > 0:
                progress = min((downloaded / total_size) * 100, 100.0)
            else:
                progress = 0

            # 已完成下载的不需要报警
            if progress >= 100:
                continue

            torrent_name = torrent_data.get("name", "未知种子")
            status_info = torrent_data.get("status", {})
            current_discount = status_info.get("discount", "")
            discount_end_time_str = status_info.get("discountEndTime")

        except (ValueError, TypeError, KeyError) as e:
            logger.debug(f"解析种子 {torrent_id} 信息失败: {e}")
            continue

        # 情况 A：免费即将到期且未下载完
        if is_free_discount(current_discount) and discount_end_time_str:
            discount_end_time = parse_datetime(discount_end_time_str)
            if discount_end_time:
                remaining = calculate_remaining_time(discount_end_time)
                remaining_minutes = remaining["hours"] * 60

                if remaining_minutes < ALERT_THRESHOLD_MINUTES and remaining_minutes > 0:
                    if can_send_alert(torrent_id, "expiring"):
                        alerts_to_send.append({
                            "type": "expiring",
                            "title": "Mteam 做种预警",
                            "content": f"""
                                <h3>⚠️ 免费即将到期警告</h3>
                                <p><strong>种子名称:</strong> {torrent_name}</p>
                                <p><strong>剩余免费时间:</strong> <span style="color:red;">{remaining['display']}</span></p>
                                <p><strong>当前下载进度:</strong> <span style="color:orange;">{progress:.1f}%</span></p>
                                <p><strong>当前优惠:</strong> {current_discount}</p>
                                <hr>
                                <p style="color:red;"><strong>请注意！</strong>该种子还有不到 {ALERT_THRESHOLD_MINUTES} 分钟结束免费，但你只下载了 {progress:.1f}%！</p>
                            """
                        })

        # 情况 B：免费突然失效（变节检测）
        if not is_free_discount(current_discount) and torrent_id in known_free_torrent_ids:
            if can_send_alert(torrent_id, "changed"):
                alerts_to_send.append({
                    "type": "changed",
                    "title": "Mteam 做种预警",
                    "content": f"""
                        <h3>🚨 种子免费状态变更警告</h3>
                        <p><strong>种子名称:</strong> {torrent_name}</p>
                        <p><strong>当前状态:</strong> <span style="color:red;">非免费 ({current_discount or 'NORMAL'})</span></p>
                        <p><strong>当前下载进度:</strong> <span style="color:orange;">{progress:.1f}%</span></p>
                        <hr>
                        <p style="color:red;"><strong>警告！</strong>该种子已从免费变为非免费状态，且当前未完成下载，正在消耗上传量/下载量！</p>
                        <p>建议立即检查并决定是否继续下载。</p>
                    """
                })

    # 发送报警
    for alert in alerts_to_send:
        await send_pushplus_alert(alert["title"], alert["content"])
        await asyncio.sleep(1)  # 避免推送太快


async def toggle_collection(torrent_id: str, make: bool) -> Dict[str, Any]:
    """切换种子收藏状态"""
    if not MT_TOKEN:
        return {"success": False, "message": "未配置 MT_TOKEN"}

    try:
        client = await get_http_client()
        headers = {
            "User-Agent": USER_AGENT,
            "x-api-key": MT_TOKEN.strip(),
            "Accept": "application/json",
        }
        form_data = {"id": torrent_id, "make": "true" if make else "false"}
        response = await client.post(MT_COLLECTION_URL, headers=headers, data=form_data)
        data = response.json()

        if data.get("code") == "0":
            action = "收藏" if make else "取消收藏"
            logger.info(f"{action}种子 {torrent_id} 成功")
            return {"success": True, "message": f"{action}成功", "collected": make}
        else:
            return {"success": False, "message": data.get("message", "操作失败")}

    except Exception as e:
        logger.error(f"收藏操作失败: {e}")
        return {"success": False, "message": str(e)}


# ============ 数据处理 ============
def process_torrent(item: Dict, discount_type: str, torrent_mode: str = "normal") -> Dict:
    """处理单个种子数据"""
    torrent_info = item if "id" in item else item.get("torrent", item)
    status_info = torrent_info.get("status", {})

    torrent_id = str(torrent_info.get("id", ""))
    name = torrent_info.get("name", "未知")
    small_descr = torrent_info.get("smallDescr", "")
    size = int(torrent_info.get("size", 0))

    seeders = int(status_info.get("seeders", 0))
    leechers = int(status_info.get("leechers", 0))

    discount = status_info.get("discount", discount_type)
    discount_end_time = parse_datetime(status_info.get("discountEndTime"))
    remaining = calculate_remaining_time(discount_end_time)

    detail_url = f"{MT_SITE_URL}/detail/{torrent_id}"

    # 用户状态
    user_status = "none"
    user_progress = 0

    if torrent_id in user_torrent_status["seeding"]:
        user_status = "seeding"
    elif torrent_id in user_torrent_status["leeching"]:
        user_status = "leeching"
        leeching_info = user_torrent_status["leeching"][torrent_id]
        try:
            peer_info = leeching_info.get("peer", {})
            torrent_data = leeching_info.get("torrent", {})
            downloaded = int(peer_info.get("downloaded", 0) or 0)
            total_size = int(torrent_data.get("size", 0) or 0)
            if total_size > 0 and downloaded > 0:
                user_progress = min((downloaded / total_size) * 100, 100.0)
        except (ValueError, TypeError, KeyError):
            user_progress = 0

    return {
        "id": torrent_id,
        "name": name,
        "small_descr": small_descr,
        "size": size,
        "size_display": format_size(size),
        "seeders": seeders,
        "leechers": leechers,
        "discount": discount,
        "discount_label": get_discount_label(discount),
        "discount_end_time": status_info.get("discountEndTime"),
        "remaining": remaining,
        "category": torrent_info.get("category", ""),
        "category_name": torrent_info.get("categoryName", ""),
        "created_date": torrent_info.get("createdDate", ""),
        "detail_url": detail_url,
        "user_status": user_status,
        "user_progress": user_progress,
        "is_collected": torrent_id in user_collection_ids,
        "mode": torrent_mode
    }


async def fetch_all_free_torrents() -> Dict[str, Any]:
    """获取所有免费种子"""
    global cached_data

    if not MT_TOKEN:
        cached_data["error"] = "未配置 MT_TOKEN 环境变量"
        return cached_data

    logger.info("开始搜索免费种子")

    # 获取用户状态
    await fetch_user_torrent_status()
    await asyncio.sleep(API_DELAY)
    await fetch_user_collection()
    await asyncio.sleep(API_DELAY)
    await fetch_user_profile()
    await asyncio.sleep(API_DELAY)
    await fetch_rival_profile()

    all_torrents = []
    seen_ids = set()

    # 并行搜索普通区和成人区
    search_tasks = [
        ("FREE", "normal"),
        ("_2X_FREE", "normal"),
        ("FREE", "adult"),
        ("_2X_FREE", "adult"),
    ]

    for discount_type, mode in search_tasks:
        await asyncio.sleep(API_DELAY)
        torrents = await search_free_torrents(discount_type, mode=mode)
        for item in torrents:
            torrent = process_torrent(item, discount_type, mode)
            if torrent["id"] not in seen_ids:
                seen_ids.add(torrent["id"])
                all_torrents.append(torrent)

    # 按剩余时间排序
    all_torrents.sort(key=lambda t: t["remaining"]["hours"])

    # 获取类别列表
    categories = await fetch_categories()

    # 统计
    free_count = sum(1 for t in all_torrents if t["discount"] == "FREE")
    free_2x_count = sum(1 for t in all_torrents if t["discount"] == "_2X_FREE")

    cached_data = {
        "torrents": all_torrents,
        "categories": categories,
        "last_update": datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "error": None,
        "total": len(all_torrents),
        "free_count": free_count,
        "free_2x_count": free_2x_count
    }

    logger.info(f"找到 {len(all_torrents)} 个免费种子 (Free: {free_count}, 2xFree: {free_2x_count})")

    # 检查紧急报警（免费即将到期/免费变收费）
    if PUSHPLUS_TOKEN:
        await check_emergency_alerts(all_torrents)

    return cached_data


async def background_refresh():
    """后台定时刷新任务"""
    while True:
        await fetch_all_free_torrents()
        await asyncio.sleep(REFRESH_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global http_client
    http_client = httpx.AsyncClient(timeout=30.0)

    await fetch_all_free_torrents()
    task = asyncio.create_task(background_refresh())

    yield

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    if http_client:
        await http_client.aclose()


# ============ FastAPI 应用 ============
app = FastAPI(
    title="MT-Free-Hunter",
    description="M-Team 免费种子猎手",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,  # Disable Swagger UI in production
    redoc_url=None  # Disable ReDoc in production
)


# ============ Security Middleware ============
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """Add security headers to all responses"""
    response = await call_next(request)
    # Prevent clickjacking
    response.headers["X-Frame-Options"] = "DENY"
    # Prevent MIME sniffing
    response.headers["X-Content-Type-Options"] = "nosniff"
    # XSS Protection
    response.headers["X-XSS-Protection"] = "1; mode=block"
    # Referrer Policy
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # Content Security Policy
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "frame-ancestors 'none';"
    )
    return response


# ============ Rate Limiting (Simple In-Memory) ============
rate_limit_store: Dict[str, List[float]] = {}
RATE_LIMIT_REQUESTS = 30  # requests
RATE_LIMIT_WINDOW = 60  # seconds


def check_rate_limit(client_ip: str) -> bool:
    """Check if client has exceeded rate limit. Returns True if allowed."""
    now = datetime.now().timestamp()
    if client_ip not in rate_limit_store:
        rate_limit_store[client_ip] = []

    # Remove old entries
    rate_limit_store[client_ip] = [
        ts for ts in rate_limit_store[client_ip]
        if now - ts < RATE_LIMIT_WINDOW
    ]

    if len(rate_limit_store[client_ip]) >= RATE_LIMIT_REQUESTS:
        return False

    rate_limit_store[client_ip].append(now)
    return True

# 静态文件（如果存在）
try:
    app.mount("/static", StaticFiles(directory="app/static"), name="static")
except Exception:
    pass


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """主仪表盘页面"""
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "data": cached_data,
            "refresh_interval": REFRESH_INTERVAL,
            "site_url": MT_SITE_URL,
            "user_profile": user_profile,
            "rival_profile": rival_profile
        }
    )


@app.get("/api/torrents")
async def api_torrents(
    discount: Optional[str] = Query(None, description="筛选优惠类型: FREE, _2X_FREE"),
    min_size: Optional[int] = Query(None, description="最小大小(字节)"),
    max_size: Optional[int] = Query(None, description="最大大小(字节)"),
    category: Optional[str] = Query(None, description="类别ID"),
    mode: Optional[str] = Query(None, description="频道: normal, adult")
):
    """API 接口返回 JSON 数据，支持筛选"""
    torrents = cached_data.get("torrents", [])

    if discount:
        torrents = [t for t in torrents if t["discount"] == discount]
    if min_size is not None:
        torrents = [t for t in torrents if t["size"] >= min_size]
    if max_size is not None:
        torrents = [t for t in torrents if t["size"] <= max_size]
    if category:
        torrents = [t for t in torrents if str(t["category"]) == category]
    if mode:
        torrents = [t for t in torrents if t["mode"] == mode]

    return {
        **cached_data,
        "torrents": torrents,
        "filtered_count": len(torrents)
    }


@app.post("/api/refresh")
async def api_refresh(request: Request):
    """手动触发刷新"""
    # Rate limiting
    client_ip = request.client.host if request.client else "unknown"
    if not check_rate_limit(client_ip):
        raise HTTPException(status_code=429, detail="Too many requests. Please wait.")

    await fetch_all_free_torrents()
    return {"status": "ok", "message": "刷新完成"}


@app.post("/api/collection")
async def api_collection(request: Request, data: CollectionRequest):
    """收藏/取消收藏种子"""
    # Rate limiting
    client_ip = request.client.host if request.client else "unknown"
    if not check_rate_limit(client_ip):
        raise HTTPException(status_code=429, detail="Too many requests. Please wait.")

    return await toggle_collection(data.id, data.make)


@app.get("/api/categories")
async def api_categories():
    """获取类别列表"""
    return {"categories": cached_data.get("categories", [])}


@app.get("/health")
async def health_check():
    """健康检查接口"""
    return {
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "torrents_count": cached_data.get("total", 0)
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5001)
