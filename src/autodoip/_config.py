"""
@文件: _config.py
@作者: 雷小鸥
@日期: 2026/6/3
@许可: MIT License
@描述: DoIP 传输调优配置 — 仅含行为参数，不含身份参数（ip/port/tester/ecus）
"""
from dataclasses import dataclass
from typing import Literal


@dataclass
class Config:
    """DoIP 传输层调优参数。全部有默认值，传入则覆盖。"""
    accept_timeout: float = 1.5
    p6_timeout: float = 0.05
    p6_star_timeout: float = 5.0
    listen_count: int = 5
    version: int = 0x02
    msg_type: int = 0x8001
    byte_order: Literal['little', 'big'] = 'big'
