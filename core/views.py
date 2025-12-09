from django.shortcuts import render, get_object_or_404, redirect
from django.http import HttpResponse
from django.template import Template, Context
from django.urls import reverse
from django.contrib import messages


from django.http import HttpResponse
from django.template import Template, Context
from django.utils.html import escape
import tempfile



from .models import Citizen, DocumentTemplate
from .forms import CitizenForm, DocumentTemplateForm

from xhtml2pdf import pisa
import io

# ---- Cetățeni ----

def citizen_list(request):
    citizens = Citizen.objects.all().order_by("full_name")
    return render(request, "core/citizen_list.html", {"citizens": citizens})


def citizen_create(request):
    if request.method == "POST":
        form = CitizenForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Cetățean salvat.")
            return redirect("citizen_list")
    else:
        form = CitizenForm()
    return render(request, "core/citizen_form.html", {"form": form})


def citizen_edit(request, pk):
    citizen = get_object_or_404(Citizen, pk=pk)
    if request.method == "POST":
        form = CitizenForm(request.POST, instance=citizen)
        if form.is_valid():
            form.save()
            messages.success(request, "Cetățean actualizat.")
            return redirect("citizen_list")
    else:
        form = CitizenForm(instance=citizen)
    return render(request, "core/citizen_form.html", {"form": form, "citizen": citizen})


def citizen_delete(request, pk):
    citizen = get_object_or_404(Citizen, pk=pk)
    if request.method == "POST":
        citizen.delete()
        messages.success(request, "Cetățean șters.")
        return redirect("citizen_list")
    return render(request, "core/confirm_delete.html", {"object": citizen})


# ---- Template-uri ----

def template_list(request):
    templates = DocumentTemplate.objects.all()
    return render(request, "core/template_list.html", {"templates": templates})


def template_create(request):
    fields = [
        {"name": f.name, "placeholder": "{{ " + f.name + " }}"}
        for f in Citizen._meta.get_fields() if f.concrete
    ]

    form = DocumentTemplateForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)

        if request.user.is_authenticated:
            obj.created_by = request.user
        else:
            obj.created_by = None   # ← IMPORTANT

        obj.save()
        return redirect("template_list")

    return render(request, "core/template_form.html", {
        "form": form,
        "fields": fields,
    })



def template_edit(request, slug):
    tmpl = get_object_or_404(DocumentTemplate, slug=slug)
    if request.method == "POST":
        form = DocumentTemplateForm(request.POST, instance=tmpl)
        if form.is_valid():
            form.save()
            messages.success(request, "Template actualizat.")
            return redirect("template_list")
    else:
        form = DocumentTemplateForm(instance=tmpl)
    return render(request, "core/template_form.html", {"form": form, "tmpl": tmpl})


def template_delete(request, slug):
    tmpl = get_object_or_404(DocumentTemplate, slug=slug)
    if request.method == "POST":
        tmpl.delete()
        messages.success(request, "Template șters.")
        return redirect("template_list")
    return render(request, "core/confirm_delete.html", {"object": tmpl})


# ---- Generare document ----

def generate_select(request):
    citizens = Citizen.objects.all()
    templates = DocumentTemplate.objects.all()
    if request.method == "POST":
        citizen_id = request.POST.get("citizen_id")
        template_slug = request.POST.get("template_slug")
        return redirect(
            reverse("generate_document", args=[citizen_id, template_slug])
        )
    return render(
        request,
        "core/generate_select.html",
        {"citizens": citizens, "templates": templates},
    )


def generate_document(request, citizen_id, template_slug):
    citizen = get_object_or_404(Citizen, id=citizen_id)
    tmpl = get_object_or_404(DocumentTemplate, slug=template_slug)

    # Construim context din model
    context = {field.name: getattr(citizen, field.name) for field in Citizen._meta.fields}

    # Interpretăm template-ul HTML cu valorile citizen
    template = Template(tmpl.body_html)
    html_content = template.render(Context(context))

    # === GENERARE PDF CU XHTML2PDF ===
    if tmpl.output_type == "pdf":
        result = io.BytesIO()
        pisa_status = pisa.CreatePDF(html_content, dest=result)

        if pisa_status.err:
            return HttpResponse("Eroare la generarea PDF", status=500)

        response = HttpResponse(result.getvalue(), content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{tmpl.slug}.pdf"'
        return response

    # === GENERARE DOCUMENT WORD ===
    if tmpl.output_type == "word":
        response = HttpResponse(html_content, content_type="application/msword")
        response["Content-Disposition"] = f'attachment; filename="{tmpl.slug}.doc"'
        return response

    return HttpResponse("Tip document necunoscut.", status=400)
