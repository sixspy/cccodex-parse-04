from django.urls import path, re_path
from catalog import views

urlpatterns = [
    path("", views.root),
    path("login", views.login_page, name="login"),
    path("logout", views.logout_view),
    path("jobs/", views.job_list),
    path("jobs/<int:job_id>/", views.job_detail),
    path("jobs/<int:job_id>/parse/", views.job_parse),
    path("jobs/<int:job_id>/land/", views.job_land),
    path("quota/", views.quota_list, name="quota_list"),
    path("quota/set/", views.quota_set, name="quota_set"),
    path("candidates/<int:candidate_id>/resume/", views.candidate_resume, name="candidate_resume"),
    re_path(r"^quotas?/?$", views.quota_list),
]
