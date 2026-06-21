from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("draw/<int:round_id>/", views.public_draw, name="public_draw"),
    path("standings/", views.public_standings, name="public_standings"),
    path("results/", views.final_results, name="final_results"),
    path("juiz/<str:token>/", views.judge_portal, name="judge_portal"),
    path("manage/", views.dashboard, name="dashboard"),
    path("manage/setup/", views.setup_rounds, name="setup_rounds"),
    path("manage/import/<str:kind>/", views.csv_import, name="csv_import"),
    path("manage/round/<int:round_id>/", views.manage_round, name="manage_round"),
    path("manage/round/<int:round_id>/generate/", views.generate_draw, name="generate_draw"),
    path("manage/round/<int:round_id>/publish/", views.publish_draw, name="publish_draw"),
    path("manage/round/<int:round_id>/swap/", views.swap_slots, name="swap_slots"),
    path("manage/round/<int:round_id>/judges/", views.allocate_judges, name="allocate_judges"),
    path("manage/judges/links/", views.judge_links, name="judge_links"),
    path("manage/judges/<int:judge_id>/new-link/", views.regenerate_judge_link, name="regenerate_judge_link"),
    path("manage/room/<int:room_id>/confirm/", views.confirm_result, name="confirm_result"),
    path("manage/standings/", views.admin_standings, name="admin_standings"),
    path("manage/break/", views.manage_break, name="manage_break"),
    path("manage/final-publish/", views.publish_final, name="publish_final"),
]
