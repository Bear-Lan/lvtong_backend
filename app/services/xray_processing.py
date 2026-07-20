"""X光图像处理服务

从 Qt ImgProcess + Levels 移植。

功能：
1. LevelImg   — 色阶校正（黑场/亮场裁剪 + Gamma 校正）
2. 满载率计算   — ComputeXRayImgLoadRateType045 / Type123
3. 200+160拼接 — stitch 参数控制

Qt 参考文件:
- cvimgproc/ImgProcess.cpp
- utils/levels.cpp
"""
import math
import cv2
import numpy as np
from io import BytesIO


class XRayProcessor:
    """X光图像处理器"""

    # ==================== 色阶校正 ====================

    @staticmethod
    def generate_lut(black_level: int = 0, gamma: float = 1.0, white_level: int = 255) -> np.ndarray:
        """生成色阶查找表 (LUT)

        对齐 Qt Levels::generateLUT():

        对每个像素值 i (0-255):
          1. 黑场/亮场裁剪: value = 255 * (i - black) / (white - black)
          2. Gamma 校正: value = 255 * (value/255)^(1/gamma)

        Args:
            black_level: 黑场值 (0-254), 低于此值变纯黑
            gamma: Gamma 校正值 (0.1-5.0), >1 提亮暗部, <1 压暗亮部
            white_level: 亮场值 (1-255), 高于此值变纯白

        Returns:
            256元素的 uint8 LUT 数组
        """
        # 参数范围约束（对齐 Qt setLevels）
        black_level = max(0, min(254, black_level))
        gamma = max(0.1, min(5.0, gamma))
        white_level = max(1, min(255, white_level))
        if white_level <= black_level:
            white_level = black_level + 1

        lut = np.zeros(256, dtype=np.uint8)

        for i in range(256):
            value = float(i)

            # 步骤1: 黑场/亮场裁剪后线性映射到 0-255
            if value <= black_level:
                value = 0.0
            elif value >= white_level:
                value = 255.0
            else:
                value = 255.0 * (value - black_level) / (white_level - black_level)

            # 步骤2: Gamma 校正
            if gamma != 1.0:
                value = 255.0 * math.pow(value / 255.0, 1.0 / gamma)

            # 步骤3: 钳位到 0-255
            value = max(0.0, min(255.0, value))
            lut[i] = int(value + 0.5)

        return lut

    @classmethod
    def level_img(cls, image: np.ndarray, gamma: float = 2.0,
                  white_level: int = 128, black_level: int = 0) -> np.ndarray:
        """色阶校正

        对齐 Qt ImgProcess::LevelImg() → Levels::adjust()

        Qt 默认参数: blackLevel=0, gamma=2.0, whiteLevel=128

        Args:
            image: 输入图像 (BGR or grayscale)
            gamma: Gamma 校正值 (默认 2.0)
            white_level: 亮场值 (默认 128)
            black_level: 黑场值 (默认 0)

        Returns:
            色阶校正后的图像
        """
        if image is None or image.size == 0:
            return image

        lut = cls.generate_lut(black_level, gamma, white_level)
        return cv2.LUT(image, lut)

    # ==================== 满载率计算 ====================

    @staticmethod
    def _compute_load_rate_raw(image: np.ndarray, threshold: int = 100) -> float:
        """基础满载率：暗像素占比

        对齐 Qt ImgProcess::ComputeXRayImgLoadRate(srcImg, nThreshHold)

        X光图像中，货物区域显示为深色（灰度低），空区域为浅色（灰度高）。
        遍历所有像素，统计灰度值 < threshold 的像素占比。

        Args:
            image: BGR 或灰度图像
            threshold: 灰度阈值 (默认 100/255, 低于此值为"有货")

        Returns:
            满载率 (0.0-1.0)
        """
        if image is None or image.size == 0:
            return 0.0

        if len(image.shape) == 3:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        else:
            gray = image

        total = gray.size
        load_count = int(np.sum(gray < threshold))

        return load_count / total if total > 0 else 0.0

    @staticmethod
    def _get_top_offset(image: np.ndarray) -> int:
        """获取顶部空白偏移量

        对齐 Qt ImgProcess::GetTopOffset()

        从上往下逐行扫描，当该行暗像素 (<128) 占比 >= 5% 时停止，
        返回该行位置作为有效内容的起始位置。
        """
        height, width = image.shape[:2]
        if len(image.shape) == 3:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        else:
            gray = image

        threshold = 128
        for row in range(height):
            dark_count = int(np.sum(gray[row, :] < threshold))
            ratio = dark_count / width
            if ratio >= 0.05:
                return row
        return 0

    @staticmethod
    def _get_left_offset(image: np.ndarray) -> int:
        """获取左边空白偏移量

        对齐 Qt ImgProcess::GetLeftOffset()
        旋转90度后用 GetTopOffset 计算
        """
        rotated = cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
        return XRayProcessor._get_top_offset(rotated)

    @staticmethod
    def _get_right_offset(image: np.ndarray) -> int:
        """获取右边空白偏移量

        对齐 Qt ImgProcess::GetRightOffset()
        旋转270度后用 GetTopOffset 计算
        """
        rotated = cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
        return XRayProcessor._get_top_offset(rotated)

    @classmethod
    def _get_car_container_part(cls, image: np.ndarray) -> np.ndarray:
        """提取车厢部分，去除车厢外空白区域

        对齐 Qt ImgProcess::GetCarContainPart()
        """
        height, width = image.shape[:2]

        top = cls._get_top_offset(image)
        left = cls._get_left_offset(image)
        right = cls._get_right_offset(image)

        return image[top:height, left:width - right]

    @classmethod
    def compute_load_rate_type045(cls, image: np.ndarray) -> float:
        """满载率计算 — 厢式/罐式/特殊结构货车 (containerType 0,4,5)

        对齐 Qt ImgProcess::ComputeXRayImgLoadRateType045()

        算法: 装载率 = 底部130px满载率*20% + 中间部分满载率*80%
        车厢轮廓顶部去掉25%，去掉底部130px。

        适用于:
          - 0: 厢式货车
          - 4: 罐式货车
          - 5: 特殊结构货车(水箱式)
        """
        contain = cls._get_car_container_part(image)
        h, w = contain.shape[:2]

        # 底部 130px
        bottom_h = min(130, h)
        bottom_part = contain[h - bottom_h:h, :]
        bottom_rate = cls._compute_load_rate_raw(bottom_part)

        # 中间部分: 30% 到 (100% - 底部)
        center_top = int(0.3 * h)
        center_bottom = h - bottom_h
        if center_bottom <= center_top:
            center_bottom = center_top + 1
        center_part = contain[center_top:center_bottom, :]
        center_rate = cls._compute_load_rate_raw(center_part)

        # 底部占20%, 中间占80%
        return bottom_rate * 0.2 + center_rate * 0.8

    @classmethod
    def compute_load_rate_type123(cls, image: np.ndarray) -> float:
        """满载率计算 — 栏板/平板货车 (containerType 1,2,3)

        对齐 Qt ImgProcess::ComputeXRayImgLoadRateType123()

        算法: 装载率 = 底部130px满载率*80% + 中间部分满载率*20%

        适用于:
          - 1: 栏板货车
          - 2: 平板货车
          - 3: 高栏货车
        """
        contain = cls._get_car_container_part(image)
        h, w = contain.shape[:2]

        # 底部 130px
        bottom_h = min(130, h)
        bottom_part = contain[h - bottom_h:h, :]
        bottom_rate = cls._compute_load_rate_raw(bottom_part)

        # 中间部分: 30% 到 (100% - 底部)
        center_top = int(0.3 * h)
        center_bottom = h - bottom_h
        if center_bottom <= center_top:
            center_bottom = center_top + 1
        center_part = contain[center_top:center_bottom, :]
        center_rate = cls._compute_load_rate_raw(center_part)

        # 底部占80%, 中间占20%
        return bottom_rate * 0.8 + center_rate * 0.2

    @classmethod
    def compute_load_rate(cls, image: np.ndarray, container_type_index: int = 0) -> float:
        """根据货箱类型计算满载率

        对齐 Qt 中根据货箱类型分派不同算法的逻辑:

        - containerType 0,4,5 → Type045 (厢式/罐式/特殊)
        - containerType 1,2,3 → Type123 (栏板/平板/高栏)

        Args:
            image: X光透视图像
            container_type_index: 货箱类型索引 (0-5)

        Returns:
            满载率 (0.0-1.0)
        """
        if container_type_index in (0, 4, 5):
            return cls.compute_load_rate_type045(image)
        else:
            return cls.compute_load_rate_type123(image)

    # ==================== 图像拼接 ====================

    @staticmethod
    def stitch_xray_images(image200: np.ndarray, image160: np.ndarray,
                           gamma200: float = 2.0, white200: int = 128,
                           gamma160: float = 1.0, white160: int = 255,
                           stitch_gamma: float = 1.0, stitch_white: int = 255) -> np.ndarray:
        """拼接 200kV 和 160kV 的 X 光透视图像

        对齐 Qt 中的拼接逻辑 (LvTongPro.cpp 中 stitchTransparentImageFetcher 相关):

        1. 200图用参数(gamma=2.0, white=128)做色阶
        2. 160图用参数(gamma=1.0, white=255)做色阶
        3. 拼接图不做色阶(gamma=1.0, white=255)

        Qt 中拼接图是通过外部 HTTP 接口获取的（stitch URL），
        此函数用于本地处理场景。

        Args:
            image200: 200kV 图像
            image160: 160kV 图像
            gamma200: 200kV 图色阶 Gamma
            white200: 200kV 图色阶白场
            gamma160: 160kV 图色阶 Gamma
            white160: 160kV 图色阶白场
            stitch_gamma: 拼接后整体色阶 Gamma
            stitch_white: 拼接后整体色阶白场

        Returns:
            垂直拼接后的图像
        """
        # 处理200kV图
        proc200 = XRayProcessor.level_img(image200, gamma200, white200)

        # 处理160kV图
        proc160 = XRayProcessor.level_img(image160, gamma160, white160)

        # 垂直拼接（宽取最小值，Resize对齐）
        w = min(proc200.shape[1], proc160.shape[1])
        proc200_resized = cv2.resize(proc200, (w, int(proc200.shape[0] * w / proc200.shape[1])))
        proc160_resized = cv2.resize(proc160, (w, int(proc160.shape[0] * w / proc160.shape[1])))

        stitched = np.vstack([proc200_resized, proc160_resized])

        # 拼接后整体色阶
        stitched = XRayProcessor.level_img(stitched, stitch_gamma, stitch_white)

        return stitched

    # ==================== 图像编解码工具 ====================

    @staticmethod
    def decode_image(data: bytes) -> np.ndarray | None:
        """从字节流解码图像"""
        arr = np.frombuffer(data, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        return img if img is not None and img.size > 0 else None

    @staticmethod
    def encode_image(image: np.ndarray, fmt: str = '.png') -> bytes:
        """将图像编码为字节流"""
        success, buf = cv2.imencode(fmt, image)
        if not success:
            raise ValueError('图像编码失败')
        return buf.tobytes()
