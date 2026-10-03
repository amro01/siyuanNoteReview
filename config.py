# -*- coding: utf-8 -*-
"""
错题拼卷机 —— 配置模块（步骤1 抽取）

职责：
  1. 定义 Config 数据类，集中承载所有运行期配置项；
  2. 从 config.json 读取并校验，返回 Config 实例；
  3. 以单例方式提供配置，替代原先散落在 siyuan_client.py 中的模块级全局变量。

设计约束（重要）：
  - 本模块在 import 时**不读文件、不访问网络、不产生任何副作用**；
  - 配置的加载由调用方显式触发（当前为 main() 中的 load_config()），
    从而消除原 siyuan_client.py 第 362 行「导入即加载配置」带来的循环导入隐患。
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional

# ============================================================
#  非 config.json 驱动的固定默认常量
# ============================================================
DEFAULT_SIYUAN_HOST_MODE = "auto"
DEFAULT_CONNECT_TIMEOUT = 3.0     # TCP 建连超时（秒）
DEFAULT_READ_TIMEOUT = 30.0       # 响应读取超时（秒）
DEFAULT_CONNECT_RETRIES = 2       # 建连失败后的额外重试次数
DEFAULT_RETRY_BACKOFF = 0.5       # 重试退避基数（秒）：等待 = 基数 * 2^attempt
DEFAULT_PROBE_TIMEOUT = 1.5       # 单候选地址 TCP 预检超时（秒）
DEFAULT_MAX_PAGES = 10
DEFAULT_EXCLUDE_RECENT_DAYS = 2


@dataclass
class Config:
    """错题拼卷机的全部运行期配置。"""

    siyuan_url: str
    api_token: str
    notebook_id: str
    siyuan_data_path: str
    target_folders: List[str]
    score_threshold: int
    min_pages: int
    max_pages: int = DEFAULT_MAX_PAGES
    exclude_recent_days: int = DEFAULT_EXCLUDE_RECENT_DAYS
    siyuan_host_mode: str = DEFAULT_SIYUAN_HOST_MODE
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    read_timeout: float = DEFAULT_READ_TIMEOUT
    connect_retries: int = DEFAULT_CONNECT_RETRIES
    retry_backoff: float = DEFAULT_RETRY_BACKOFF
    probe_timeout: float = DEFAULT_PROBE_TIMEOUT

    @property
    def headers(self) -> Dict[str, str]:
        """思源 API 请求头（由 API_TOKEN 派生）。"""
        return {
            "Authorization": f"Token {self.api_token}",
            "Content-Type": "application/json",
        }


def load_config(config_path: str = "config.json") -> Config:
    """
    从 config.json 读取并校验配置，返回 Config 实例。

    与旧实现的区别：本函数**只返回值**，不再向任何模块写入全局变量，
    也不在导入期被调用。文件缺失时保持原有行为（提示并退出）。

    兼容旧配置文件：MAX_PAGES / EXCLUDE_RECENT_DAYS / 网络相关项缺失时使用默认值。
    """
    if not os.path.isfile(config_path):
        print(f"❌ 未找到配置文件 {config_path}")
        print("   请复制 config.json.example 为 config.json，并填写你的实际配置。")
        print("   参考命令: cp config.json.example config.json")
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    # auto : 依次探测 [配置地址, 127.0.0.1, localhost, 默认网关]，自动适配 NAT / mirrored 模式
    # fixed: 仅使用 SIYUAN_URL 中的地址，不做自动探测
    host_mode = str(cfg.get("SIYUAN_HOST_MODE", DEFAULT_SIYUAN_HOST_MODE)).strip().lower()
    if host_mode not in ("auto", "fixed"):
        print(f"⚠️  未知的 SIYUAN_HOST_MODE={host_mode!r}，已回退为 auto")
        host_mode = "auto"

    return Config(
        siyuan_url=cfg["SIYUAN_URL"],
        api_token=cfg["API_TOKEN"],
        notebook_id=cfg["NOTEBOOK_ID"],
        siyuan_data_path=cfg["SIYUAN_DATA_PATH"],
        target_folders=cfg["TARGET_FOLDERS"],
        score_threshold=cfg["SCORE_THRESHOLD"],
        min_pages=cfg["MIN_PAGES"],
        max_pages=cfg.get("MAX_PAGES", DEFAULT_MAX_PAGES),
        exclude_recent_days=cfg.get("EXCLUDE_RECENT_DAYS", DEFAULT_EXCLUDE_RECENT_DAYS),
        siyuan_host_mode=host_mode,
        connect_timeout=float(cfg.get("CONNECT_TIMEOUT", DEFAULT_CONNECT_TIMEOUT)),
        read_timeout=float(cfg.get("READ_TIMEOUT", DEFAULT_READ_TIMEOUT)),
        connect_retries=int(cfg.get("CONNECT_RETRIES", DEFAULT_CONNECT_RETRIES)),
        retry_backoff=float(cfg.get("RETRY_BACKOFF", DEFAULT_RETRY_BACKOFF)),
        probe_timeout=DEFAULT_PROBE_TIMEOUT,
    )


# ============================================================
#  配置单例访问（显式触发，非导入期）
# ============================================================
_CONFIG: Optional[Config] = None


def set_config(cfg: Config) -> Config:
    """显式设置配置单例（供 shim / 测试注入）。"""
    global _CONFIG
    _CONFIG = cfg
    return _CONFIG


def get_config(config_path: str = "config.json") -> Config:
    """获取配置单例；若尚未加载则按需加载（由调用方显式触发）。"""
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_config(config_path)
    return _CONFIG


def reset_config() -> None:
    """清空配置单例（供测试或配置重载使用）。"""
    global _CONFIG
    _CONFIG = None
