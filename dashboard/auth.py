"""
用户认证模块

提供注册、登录、退出的 Blueprint，基于 Flask-Login + werkzeug 密码哈希。
"""
import re
import logging
from flask import Blueprint, request, redirect, url_for, render_template, flash, jsonify
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from dashboard import models

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")

# ---- LoginManager 配置 ----
login_manager = LoginManager()
login_manager.login_view = "auth.login_page"
login_manager.login_message = "请先登录后使用"
login_manager.login_message_category = "info"


class User(UserMixin):
    """Flask-Login 用户代理"""

    def __init__(self, user_id: int, username: str):
        self.id = user_id
        self.username = username


@login_manager.user_loader
def load_user(user_id):
    row = models.get_user_by_id(int(user_id))
    if row:
        return User(row["id"], row["username"])
    return None


@login_manager.unauthorized_handler
def unauthorized():
    """API 请求未登录时返回 401 JSON，页面请求重定向到登录页"""
    if request.path.startswith("/api/"):
        return jsonify({"error": "请先登录", "redirect": url_for("auth.login_page")}), 401
    return redirect(url_for("auth.login_page"))


# ---- 页面路由 ----

@auth_bp.route("/login", methods=["GET"])
def login_page():
    """登录/注册页面"""
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    return render_template("auth.html")


# ---- API 路由 ----

@auth_bp.route("/register", methods=["POST"])
def register():
    """注册新用户"""
    data = request.form or request.json or {}
    username = (data.get("username") or "").strip()
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""

    # 验证
    errors = []
    if not username or len(username) < 3 or len(username) > 20:
        errors.append("用户名需 3-20 个字符")
    if not re.match(r'^[a-zA-Z0-9_\u4e00-\u9fff]+$', username):
        errors.append("用户名只能包含字母、数字、下划线或中文")
    if len(password) < 6:
        errors.append("密码至少 6 个字符")
    if email and not re.match(r'^[^@]+@[^@]+\.[^@]+$', email):
        errors.append("邮箱格式不正确")

    if errors:
        if request.is_json:
            return jsonify({"success": False, "errors": errors}), 400
        for e in errors:
            flash(e, "danger")
        return render_template("auth.html", tab="register")

    # 检查用户名是否已存在
    if models.get_user_by_username(username):
        msg = "该用户名已被注册"
        if request.is_json:
            return jsonify({"success": False, "errors": [msg]}), 400
        flash(msg, "danger")
        return render_template("auth.html", tab="register")

    # 创建用户
    pw_hash = generate_password_hash(password, method="pbkdf2:sha256")
    user_id = models.create_user(username, email, pw_hash)
    user = User(user_id, username)
    login_user(user, remember=True)

    logger.info("新用户注册: %s (id=%d)", username, user_id)
    if request.is_json:
        return jsonify({"success": True, "username": username})
    flash(f"欢迎 {username}！注册成功并已自动登录。", "success")
    return redirect(url_for("index"))


@auth_bp.route("/login", methods=["POST"])
def login():
    """用户登录"""
    data = request.form or request.json or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    if not username or not password:
        msg = "请输入用户名和密码"
        if request.is_json:
            return jsonify({"success": False, "errors": [msg]}), 400
        flash(msg, "danger")
        return render_template("auth.html", tab="login")

    row = models.get_user_by_username(username)
    if not row or not check_password_hash(row["password_hash"], password):
        msg = "用户名或密码错误"
        if request.is_json:
            return jsonify({"success": False, "errors": [msg]}), 401
        flash(msg, "danger")
        return render_template("auth.html", tab="login")

    user = User(row["id"], row["username"])
    login_user(user, remember=True)

    logger.info("用户登录: %s", username)
    if request.is_json:
        return jsonify({"success": True, "username": username})
    flash(f"欢迎回来，{username}！", "success")
    return redirect(url_for("index"))


@auth_bp.route("/logout")
def logout():
    """退出登录"""
    logout_user()
    flash("已退出登录", "info")
    return redirect(url_for("auth.login_page"))


@auth_bp.route("/me")
def current_user_info():
    """获取当前登录用户信息"""
    if current_user.is_authenticated:
        return jsonify({
            "logged_in": True,
            "username": current_user.username,
            "user_id": current_user.id,
        })
    return jsonify({"logged_in": False}), 401
