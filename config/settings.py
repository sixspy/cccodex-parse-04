import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = "cc-parse-lab-dev-only"
DEBUG = True
ALLOWED_HOSTS = ["*"]
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "catalog",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.messages.context_processors.messages",
    ]},
}]
db_path = os.environ.get("DATABASE_PATH") or str(BASE_DIR / "instance" / "parse_lab.db")
Path(db_path).parent.mkdir(parents=True, exist_ok=True)
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": db_path,
        "OPTIONS": {"timeout": 20},
    }
}
LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LAB_NAME = "解析入库台"
ADMIN_USER = "admin"
ADMIN_PASSWORD = "123456"
SESSION_COOKIE_NAME = "parse_session"

# 每日解析成功行数额度
DAILY_QUOTA = int(os.environ.get("DAILY_QUOTA") or 30)
# 本日超额策略默认值：candidate=入次日候补匣，fail=当场失败并写额度已满
OVERAGE_DEFAULT = os.environ.get("OVERAGE_DEFAULT") or "candidate"
# 测试可钉死结算日：ISO 日期字符串（如 2026-11-14）或 datetime.date
QUOTA_TODAY = os.environ.get("QUOTA_TODAY") or None
