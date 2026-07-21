"""字典数据数据库操作层

包含：货车类型、货箱类型、收费站信息、不合格类型。
参考 Qt TruckTypeDatabase / ContainerTypeDatabase 实现。
"""
from app.db.pool import DBPool


class DBDictionary(DBPool):
    """字典数据查询"""

    # ========== 货车类型 ==========
    def getAllTruckTypes(self) -> list[dict]:
        conn = self.getConn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM truck_type ORDER BY sort_order"
                )
                return [self._row_to_dict(r, cursor) for r in cursor.fetchall()]
        finally:
            self.releaseConn(conn)

    # ========== 货箱类型 ==========
    def getAllContainerTypes(self) -> list[dict]:
        conn = self.getConn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM container_type ORDER BY sort_order"
                )
                return [self._row_to_dict(r, cursor) for r in cursor.fetchall()]
        finally:
            self.releaseConn(conn)

    # ========== 收费站 ==========
    def getStationById(self, station_id: str) -> dict | None:
        conn = self.getConn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM station_info WHERE station_id = %s", (station_id,)
                )
                row = cursor.fetchone()
                return self._row_to_dict(row, cursor) if row else None
        finally:
            self.releaseConn(conn)

    # ========== 不合格类型 ==========
    def getAllNoPassTypes(self) -> list[dict]:
        conn = self.getConn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM nopass_type ORDER BY id")
                return [self._row_to_dict(r, cursor) for r in cursor.fetchall()]
        finally:
            self.releaseConn(conn)

    @staticmethod
    def _row_to_dict(row, cursor) -> dict:
        cols = [desc[0] for desc in cursor.description]
        return dict(zip(cols, row))
