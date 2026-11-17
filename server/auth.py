# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""账号管理、密码哈希与 HMAC 令牌校验。"""

import base64
import hashlib
import re
import hmac
import json
import os
import secrets
import time

import db

TOKEN_TTL = int(os.environ.get("DM_TOKEN_TTL", "86400"))   

def _secret():
    """优先使用环境变量中的签名密钥，否则生成并持久化。"""
    env = os.environ.get("DM_SECRET")
    if env:
        return env.encode()
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".secret")
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    val = secrets.token_bytes(32)
    with open(path, "wb") as f:
        f.write(val)
    os.chmod(path, 0o600)
    return val


PBKDF2_ROUNDS = 200_000


def hash_password(password):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return "pbkdf2$%d$%s$%s" % (
        PBKDF2_ROUNDS,
        base64.b64encode(salt).decode(),
        base64.b64encode(dk).decode())


def verify_password(password, stored):
    try:
        algo, rounds, salt_b64, hash_b64 = stored.split("$")
        if algo != "pbkdf2":
            return False
        salt = base64.b64decode(salt_b64)
        expect = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(rounds))
        return hmac.compare_digest(dk, expect)
    except Exception:
        return False


def _b64(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(s):
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def issue_token(account, role):
    payload = {"sub": account, "role": role, "iat": int(time.time()),
               "exp": int(time.time()) + TOKEN_TTL}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
    return body + "." + sig


def parse_token(token):
    """校验签名及有效期，成功返回载荷，否则返回 None。"""
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    want = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, want):
        return None
    try:
        payload = json.loads(_unb64(body))
    except Exception:
        return None
    if payload.get("exp", 0) < int(time.time()):
        return None
    return payload


ROLE_RANK = {"user": 1, "operator": 1, "reviewer": 1, "admin": 2}
ROLE_CANON = {"operator": "user", "reviewer": "user", "user": "user", "admin": "admin"}
def canon_role(role):

    return ROLE_CANON.get((role or "").strip(), "user")

def ensure_bootstrap_user():
    """无账号时创建初始管理员，返回 (account, password_or_None)。"""
    with db.connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        if row["n"] > 0:
            return None, None
        account = os.environ.get("DM_ADMIN_ACCOUNT", "admin")
        pwd = os.environ.get("DM_ADMIN_PASSWORD")
        generated = False
        if not pwd:
            pwd = secrets.token_urlsafe(12)
            generated = True
        conn.execute(
            "INSERT INTO users(account, password_hash, role, created_at) VALUES (?,?,?,?)",
            (account, hash_password(pwd), "admin", db.now()))
        db.audit(conn, account, "bootstrap_admin", account)
        return account, (pwd if generated else None)


def create_user(account, password, role="user", display_name=None):
    role = canon_role(role)
    if role not in ("user", "admin"):
        raise ValueError("role 必须是 user 或 admin")
    name = (display_name or "").strip() or account
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO users(account, password_hash, role, created_at, display_name) "
            "VALUES (?,?,?,?,?)",
            (account, hash_password(password), role, db.now(), name))
    return {"account": account, "role": role, "display_name": name}


ACCOUNT_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
PASSWORD_MIN = 8
PASSWORD_MAX = 128


def validate_registration(account, password, display_name=None):
    """校验账号、密码及展示名；通过时返回 None。"""
    account = (account or "").strip()
    password = password or ""

    if not account:
        return "账号不能为空"
    if not ACCOUNT_RE.match(account):
        return "账号只能用字母、数字、下划线、点或横线，长度 3-32"

    if display_name is not None:
        dn = (display_name or "").strip()
        if not dn:
            return "用户名不能为空"
        if len(dn) > 32:
            return "用户名不能超过 32 个字符"

    if not password:
        return "密码不能为空"
    if len(password) < PASSWORD_MIN:
        return "密码至少 %d 位" % PASSWORD_MIN
    if len(password) > PASSWORD_MAX:
        return "密码不能超过 %d 位" % PASSWORD_MAX
    if password.isdigit():
        return "密码不能是纯数字"
    return None


def account_exists(account):
    with db.connect() as conn:
        row = conn.execute("SELECT 1 FROM users WHERE account=?", (account,)).fetchone()
    return bool(row)


def register(account, password, role="user", display_name=None):
    err = validate_registration(account, password, display_name)
    if err:
        return False, {"code": 400, "message": err}
    account = account.strip()
    if account_exists(account):
        return False, {"code": 409, "message": "账号已存在"}
    try:
        info = create_user(account, password, role, display_name)
    except Exception as e:
        return False, {"code": 500, "message": "创建账号失败：%s" % e}
    with db.connect() as conn:
        db.audit(conn, account, "register", account)
    return True, info


RESET_TTL_MIN = int(os.environ.get("DM_RESET_TTL_MIN", "30"))
def issue_reset_code(account, created_by=None):
    """为账号签发一次性重置码。账号不存在也返回同样的错误，避免探测账号是否存在。"""
    account = (account or "").strip()
    if not account:
        return False, {"code": 400, "message": "账号不能为空"}
    if not account_exists(account):

        return False, {"code": 404, "message": "无法为该账号生成重置码"}

    code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(10))
    expires = time.strftime("%Y-%m-%d %H:%M:%S",
                            time.localtime(time.time() + RESET_TTL_MIN * 60))
    with db.connect() as conn:

        conn.execute("DELETE FROM reset_codes WHERE account=? AND used_at IS NULL", (account,))
        conn.execute(
            "INSERT INTO reset_codes(code, account, created_at, created_by, expires_at) "
            "VALUES (?,?,?,?,?)",
            (code, account, db.now(), created_by, expires))
        db.audit(conn, created_by, "reset_code_issue", account)
    return True, {"account": account, "code": code, "expires_at": expires,
                  "ttl_min": RESET_TTL_MIN}


def consume_reset_code(account, code, new_password, ip=None):
    """校验一次性重置码并更新密码。"""
    account = (account or "").strip()
    code = (code or "").strip().upper()

    err = validate_registration(account, new_password)
    if err:
        return False, {"code": 400, "message": err}

    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM reset_codes WHERE code=? AND account=?", (code, account)).fetchone()
        if not row:
            return False, {"code": 400, "message": "重置码无效"}
        if row["used_at"]:
            return False, {"code": 400, "message": "该重置码已被使用"}
        if row["expires_at"] < db.now():
            return False, {"code": 400, "message": "重置码已过期，请重新申请"}

        conn.execute("UPDATE users SET password_hash=? WHERE account=?",
                     (hash_password(new_password), account))
        conn.execute("UPDATE reset_codes SET used_at=?, used_ip=? WHERE code=?",
                     (db.now(), ip, code))
        db.audit(conn, account, "reset_password", account, ip)
    return True, {"account": account}


def login(account, password):
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE account=?", (account,)).fetchone()
        if not row or not verify_password(password, row["password_hash"]):
            return None
        conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (db.now(), row["id"]))
        db.audit(conn, account, "login")
        return {"token": issue_token(row["account"], row["role"]),
                "account": row["account"], "role": row["role"],
                "display_name": row["display_name"] or row["account"],
                "expires_in": TOKEN_TTL}


def current_user(token):
    payload = parse_token(token)
    if not payload:
        return None
    with db.connect() as conn:
        row = conn.execute(
            "SELECT account, role, display_name FROM users WHERE account=?",
            (payload["sub"],)).fetchone()
        if not row:
            return None
        return {"account": row["account"], "role": row["role"],
                "display_name": row["display_name"] or row["account"]}


def require(user, min_role="user"):
    if not user:
        return False
    return ROLE_RANK.get(user.get("role"), 0) >= ROLE_RANK.get(min_role, 1)


def _admin_count(conn):
    return conn.execute(
        "SELECT COUNT(*) AS n FROM users WHERE role='admin'").fetchone()["n"]


def list_users():
    """返回账号列表，管理员优先，其余按创建时间升序。"""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT account, role, display_name, created_at, last_login_at "
            "FROM users ORDER BY CASE WHEN role='admin' THEN 0 ELSE 1 END, "
            "created_at ASC, account ASC").fetchall()
    return [{
        "account": r["account"],
        "display_name": r["display_name"] or r["account"],
        "role": canon_role(r["role"]),
        "created_at": r["created_at"],
        "last_login_at": r["last_login_at"],
    } for r in rows]


def set_user_role(account, role, by=None):
    """更新角色，返回 (ok, payload)。"""
    account = (account or "").strip()
    role = canon_role(role)
    if role not in ("user", "admin"):
        return False, {"code": 400, "message": "权限只能是用户或管理员"}
    if not account:
        return False, {"code": 400, "message": "账号不能为空"}
    if by and account == by:
        return False, {"code": 400, "message": "不能修改自己的权限"}

    with db.connect() as conn:
        row = conn.execute("SELECT role FROM users WHERE account=?",
                           (account,)).fetchone()
        if not row:
            return False, {"code": 404, "message": "账号不存在"}
        before = canon_role(row["role"])
        if before == role:
            return True, {"account": account, "role": role, "unchanged": True}
        if before == "admin" and _admin_count(conn) <= 1:
            return False, {"code": 400, "message": "至少要保留一个管理员"}
        conn.execute("UPDATE users SET role=? WHERE account=?", (role, account))
        db.audit(conn, by, "user_role", account, before + "→" + role)
    return True, {"account": account, "role": role}


def delete_user(account, by=None):
    """删除账号，返回 (ok, payload)。"""
    account = (account or "").strip()
    if not account:
        return False, {"code": 400, "message": "账号不能为空"}
    if by and account == by:
        return False, {"code": 400, "message": "不能删除自己的账号"}

    with db.connect() as conn:
        row = conn.execute("SELECT role FROM users WHERE account=?",
                           (account,)).fetchone()
        if not row:
            return False, {"code": 404, "message": "账号不存在"}
        if canon_role(row["role"]) == "admin" and _admin_count(conn) <= 1:
            return False, {"code": 400, "message": "至少要保留一个管理员"}

        conn.execute("DELETE FROM reset_codes WHERE account=?", (account,))
        conn.execute("DELETE FROM users WHERE account=?", (account,))
        db.audit(conn, by, "user_delete", account)
    return True, {"account": account}

if __name__ == "__main__":
    db.migrate()
    acc, pwd = ensure_bootstrap_user()
    if acc:
        print("已创建初始管理员:", acc)
        if pwd:
            print("  密码（仅本次显示，请记录）:", pwd)
    else:
        print("用户表已有账号，跳过")
