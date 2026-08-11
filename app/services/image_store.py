"""图像存储服务

集中管理检测流程中所有图像的本地存储和路径转换。
对齐 Qt ImageStorageHelper。

# 写盘规则（与 Qt 对齐）
- 入库形态：磁盘绝对路径（Windows：`D:/...`）。
- 新采集图保存到 IMAGE_STORAGE_ROOT/<yyyy>/<MM>/<dd>/<category>/<file>。
- 提交时由 persist_to_storage() 把"任何来源的图片引用"统一收口为绝对路径；
  不在 IMAGE_STORAGE_ROOT 下的图（如 mobile 上传到 MOCK_PHOTO_DIR）会被复制进根目录。
- 前端展示 / API 返回：/api/images/<relative>，通过 storage_path_to_api() 转换。
"""
import os
import shutil
import threading
from datetime import datetime
from typing import Optional
from urllib.parse import unquote, urlparse

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
        # 不是存储根目录下的路径，返回原样（采集时使用）
        return norm

    # ---------- 提交时持久化（核心） ----------

    @staticmethod
    def persist_to_storage(reference: str, category: str = 'misc') -> str:
        """把"任何形态的图片引用"归一为磁盘绝对路径。

        接收形态：
          - 空串                         -> ''（不入库字段）
          - data:image/...;base64,...    -> 解码 + 写盘，返回新绝对路径
          - '/api/temp-images/<id>'      -> 从内存临时缓存取字节写盘（预览图提交落库）
          - 任意磁盘上的图片（含已经在 IMAGE_STORAGE_ROOT 内的）
                                        -> **统一**复制并重命名为
                                            IMAGE_STORAGE_ROOT/<yyyy>/<MM>/<dd>/<category>/<seq>.<ext>
          - 相对路径（不含盘符 / 不以 / 开头）
                                        -> 拼到 IMAGE_STORAGE_ROOT 之下作为 source；找不到则不入库
          - '/api/images/<rel>'          -> 抽 rel，作为相对路径处理
          - '/api/image?path=<encoded>'  -> 解码、抽 path 作为磁盘文件处理
          - 'http(s)://...'              -> 原样返回（外部链接，不复制）

        **强制规则**：所有入库的图片必须先复制到 IMAGE_STORAGE_ROOT 下的规范目录
                    (`<yyyy>/<MM>/<dd>/<category>/<seq>.<ext>`)，不入库调用者给的原路径。
                    原文件保留不动（不删除）。

        Returns:
            绝对路径字符串（规范目录下的新路径），找不到/外部链接则返回原串或空串。
        """
        if not reference or not isinstance(reference, str):
            return ''
        s = reference.strip()
        if not s:
            return ''

        # 1. dataUrl 形态：解码 + 写盘（已自行落到规范目录）
        if s.startswith('data:image/') or s.startswith('data:application/octet-stream;base64,'):
            return ImageStore._save_data_url(s, category)

        # 1b. 内存临时预览图：提交时才写入正式库
        if s.startswith('/api/temp-images/'):
            return ImageStore._save_temp_image(s[len('/api/temp-images/'):].strip('/'), category)

        # 2. http(s) 外部链接：不接管
        lower = s.lower()
        if lower.startswith('http://') or lower.startswith('https://'):
            return s

        # 3. 解 URL 形态（剥 /api/image?path=<encoded> / /api/images/<rel>）
        rel_or_path: Optional[str] = None
        if s.startswith('/api/images/'):
            rel_or_path = s[len('/api/images/'):]
        elif s.startswith('/api/image'):
            qs = urlparse(s).query
            if qs:
                from urllib.parse import parse_qs
                params = parse_qs(qs)
                p = params.get('path', [''])[0]
                rel_or_path = unquote(p) if p else None
            else:
                rel_or_path = None

        norm_root = os.path.normpath(IMAGE_STORAGE_ROOT)

        if rel_or_path is not None:
            if not rel_or_path:
                return ''
            # 解出的 rel_or_path 可能是 absolute path（来自 URL encode 的 abs）或 root-relative
            if os.path.isabs(rel_or_path):
                src = os.path.normpath(rel_or_path)
            else:
                src = os.path.normpath(os.path.join(norm_root, rel_or_path))
            return ImageStore._copy_to_standard(src, category)

        # 4. 相对路径：拼到 IMAGE_STORAGE_ROOT 作为 source；**统一复制**
        if not os.path.isabs(s):
            src = os.path.normpath(os.path.join(norm_root, s))
            return ImageStore._copy_to_standard(src, category)

        # 5. 已是绝对路径：统一复制
        return ImageStore._copy_to_standard(os.path.normpath(s), category)

    @staticmethod
    def _save_bytes(blob: bytes, category: str, prefix: str = 'dataurl',
                    mime: str = 'image/jpeg') -> str:
        """字节写盘到 IMAGE_STORAGE_ROOT，返回绝对路径。"""
        if not blob:
            return ''
        ext_map = {
            'image/jpeg': 'jpg', 'image/jpg': 'jpg', 'image/png': 'png',
            'image/gif': 'gif', 'image/bmp': 'bmp', 'image/webp': 'webp',
            'image/tiff': 'tiff',
        }
        ext = ext_map.get((mime or '').strip().lower(), 'jpg')
        inst = ImageStore()
        directory = inst._category_dir(category)
        filename = inst._generate_filename(category, prefix=prefix, ext=ext)
        abs_path = os.path.join(directory, filename)
        with inst._write_lock:
            with open(abs_path, 'wb') as f:
                f.write(blob)
        return abs_path

    @staticmethod
    def _save_temp_image(temp_id: str, category: str) -> str:
        """从内存临时缓存取图并写入正式库。"""
        from app.services.temp_image_cache import get as temp_get
        entry = temp_get(temp_id)
        if entry is None:
            return ''
        return ImageStore._save_bytes(
            entry.data, category, prefix='temp', mime=entry.mime,
        )

    @staticmethod
    def _save_data_url(data_url: str, category: str) -> str:
        """把 base64 dataUrl 解码写盘到 IMAGE_STORAGE_ROOT，返回绝对路径。"""
        import base64
        import re
        m = re.match(r'^data:(?P<mime>[^;,]+)(;base64)?,(?P<data>.+)$', data_url, re.DOTALL)
        if not m:
            return ''
        mime = m.group('mime').strip().lower()
        raw = m.group('data').strip()
        try:
            blob = base64.b64decode(raw, validate=False)
        except Exception:
            return ''
        return ImageStore._save_bytes(blob, category, prefix='dataurl', mime=mime)

    @staticmethod
    def _copy_to_standard(source_abs: str, category: str) -> str:
        """强制把 source_abs 指向的图片复制到规范目录，
        IMAGE_STORAGE_ROOT/<yyyy>/<MM>/<dd>/<category>/<新文件名>.<原 ext>，
        然后返回新绝对路径。

        强制规则（与产品要求对齐）：
          - 不管 source 在哪（根目录内/外、任意绝对路径）都必须复制重命名
          - 原文件保留不动
          - 若 source 不存在，返回空串（让上层报缺图）
        """
        norm_root = os.path.normpath(IMAGE_STORAGE_ROOT)
        src = os.path.normpath(source_abs)

        if not src or not os.path.isfile(src):
            return ''

        ts = datetime.now()
        category_dir = os.path.join(
            norm_root,
            ts.strftime('%Y'), ts.strftime('%m'), ts.strftime('%d'),
            category,
        )
        os.makedirs(category_dir, exist_ok=True)
        ext = os.path.splitext(src)[1] or '.jpg'
        new_name = ImageStore._generate_filename(category, prefix='copied', ext=ext.lstrip('.'))
        dst = os.path.join(category_dir, new_name)
        shutil.copy2(src, dst)
        return dst


# 便捷函数
def save_image(data: bytes, category: str, prefix: str = '') -> str:
    return ImageStore().save_image_bytes(data, category, prefix)


def persist_to_storage(reference: str, category: str = 'misc') -> str:
    """便捷函数：归一图片引用为磁盘绝对路径。"""
    return ImageStore.persist_to_storage(reference, category)


def storage_path_to_api(abs_path: str) -> str:
    """磁盘绝对路径 → /api/images/<rel> URL。

    D:/code_product/gcms_src/captures/2026/07/29/head/xxx.jpg
    → /api/images/2026/07/29/head/xxx.jpg

    非根目录内路径原样返回（外部链接 / 不存在路径）。
    """
    if not abs_path:
        return ''
    return ImageStore.absolute_to_api_path(abs_path)


def db_path_to_api(db_path: str) -> str:
    """数据库存储路径 → API URL（新版：/api/images/<rel>）。

    自动识别形态：
      - 以 /api/images/ 开头：原样
      - 空 / 已是 /api/images/... 形式：保留
      - 绝对路径在 IMAGE_STORAGE_ROOT 内：转 /api/images/<rel>
      - 绝对路径不在根目录：保持空（旧接口兜底，等待下次写入被规范化）

    Returns:
        如不需要对外暴露，返回空串即可。
    """
    if not db_path:
        return ''
    if db_path.startswith('/api/images/') or db_path.startswith('/api/image?path='):
        return db_path
    return storage_path_to_api(db_path)
