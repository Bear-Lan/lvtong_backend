"""LED 显示屏控制器

通过串口/HTTP 控制 LED 显示内容。
参考 Qt Led (device/led.h)。

LED 步骤显示内容：
1 - 绿通车辆 按键检测
2 - 已接收 请原地等待
3 - 已受理 请等待放行
4 - 待检车辆 请通行 勿停车倒车
5 - 检测中 请勿跟车
"""
import requests


class LedController:
    """LED 显示屏 HTTP 接口封装"""

    STEPS = {
        1: '绿通车辆 按键检测',
        2: '已接收 请原地等待',
        3: '已受理 请等待放行',
        4: '待检车辆 请通行 勿停车倒车',
        5: '检测中 请勿跟车',
    }

    def __init__(self, base_url: str = ''):
        self.base_url = base_url
        self._current_step = 1

    @property
    def current_step(self) -> int:
        return self._current_step

    def set_step(self, step: int) -> bool:
        """设置 LED 显示步骤

        参考 Qt Led::setStep1() ~ setStep5()
        """
        if step not in self.STEPS:
            return False

        message = self.STEPS[step]
        if not self.base_url:
            print(f'[LED] 模拟 setStep{step}: {message}')
            self._current_step = step
            return True

        try:
            resp = requests.post(
                f'{self.base_url}/set',
                json={'step': step, 'message': message},
                timeout=5
            )
            if resp.status_code == 200:
                self._current_step = step
                return True
        except requests.RequestException as e:
            print(f'[LED] setStep 失败: {e}')

        return False

    def set_step1(self) -> bool:
        return self.set_step(1)

    def set_step2(self) -> bool:
        return self.set_step(2)

    def set_step3(self) -> bool:
        return self.set_step(3)

    def set_step4(self) -> bool:
        return self.set_step(4)

    def set_step5(self) -> bool:
        return self.set_step(5)
