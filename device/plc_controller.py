"""PLC 控制器

通过 HTTP 协议控制 PLC 设备的红黄绿灯、光闸、补光灯。
参考 Qt PLCModbus::executeAction()。

PLC 控制参数：
- redlight / yellowlight / greenlight: 红/黄/绿灯
- greatlight: 补光灯
- lightgate160 / lightgate200: 160/200kV 光闸
- urgentstop: 急停
"""
import requests


class PLCController:
    """PLC 设备 HTTP 接口封装"""

    def __init__(self, base_url: str = ''):
        self.base_url = base_url
        self._status = {
            'redlight': False,
            'yellowlight': False,
            'greenlight': False,
            'greatlight': False,
            'lightgate160': False,
            'lightgate200': False,
            'urgentstop': False,
        }

    @property
    def status(self) -> dict:
        return dict(self._status)

    def set_plc(self, red: bool = False, yellow: bool = False, green: bool = False,
                greatlight: bool = False, lightgate160: bool = False,
                lightgate200: bool = False, urgentstop: bool = False) -> bool:
        """设置 PLC 各通道状态

        参考 Qt PLCModbus::executeAction("setPLC", params)
        """
        params = {
            'redlight': red,
            'yellowlight': yellow,
            'greenlight': green,
            'greatlight': greatlight,
            'lightgate160': lightgate160,
            'lightgate200': lightgate200,
        }
        if urgentstop is not None:
            params['urgentstop'] = urgentstop

        if not self.base_url:
            print(f'[PLC] 模拟 setPLC: {params}')
            self._status.update(params)
            return True

        try:
            resp = requests.post(
                f'{self.base_url}/setPLC',
                json=params,
                timeout=5
            )
            if resp.status_code == 200:
                self._status.update(params)
                return True
        except requests.RequestException as e:
            print(f'[PLC] setPLC 失败: {e}')

        return False

    def execute_action(self, action: str, params: dict = None) -> bool:
        """通用设备操作

        参考 Qt DeviceBase::executeAction()
        """
        if action == 'setPLC':
            return self.set_plc(**params) if params else False
        # 其他 action 类型可按需扩展
        print(f'[PLC] 未知 action: {action}')
        return False
