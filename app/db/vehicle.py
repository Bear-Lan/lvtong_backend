"""车辆检测记录 vehicle_inspections"""
from sqlalchemy import text as _text
from app.db.base import BaseRepo


class DBVehicle(BaseRepo):

    # ---- 写入 ----

    def addInspection(self, inspection: dict) -> int:
        """新增检测记录，返回自增 id

        只 INSERT 表中真实存在的列，不存在的字段自动跳过。
        """
        columns = _get_columns()
        # 排除只读列
        skip = {'id', 'created_time', 'updated_time'}

        # 默认值（只包含表中存在的列）
        defaults = {
            'plate_number': '', 'plate_number_gc': '', 'driver_phone': '',
            'vehicle_type': '', 'vehicle_containertype': '',
            'goods_type': '', 'goods_category': '',
            'load_rate': 0, 'load_weight': 0, 'vehicle_size': '',
            'body_image_path': '', 'transparent_image_path': '',
            'head_image_path': '', 'tail_image_path': '', 'top_image_path': '',
            'goods_image_path': '', 'evidences_image_path': '',
            'license_image_path': '', 'passcode_image_path': '',
            'operator_name': '', 'inspector_phone': '',
            'reviewer_phone': '', 'group_id': '',
            'btn_prebook_time': None, 'acceptance_time': None,
            'opengate_time': None, 'openlightscreen_time': None,
            'closelightscreen_time': None, 'cd_photo_time': None,
            'inspection_time': None,
            'result_status': 0, 'nopass_type': 0, 'status': 0,
            'passcode_vehicle_id': '', 'passcode_vehicle_display_id': '',
            'passcode_vehicle_color_name': '',
            'passcode_en_station_id': '', 'passcode_ex_station_id': '',
            'passcode_en_weight': '', 'passcode_ex_weight': '',
            'passcode_media_type': '', 'passcode_transaction_id': '',
            'passcode_pass_id': '',
            'passcode_ex_time': '', 'passcode_trans_pay_type': '',
            'passcode_fee': '', 'passcode_pay_fee': '',
            'passcode_vehicle_sign': '', 'passcode_province_count': '',
        }
        # 只保留真实列
        data = {k: v for k, v in defaults.items() if k in columns and k not in skip}
        # 合并传入数据（同样只取真实列）
        for k, v in inspection.items():
            if k in columns and k not in skip:
                data[k] = v

        cols = list(data.keys())
        placeholders = ', '.join(f':{c}' for c in cols)
        names = ', '.join(cols)
        sql = f"INSERT INTO vehicle_inspections ({names}) VALUES ({placeholders}) RETURNING id"

        with self._tx() as conn:
            result = conn.execute(_text(sql), data)
            row = result.fetchone()
            return row[0] if row else -1

    def updateInspection(self, inspection_id: int, data: dict) -> bool:
        """更新检测记录

        只允许 UPDATE 表中实际存在的列，自动过滤不存在的字段。
        排除 id / created_time / updated_time。
        """
        columns = _get_columns()
        readonly = {'id', 'created_time', 'updated_time'}

        updates = {
            k: v for k, v in data.items()
            if k in columns and k not in readonly
        }
        if not updates:
            return False

        set_clause = ', '.join(f"{k} = :{k}" for k in updates)
        updates['_id'] = inspection_id
        sql = (f"UPDATE vehicle_inspections SET {set_clause}, "
               f"updated_time = NOW() WHERE id = :_id")

        with self._tx() as conn:
            result = conn.execute(_text(sql), updates)
            return result.rowcount > 0

    def deleteInspection(self, inspection_id: int) -> bool:
        with self._tx() as conn:
            return self._exec(
                "UPDATE vehicle_inspections SET status=1, updated_time=NOW() "
                "WHERE id = :id",
                {'id': inspection_id}, conn=conn,
            ) > 0

    # ---- 查询 ----

    def getInspectionById(self, inspection_id: int) -> dict | None:
        d = self._one(
            "SELECT * FROM vehicle_inspections WHERE id = :id AND status = 0",
            {'id': inspection_id},
        )
        return _normalize(d) if d else None

    def getInspectionsByPlate(self, plate: str) -> list[dict]:
        rows = self._rows(
            "SELECT * FROM vehicle_inspections "
            "WHERE plate_number = :plate AND status = 0 "
            "ORDER BY inspection_time DESC",
            {'plate': plate},
        )
        return [_normalize(r) for r in rows]

    def getInspectionCountByPlate(self, plate: str) -> int:
        return self._scalar(
            "SELECT COUNT(*) FROM vehicle_inspections "
            "WHERE plate_number = :plate AND status = 0",
            {'plate': plate},
        )

    def getDriverPhoneByPlate(self, plate: str) -> str:
        row = self._one(
            "SELECT driver_phone FROM vehicle_inspections "
            "WHERE plate_number = :plate AND status = 0 "
            "ORDER BY inspection_time DESC LIMIT 1",
            {'plate': plate},
        )
        return row['driver_phone'] if row else ''

    def getGCPlateByPlate(self, plate: str) -> str:
        row = self._one(
            "SELECT plate_number_gc FROM vehicle_inspections "
            "WHERE plate_number = :plate AND status = 0 "
            "ORDER BY inspection_time DESC LIMIT 1",
            {'plate': plate},
        )
        return row['plate_number_gc'] if row else ''

    def getInspectionsCount(self, plate_number='', driver_phone='',
                            vehicle_type='', goods_type='', operator_name='',
                            start_time=None, end_time=None) -> int:
        sql, params = _build_where(
            "SELECT COUNT(*) FROM vehicle_inspections WHERE status = 0",
            plate_number, driver_phone, vehicle_type, goods_type,
            operator_name, start_time, end_time,
        )
        return self._scalar(sql, params)

    def getInspectionsWithFilter(self, plate_number='', driver_phone='',
                                 vehicle_type='', goods_type='',
                                 operator_name='',
                                 start_time=None, end_time=None,
                                 page=1, page_size=50) -> list[dict]:
        sql, params = _build_where(
            "SELECT * FROM vehicle_inspections WHERE status = 0",
            plate_number, driver_phone, vehicle_type, goods_type,
            operator_name, start_time, end_time,
        )
        sql += " ORDER BY inspection_time DESC LIMIT :limit OFFSET :offset"
        params['limit'] = page_size
        params['offset'] = (page - 1) * page_size
        rows = self._rows(sql, params)
        return [_normalize(r) for r in rows]


# ---- 模块级缓存 & 工具函数 ----

_table_columns: set | None = None


def _get_columns() -> set:
    """从 information_schema 读取 vehicle_inspections 的真实列名（缓存）"""
    global _table_columns
    if _table_columns is not None:
        return _table_columns

    from app.db.engine import engine
    from sqlalchemy import text

    with engine.connect() as conn:
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'vehicle_inspections'"
        ))
        _table_columns = {row[0] for row in result.fetchall()}
    return _table_columns


def _build_where(base_sql: str, plate_number='', driver_phone='',
                 vehicle_type='', goods_type='', operator_name='',
                 start_time=None, end_time=None):
    """构建 WHERE 筛选条件，返回 (sql, params_dict)"""
    params: dict = {}
    if plate_number:
        base_sql += " AND plate_number LIKE :pn"
        params['pn'] = f'%{plate_number}%'
    if driver_phone:
        base_sql += " AND driver_phone LIKE :dp"
        params['dp'] = f'%{driver_phone}%'
    if vehicle_type:
        base_sql += " AND vehicle_type = :vt"
        params['vt'] = vehicle_type
    if goods_type:
        base_sql += " AND goods_type LIKE :gt"
        params['gt'] = f'%{goods_type}%'
    if operator_name:
        base_sql += " AND operator_name LIKE :op"
        params['op'] = f'%{operator_name}%'
    if start_time:
        base_sql += " AND inspection_time >= :st"
        params['st'] = start_time
    if end_time:
        base_sql += " AND inspection_time <= :et"
        params['et'] = end_time
    return base_sql, params


_TIME_FIELDS = (
    'btn_prebook_time', 'acceptance_time', 'opengate_time',
    'openlightscreen_time', 'closelightscreen_time',
    'cd_photo_time', 'inspection_time', 'created_time', 'updated_time',
)


def _normalize(d: dict) -> dict:
    """行后处理：时间 isoformat + 字段名规范化"""
    _dt(d)
    _rename_prefix(d, 'passcode_', 'pass_code_')
    _rename(d, 'passcode_media_type', 'pass_code_media_type_id')
    _rename(d, 'vehicle_containertype', 'vehicle_container_type')
    _rename(d, 'nopass_type', 'no_pass_type')
    return d


def _dt(d: dict):
    for field in _TIME_FIELDS:
        val = d.get(field)
        if val is not None and hasattr(val, 'isoformat'):
            d[field] = val.isoformat()


def _rename(d: dict, old: str, new: str):
    if old in d and new not in d:
        d[new] = d.pop(old)


def _rename_prefix(d: dict, prefix: str, new_prefix: str):
    """passcode_vehicle_id → pass_code_vehicle_id"""
    for old_key in list(d.keys()):
        if old_key.startswith(prefix) and not old_key.startswith(new_prefix):
            new_key = new_prefix + old_key[len(prefix):]
            if new_key not in d:
                d[new_key] = d.pop(old_key)
