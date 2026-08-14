"""Vercel serverless 入口。

Vercel 的 Python runtime 会在本文件里找名为 `app` 的 WSGI 应用，
所以这里只做路径挂载 + 转出根目录的 Flask 实例。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402

__all__ = ["app"]
