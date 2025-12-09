from django.contrib import admin
from .models import Citizen, DocumentTemplate


@admin.register(Citizen)
class CitizenAdmin(admin.ModelAdmin):
    list_display = ("full_name", "identifier", "created_at")
    search_fields = ("full_name", "identifier")


@admin.register(DocumentTemplate)
class DocumentTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "output_type", "created_at")
    search_fields = ("name", "slug")
