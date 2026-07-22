"""用户 users"""
import hashlib
from app.db.base import BaseRepo


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode('utf-8')).hexdigest()


class DBUser(BaseRepo):

    def changePassword(self, uid: str, password: str) -> bool:
        with self._tx() as conn:
            row = self._one(
                "SELECT 1 FROM users WHERE username = :uid",
                {'uid': uid}, conn=conn,
            )
            if row:
                self._exec(
                    "UPDATE users SET password = :pw WHERE username = :uid",
                    {'pw': _sha256(password), 'uid': uid}, conn=conn,
                )
                return True
        return False

    def updateUser(self, username: str, user: dict):
        pw = _sha256(user['password'])
        phone = user.get('phone', '')
        email = user.get('email', '')
        usergroup = user.get('usergroup', 0)
        usertype = user.get('usertype', '')
        with self._tx() as conn:
            exists = self._one(
                "SELECT 1 FROM users WHERE username = :uid",
                {'uid': username}, conn=conn,
            )
            if exists:
                self._exec(
                    "UPDATE users SET password=:pw, real_name=:name, role=:role, "
                    "phone=:phone, email=:email, group_id=:grp, user_type=:utype "
                    "WHERE username = :uid",
                    {'pw': pw, 'name': user['real_name'], 'role': user['role'],
                     'phone': phone, 'email': email, 'grp': usergroup,
                     'utype': usertype, 'uid': username},
                    conn=conn,
                )
            else:
                self._exec(
                    "INSERT INTO users(username, real_name, password, role, "
                    "phone, email, group_id, user_type) "
                    "VALUES(:uid, :name, :pw, :role, :phone, :email, :grp, :utype)",
                    {'uid': username, 'name': user['real_name'],
                     'pw': pw, 'role': user['role'],
                     'phone': phone, 'email': email, 'grp': usergroup,
                     'utype': usertype},
                    conn=conn,
                )

    def users(self, filter: str = "") -> list:
        base_sql = (
            "SELECT id, username, real_name, password, role, "
            "phone, email, group_id AS usergroup, user_type AS usertype FROM users"
        )
        if filter:
            return self._rows(
                base_sql + " WHERE username = :uid ORDER BY id",
                {'uid': filter},
            )
        return self._rows(base_sql + " ORDER BY id")

    def loginUser(self, username: str, password: str) -> dict | None:
        return self._one(
            "SELECT username, real_name, password, role, phone FROM users "
            "WHERE username = :uid AND password = :pw",
            {'uid': username, 'pw': _sha256(password)},
        )

    def delUser(self, username: str):
        with self._tx() as conn:
            self._exec(
                "DELETE FROM users WHERE username = :uid",
                {'uid': username}, conn=conn,
            )
