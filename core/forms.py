from django import forms
from ckeditor.widgets import CKEditorWidget
from .models import Citizen, DocumentTemplate


class CitizenForm(forms.ModelForm):
    class Meta:
        model = Citizen
        fields = [
            "full_name", "identifier",
            "nume", "prenume", "cnp",
            "strada", "nr", "localitate", "judet",
            "telefon",
            "beneficiar", "emitent", "tip_document",
            "numar_document_extern", "data_emitere",
        ]
        widgets = {
            "full_name": forms.TextInput(attrs={"class": "form-control"}),
            "identifier": forms.TextInput(attrs={"class": "form-control"}),

            "nume": forms.TextInput(attrs={"class": "form-control"}),
            "prenume": forms.TextInput(attrs={"class": "form-control"}),
            "cnp": forms.TextInput(attrs={"class": "form-control"}),

            "strada": forms.TextInput(attrs={"class": "form-control"}),
            "nr": forms.TextInput(attrs={"class": "form-control"}),
            "localitate": forms.TextInput(attrs={"class": "form-control"}),
            "judet": forms.TextInput(attrs={"class": "form-control"}),

            "telefon": forms.TextInput(attrs={"class": "form-control"}),

            "beneficiar": forms.TextInput(attrs={"class": "form-control"}),
            "emitent": forms.TextInput(attrs={"class": "form-control"}),
            "tip_document": forms.TextInput(attrs={"class": "form-control"}),
            "numar_document_extern": forms.TextInput(attrs={"class": "form-control"}),

            "data_emitere": forms.DateInput(
                attrs={"class": "form-control", "type": "date"}
            ),
        }



class DocumentTemplateForm(forms.ModelForm):
    body_html = forms.CharField(widget=CKEditorWidget())

    class Meta:
        model = DocumentTemplate
        fields = ["name", "description", "output_type", "body_html"]