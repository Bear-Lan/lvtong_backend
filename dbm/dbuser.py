"""用户数据访问层"""
import hashlib
from dbm.dbpool import DBPool


def _sha256(s: str) -> str:
    """对字符串做 SHA-256 哈希，返回 hex 字符串"""
    return hashlib.sha256(s.encode('utf-8')).hexdigest()


class DBUser(DBPool):
    """用户数据库操作"""

    def changePassword(self, uid: str, password: str) -> bool:
        """修改密码"""
        conn = self.getConn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute("SELECT 1 FROM users WHERE username = %s", (uid,))
                if bool(cursor.fetchone()):
                    cursor.execute(
                        "UPDATE users SET password=%s WHERE username = %s",
                        (_sha256(password), uid)
                    )
                    return True
        finally:
            self.releaseConn(conn)
        return False

    def updateUser(self, username: str, user: dict):
        """创建或更新用户"""
        conn = self.getConn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT 1 FROM users WHERE username = %s", (username,)
                )
                if bool(cursor.fetchone()):
                    cursor.execute(
                        "UPDATE users SET password=%s, real_name=%s, role=%s WHERE username = %s",
                        (_sha256(user["password"]), user["real_name"], user["role"], username)
                    )
                else:
                    cursor.execute(
                        "INSERT INTO users(username, real_name, password, role) VALUES(%s, %s, %s, %s)",
                        (username, user["real_name"], _sha256(user["password"]), user["role"])
                    )
        finally:
            self.releaseConn(conn)

    def users(self, filter: str = "") -> list:
        """查询用户列表"""
        userInfo = []
        conn = self.getConn()
        try:
            with conn:
                cursor = conn.cursor()
                if filter == "":
                    cursor.execute(
                        "SELECT username, real_name, password, role FROM users ORDER BY id"
                    )
                else:
                    cursor.execute(
                        "SELECT username, real_name, password, role FROM users WHERE username = %s ORDER BY id",
                        (filter,)
                    )
                rows = cursor.fetchall()
                for row in rows:
                    userInfo.append({
                        "username": row[0],
                        "real_name": row[1],
                        "password": row[2],
                        "role": row[3],
                    })
        finally:
            self.releaseConn(conn)
        return userInfo

    def loginUser(self, username: str, password: str) -> dict | None:
        """登录验证：根据用户名和密码查询用户（后端 SHA-256 哈希）"""
        conn = self.getConn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT username, real_name, password, role, phone FROM users WHERE username = %s AND password = %s",
                    (username, _sha256(password))
                )
                row = cursor.fetchone()
                if row:
                    return {
                        "username": row[0],
                        "real_name": row[1],
                        "password": row[2],
                        "role": row[3],
                        "phone": row[4] or '',
                    }
                return None
        finally:
            self.releaseConn(conn)

    def delUser(self, username: str):
        """删除用户"""
        conn = self.getConn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM users WHERE username = %s", (username,))
        finally:
            self.releaseConn(conn)
