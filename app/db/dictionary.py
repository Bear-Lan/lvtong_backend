"""字典数据

- 货车类型 truck_type
- 货箱类型 container_type
- 收费站 station_info
- 不合格类型 nopass_type
"""
from app.db.base import BaseRepo


class DBDictionary(BaseRepo):

    def getAllTruckTypes(self) -> list[dict]:
        return self._rows("SELECT * FROM truck_type ORDER BY sort_order")

    def getAllContainerTypes(self) -> list[dict]:
        return self._rows("SELECT * FROM container_type ORDER BY sort_order")

    def getStationById(self, station_id: str) -> dict | None:
        return self._one(
            "SELECT * FROM station_info WHERE station_id = :id",
            {'id': station_id},
        )

    def getAllNoPassTypes(self) -> list[dict]:
        return self._rows("SELECT * FROM nopass_type ORDER BY id")
