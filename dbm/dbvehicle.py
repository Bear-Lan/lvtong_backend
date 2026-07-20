"""车辆检测记录数据库操作层

参考 Qt VehicleDatabase 实现。
"""
from dbm.dbpool import DBPool


class DBVehicle(DBPool):
    """车辆检测记录 CRUD"""

    # ---------- 写入 ----------
    def addInspection(self, inspection: dict) -> int:
        """新增检测记录，返回自增 id"""
        sql = """
            INSERT INTO vehicle_inspections (
                plate_number, plate_number_gc, driver_phone,
                vehicle_type, vehicle_name, vehicle_container_type, vehicle_container_name,
                goods_type, goods_name, goods_category,
                load_rate, load_weight, vehicle_size,
                body_image_path, transparent_image_path,
                head_image_path, tail_image_path, top_image_path,
                goods_image_path, evidences_image_path,
                license_image_path1, license_image_path2, license_image_path,
                pass_code_image_path,
                operator_name, inspector_phone, reviewer_name, reviewer_phone, group_id,
                btn_prebook_time, acceptance_time,
                open_gate_time, open_light_screen_time, close_light_screen_time,
                cd_photo_time, inspection_time,
                result_status, no_pass_type, status,
                pass_code_vehicle_id, pass_code_vehicle_display_id,
                pass_code_vehicle_color_name,
                pass_code_en_station_id, pass_code_ex_station_id,
                pass_code_en_weight, pass_code_ex_weight,
                pass_code_media_type_id, pass_code_transaction_id, pass_code_pass_id,
                pass_code_ex_time, pass_code_trans_pay_type,
                pass_code_fee, pass_code_pay_fee, pass_code_vehicle_sign,
                pass_code_province_count,
                cvarietyname, btypename, ccategoryname, ctypename
            ) VALUES (
                %(plate_number)s, %(plate_number_gc)s, %(driver_phone)s,
                %(vehicle_type)s, %(vehicle_name)s,
                %(vehicle_container_type)s, %(vehicle_container_name)s,
                %(goods_type)s, %(goods_name)s, %(goods_category)s,
                %(load_rate)s, %(load_weight)s, %(vehicle_size)s,
                %(body_image_path)s, %(transparent_image_path)s,
                %(head_image_path)s, %(tail_image_path)s, %(top_image_path)s,
                %(goods_image_path)s, %(evidences_image_path)s,
                %(license_image_path1)s, %(license_image_path2)s, %(license_image_path)s,
                %(pass_code_image_path)s,
                %(operator_name)s, %(inspector_phone)s,
                %(reviewer_name)s, %(reviewer_phone)s, %(group_id)s,
                %(btn_prebook_time)s, %(acceptance_time)s,
                %(open_gate_time)s, %(open_light_screen_time)s, %(close_light_screen_time)s,
                %(cd_photo_time)s, %(inspection_time)s,
                %(result_status)s, %(no_pass_type)s, %(status)s,
                %(pass_code_vehicle_id)s, %(pass_code_vehicle_display_id)s,
                %(pass_code_vehicle_color_name)s,
                %(pass_code_en_station_id)s, %(pass_code_ex_station_id)s,
                %(pass_code_en_weight)s, %(pass_code_ex_weight)s,
                %(pass_code_media_type_id)s, %(pass_code_transaction_id)s, %(pass_code_pass_id)s,
                %(pass_code_ex_time)s, %(pass_code_trans_pay_type)s,
                %(pass_code_fee)s, %(pass_code_pay_fee)s, %(pass_code_vehicle_sign)s,
                %(pass_code_province_count)s,
                %(cvarietyname)s, %(btypename)s, %(ccategoryname)s, %(ctypename)s
            )
            RETURNING id
        """
        defaults = {
            'plate_number': '', 'plate_number_gc': '', 'driver_phone': '',
            'vehicle_type': '', 'vehicle_name': '',
            'vehicle_container_type': '', 'vehicle_container_name': '',
            'goods_type': '', 'goods_name': '', 'goods_category': '',
            'load_rate': 0, 'load_weight': 0, 'vehicle_size': '',
            'body_image_path': '', 'transparent_image_path': '',
            'head_image_path': '', 'tail_image_path': '', 'top_image_path': '',
            'goods_image_path': '', 'evidences_image_path': '',
            'license_image_path1': '', 'license_image_path2': '', 'license_image_path': '',
            'pass_code_image_path': '',
            'operator_name': '', 'inspector_phone': '',
            'reviewer_name': '', 'reviewer_phone': '', 'group_id': 0,
            'btn_prebook_time': None, 'acceptance_time': None,
            'open_gate_time': None, 'open_light_screen_time': None,
            'close_light_screen_time': None, 'cd_photo_time': None,
            'inspection_time': None,
            'result_status': 0, 'no_pass_type': 0, 'status': 0,
            'pass_code_vehicle_id': '', 'pass_code_vehicle_display_id': '',
            'pass_code_vehicle_color_name': '',
            'pass_code_en_station_id': '', 'pass_code_ex_station_id': '',
            'pass_code_en_weight': '', 'pass_code_ex_weight': '',
            'pass_code_media_type_id': '9', 'pass_code_transaction_id': '',
            'pass_code_pass_id': '',
            'pass_code_ex_time': '', 'pass_code_trans_pay_type': '',
            'pass_code_fee': '', 'pass_code_pay_fee': '', 'pass_code_vehicle_sign': '',
            'pass_code_province_count': '',
            'cvarietyname': '', 'btypename': '', 'ccategoryname': '', 'ctypename': '',
        }
        for k, v in defaults.items():
            inspection.setdefault(k, v)

        conn = self.getConn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute(sql, inspection)
                row = cursor.fetchone()
                return row[0] if row else -1
        finally:
            self.releaseConn(conn)

    def updateInspection(self, inspection_id: int, data: dict) -> bool:
        """更新检测记录（只更新传入的字段）"""
        allowed = {
            'vehicle_type', 'vehicle_name', 'vehicle_container_type', 'vehicle_container_name',
            'goods_type', 'goods_name', 'goods_category',
            'load_rate', 'load_weight', 'vehicle_size',
            'plate_number', 'plate_number_gc', 'driver_phone',
            'body_image_path', 'transparent_image_path',
            'head_image_path', 'tail_image_path', 'top_image_path',
            'goods_image_path', 'evidences_image_path',
            'license_image_path1', 'license_image_path2', 'license_image_path',
            'pass_code_image_path',
            'result_status', 'no_pass_type', 'status',
        }
        updates = {k: v for k, v in data.items() if k in allowed}
        if not updates:
            return False

        set_clause = ', '.join(f"{k} = %({k})s" for k in updates)
        updates['id'] = inspection_id
        sql = f"UPDATE vehicle_inspections SET {set_clause}, updated_time = NOW() WHERE id = %(id)s"

        conn = self.getConn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute(sql, updates)
                return cursor.rowcount > 0
        finally:
            self.releaseConn(conn)

    def deleteInspection(self, inspection_id: int) -> bool:
        """软删除检测记录"""
        conn = self.getConn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE vehicle_inspections SET status = 1, updated_time = NOW() WHERE id = %s",
                    (inspection_id,)
                )
                return cursor.rowcount > 0
        finally:
            self.releaseConn(conn)

    # ---------- 查询 ----------
    def getInspectionById(self, inspection_id: int) -> dict | None:
        conn = self.getConn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM vehicle_inspections WHERE id = %s AND status = 0", (inspection_id,))
                row = cursor.fetchone()
                if row:
                    return self._row_to_dict(row, cursor)
                return None
        finally:
            self.releaseConn(conn)

    def getInspectionsByPlate(self, plate: str) -> list[dict]:
        conn = self.getConn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM vehicle_inspections WHERE plate_number = %s AND status = 0 "
                    "ORDER BY inspection_time DESC",
                    (plate,)
                )
                return [self._row_to_dict(r, cursor) for r in cursor.fetchall()]
        finally:
            self.releaseConn(conn)

    def getInspectionCountByPlate(self, plate: str) -> int:
        conn = self.getConn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT COUNT(*) FROM vehicle_inspections WHERE plate_number = %s AND status = 0",
                    (plate,)
                )
                return cursor.fetchone()[0]
        finally:
            self.releaseConn(conn)

    def getDriverPhoneByPlate(self, plate: str) -> str:
        conn = self.getConn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT driver_phone FROM vehicle_inspections "
                    "WHERE plate_number = %s AND status = 0 ORDER BY inspection_time DESC LIMIT 1",
                    (plate,)
                )
                row = cursor.fetchone()
                return row[0] if row else ''
        finally:
            self.releaseConn(conn)

    def getGCPlateByPlate(self, plate: str) -> str:
        conn = self.getConn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT plate_number_gc FROM vehicle_inspections "
                    "WHERE plate_number = %s AND status = 0 ORDER BY inspection_time DESC LIMIT 1",
                    (plate,)
                )
                row = cursor.fetchone()
                return row[0] if row else ''
        finally:
            self.releaseConn(conn)

    def getInspectionsCount(self, plate_number='', driver_phone='', vehicle_type='',
                            goods_type='', operator_name='',
                            start_time=None, end_time=None) -> int:
        sql, params = self._build_filter_query(
            "SELECT COUNT(*) FROM vehicle_inspections WHERE status = 0",
            plate_number, driver_phone, vehicle_type, goods_type, operator_name,
            start_time, end_time
        )
        conn = self.getConn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute(sql, params)
                return cursor.fetchone()[0]
        finally:
            self.releaseConn(conn)

    def getInspectionsWithFilter(self, plate_number='', driver_phone='', vehicle_type='',
                                 goods_type='', operator_name='',
                                 start_time=None, end_time=None,
                                 page=1, page_size=50) -> list[dict]:
        base = "SELECT * FROM vehicle_inspections WHERE status = 0"
        sql, params = self._build_filter_query(
            base, plate_number, driver_phone, vehicle_type, goods_type, operator_name,
            start_time, end_time
        )
        sql += " ORDER BY inspection_time DESC LIMIT %s OFFSET %s"
        params.append(page_size)
        params.append((page - 1) * page_size)

        conn = self.getConn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute(sql, params)
                return [self._row_to_dict(r, cursor) for r in cursor.fetchall()]
        finally:
            self.releaseConn(conn)

    def _build_filter_query(self, base_sql, plate_number, driver_phone, vehicle_type,
                            goods_type, operator_name, start_time, end_time):
        clauses = []
        params = []
        if plate_number:
            clauses.append("plate_number LIKE %s")
            params.append(f"%{plate_number}%")
        if driver_phone:
            clauses.append("driver_phone LIKE %s")
            params.append(f"%{driver_phone}%")
        if vehicle_type:
            clauses.append("vehicle_type = %s")
            params.append(vehicle_type)
        if goods_type:
            clauses.append("goods_type LIKE %s")
            params.append(f"%{goods_type}%")
        if operator_name:
            clauses.append("operator_name LIKE %s")
            params.append(f"%{operator_name}%")
        if start_time:
            clauses.append("inspection_time >= %s")
            params.append(start_time)
        if end_time:
            clauses.append("inspection_time <= %s")
            params.append(end_time)

        if clauses:
            base_sql += " AND " + " AND ".join(clauses)
        return base_sql, params

    @staticmethod
    def _row_to_dict(row, cursor) -> dict:
        cols = [desc[0] for desc in cursor.description]
        d = dict(zip(cols, row))
        # 时间字段转字符串
        for ts_field in ('btn_prebook_time', 'acceptance_time', 'open_gate_time',
                         'open_light_screen_time', 'close_light_screen_time',
                         'cd_photo_time', 'inspection_time', 'created_time', 'updated_time'):
            if d.get(ts_field):
                d[ts_field] = d[ts_field].isoformat()
        return d
