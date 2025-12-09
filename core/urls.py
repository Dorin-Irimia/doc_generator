from django.urls import path
from . import views

urlpatterns = [
    path("", views.generate_select, name="home"),

    # cetățeni
    path("citizens/", views.citizen_list, name="citizen_list"),
    path("citizens/new/", views.citizen_create, name="citizen_create"),
    path("citizens/<int:pk>/edit/", views.citizen_edit, name="citizen_edit"),
    path("citizens/<int:pk>/delete/", views.citizen_delete, name="citizen_delete"),

    # template-uri
    path("templates/", views.template_list, name="template_list"),
    path("templates/new/", views.template_create, name="template_create"),
    path("templates/<slug:slug>/edit/", views.template_edit, name="template_edit"),
    path("templates/<slug:slug>/delete/", views.template_delete, name="template_delete"),

    # generare
    path("generate/", views.generate_select, name="generate_select"),
    path(
        "generate/<int:citizen_id>/<slug:template_slug>/",
        views.generate_document,
        name="generate_document",
    ),
]
