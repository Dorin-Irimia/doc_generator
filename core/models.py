from django.db import models
from django.utils.text import slugify
from django.contrib.auth.models import User


class Citizen(models.Model):
    full_name = models.CharField(max_length=200)
    identifier = models.CharField(
        max_length=50,
        blank=True,
        help_text="Ex: CNP sau un cod intern"
    )

    # date personale
    nume = models.CharField(max_length=100, blank=True)
    prenume = models.CharField(max_length=100, blank=True)
    cnp = models.CharField(max_length=13, blank=True)

    # adresa
    strada = models.CharField(max_length=200, blank=True)
    nr = models.CharField(max_length=20, blank=True)
    localitate = models.CharField(max_length=200, blank=True)
    judet = models.CharField(max_length=200, blank=True)

    telefon = models.CharField(max_length=50, blank=True)

    # document registration type
    beneficiar = models.CharField(max_length=200, blank=True)
    emitent = models.CharField(max_length=200, blank=True)
    tip_document = models.CharField(max_length=200, blank=True)
    numar_document_extern = models.CharField(max_length=200, blank=True)
    data_emitere = models.DateField(null=True, blank=True)

    # JSON automat (fallback)
    data = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.full_name

    def save(self, *args, **kwargs):
        # generăm JSON automat din câmpurile introduse
        self.data = {
            "nume": self.nume,
            "prenume": self.prenume,
            "cnp": self.cnp,
            "strada": self.strada,
            "nr": self.nr,
            "localitate": self.localitate,
            "judet": self.judet,
            "telefon": self.telefon,
            "beneficiar": self.beneficiar,
            "emitent": self.emitent,
            "tip_document": self.tip_document,
            "numar_document_extern": self.numar_document_extern,
            "data_emitere": str(self.data_emitere) if self.data_emitere else "",
        }
        super().save(*args, **kwargs)


class DocumentTemplate(models.Model):
    OUTPUT_CHOICES = [
        ("pdf", "PDF"),
        ("word", "Word (.doc pe bază de HTML)"),
    ]

    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True, blank=True)
    description = models.TextField(blank=True)
    body_html = models.TextField()
    output_type = models.CharField(
        max_length=10, choices=OUTPUT_CHOICES, default="pdf"
    )

    created_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)
