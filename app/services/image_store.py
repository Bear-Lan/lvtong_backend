"""图像存储服务

集中管理检测流程中所有图像的本地存储和路径转换。
对齐 Qt ImageStorageHelper。
"""
import os
import threading
from datetime import datetime
from typing import Optional

from config import IMAGE_STORAGE_ROOT


class ImageStore:
    """图像存储服务（单例，线程安全）"""

    _instance: Optional['ImageStore'] = None
    _lock = threading.Lock()

    def __new__(cls) -> 'ImageStore':
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._write_lock = threading.Lock()
        # 确保根目录存在
        os.makedirs(IMAGE_STORAGE_ROOT, exist_ok=True)

    # ---------- 路径工具 ----------

    @staticmethod
    def _category_dir(category: str) -> str:
        """生成分类子目录路径: {root}/{yyyy}/{MM}/{dd}/{category}/"""
        now = datetime.now()
        path = os.path.join(
            IMAGE_STORAGE_ROOT,
            now.strftime('%Y'), now.strftime('%m'), now.strftime('%d'),
            category,
        )
        os.makedirs(path, exist_ok=True)
        return path

    @staticmethod
    def _generate_filename(category: str, prefix: str = '', ext: str = 'jpg') -> str:
        """生成唯一文件名: yyyyMMdd_HHmmss_{prefix}_{seq}.{ext}"""
        now = datetime.now()
        ts = now.strftime('%Y%m%d_%H%M%S')
        seq = str(now.microsecond)[:5]  # 用微秒作序列号
        parts = [ts]
        if prefix:
            parts.append(prefix)
        parts.append(seq)
        return f"{'_'.join(parts)}.{ext}"

    # ---------- 读写 ----------

    def save_image_bytes(self, data: bytes, category: str,
                         prefix: str = '', ext: str = 'jpg') -> str:
        """保存图像二进制数据，返回 API 可访问的相对路径。

        Returns:
            API 相对路径，如 /api/images/2026/07/21/head/20260721_141530_00123.jpg
        """
        directory = self._category_dir(category)
        filename = self._generate_filename(category, prefix, ext)
        abs_path = os.path.join(directory, filename)

        with self._write_lock:
            with open(abs_path, 'wb') as f:
                f.write(data)

        return self.absolute_to_api_path(abs_path)

    def save_image_file(self, source_path: str, category: str,
                        prefix: str = '') -> str:
        """从本地文件复制到存储目录，返回 API 路径。"""
        directory = self._category_dir(category)
        ext = os.path.splitext(source_path)[1].lstrip('.') or 'jpg'
        filename = self._generate_filename(category, prefix, ext)
        abs_path = os.path.join(directory, filename)

        with self._write_lock:
            with open(source_path, 'rb') as src:
                with open(abs_path, 'wb') as dst:
                    dst.write(src.read())

        return self.absolute_to_api_path(abs_path)

    # ---------- 路径转换 ----------

    @staticmethod
    def absolute_to_api_path(abs_path: str) -> str:
        """Windows 绝对路径 → API URL 相对路径。

        D:/LvTongFiles/Images/captures/2026/07/21/head/xxx.jpg
        → /api/images/2026/07/21/head/xxx.jpg
        """
        norm = os.path.normpath(abs_path).replace('\\', '/')
        root = os.path.normpath(IMAGE_STORAGE_ROOT).replace('\\', '/')
        if norm.startswith(root):
            rel = norm[len(root):].lstrip('/')
            return f'/api/images/{rel}'
        # 不是存储根目录下的路径，返回原样
        return norm

    @staticmethod
    def api_path_to_absolute(api_path: str) -> str:
        """API URL 路径 → Windows 绝对路径。

        /api/images/2026/07/21/head/xxx.jpg
        → D:/LvTongFiles/Images/captures/2026/07/21/head/xxx.jpg
        """
        prefix = '/api/images/'
        if api_path.startswith(prefix):
            rel = api_path[len(prefix):]
            return os.path.normpath(os.path.join(IMAGE_STORAGE_ROOT, rel))
        return api_path

    @staticmethod
    def is_stored_path(abs_path: str) -> bool:
        """判断是否为存储根目录下的路径"""
        norm = os.path.normpath(abs_path).replace('\\', '/')
        root = os.path.normpath(IMAGE_STORAGE_ROOT).replace('\\', '/')
        return norm.startswith(root)


# 便捷函数
def save_image(data: bytes, category: str, prefix: str = '') -> str:
    return ImageStore().save_image_bytes(data, category, prefix)


def db_path_to_api(db_path: str) -> str:
    """数据库存储路径 → API URL 路径"""
    if not db_path:
        return ''
    store = ImageStore()
    if store.is_stored_path(db_path):
        return store.absolute_to_api_path(db_path)
    return db_path
