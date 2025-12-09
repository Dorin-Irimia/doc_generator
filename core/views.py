import io
import secrets
import csv
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.mail import send_mail
from django.db import models
from django.http import HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template import Context, Template
from django.urls import reverse
from django.utils import timezone
from xhtml2pdf import pisa

from .forms import (
    CitizenForm,
    CitizenLoginForm,
    CitizenSelfForm,
    DocumentTemplateForm,
    ExtraFieldFormSet,
    AdminInviteForm,
    AdminAcceptForm,
    MunicipalityForm,
    SendTestEmailForm,
    SuperAdminRequestCodeForm,
    SuperAdminVerifyCodeForm,
    ForgotPasswordRequestForm,
    ForgotPasswordVerifyForm,
    ConfirmEmailForm,
    MunicipalityProfileForm,
    ImportCitizensForm,
    ImportTemplatesForm,
    parse_dynamic_fields,
)
from .models import (
    Citizen,
    DocumentTemplate,
    ExtraFieldDefinition,
    ExtraFieldValue,
    GeneratedDocument,
    Notification,
    Municipality,
    MunicipalityAdmin,
    AdminInvite,
    SuperAdminCode,
    Message,
    PasswordResetCode,
    EmailVerificationCode,
)


# Helpers -----------------------------------------------------------------


def _sync_user_account(citizen: Citizen, password: str | None = None):
    """
    Creeaza sau sincronizeaza user-ul pentru autentificare pe baza de CNP.
    Username = CNP; email nu este obligatoriu.
    """
    if not citizen.cnp:
        return citizen

    username = citizen.cnp
    user = citizen.user
    if user and user.username != username:
        user.username = username
    if not user:
        user, _ = User.objects.get_or_create(username=username)
        citizen.user = user

    if password:
        user.set_password(password)
    elif not user.has_usable_password():
        user.set_unusable_password()

    user.save()
    citizen.save(update_fields=["user"])
    return citizen


def _user_municipality(user):
    if not user.is_authenticated:
        return None
    if hasattr(user, "municipality_admin"):
        return user.municipality_admin.municipality
    return None


# ---- Cetateni -----------------------------------------------------------

@user_passes_test(lambda u: u.is_staff)
def citizen_list(request):
    muni = _user_municipality(request.user)
    qs = Citizen.objects.all().select_related("user")
    # adnotari pentru numarul de mesaje ne-citite trimise de cetatean
    qs = qs.annotate(
        msg_from_citizen=models.Count(
            "messages",
            filter=models.Q(messages__sender=models.F("user"), messages__read_by_staff=False),
        )
    )
    if muni:
        qs = qs.filter(municipality=muni)

    # filtre simple
    q = request.GET.get("q", "").strip()
    status_f = request.GET.get("status", "").strip()
    if q:
        qs = qs.filter(
            models.Q(full_name__icontains=q)
            | models.Q(cnp__icontains=q)
            | models.Q(identifier__icontains=q)
        )
    if status_f:
        qs = qs.filter(profile_status=status_f)

    # sortare
    sort = request.GET.get("sort", "")
    if sort == "messages":
        qs = qs.order_by("-msg_from_citizen", "full_name")
    else:
        qs = qs.order_by("full_name")

    if request.method == "POST":
        citizen_id = request.POST.get("citizen_id")
        new_status = request.POST.get("profile_status")
        if citizen_id and new_status:
            ctz = get_object_or_404(qs, pk=citizen_id)
            ctz.profile_status = new_status
            ctz.save(update_fields=["profile_status"])
            _notify_citizen(
                ctz,
                "Status profil actualizat",
                f"Statusul profilului tau este acum: {ctz.get_profile_status_display()}",
            )
            messages.success(request, "Status actualizat.")
            return redirect("citizen_list")
    citizens = qs
    status_choices = Citizen.STATUS_CHOICES
    return render(
        request,
        "core/citizen_list.html",
        {
            "citizens": citizens,
            "status_choices": status_choices,
            "q": q,
            "status_f": status_f,
            "sort": sort,
        },
    )


@user_passes_test(lambda u: u.is_staff)
def citizen_create(request):
    muni = _user_municipality(request.user)
    form = CitizenForm(request.POST or None, user=request.user)
    formset = ExtraFieldFormSet(request.POST or None, prefix="extra")

    if request.method == "POST" and form.is_valid() and formset.is_valid():
        citizen = form.save(commit=False)
        if muni:
            citizen.municipality = muni
        citizen.save()
        password = form.cleaned_data.get("password1") or None
        _sync_user_account(citizen, password)
        _process_extra_fields(citizen, formset)
        messages.success(request, "Cetatean salvat.")
        return redirect("citizen_list")

    return render(
        request,
        "core/citizen_form.html",
        {"form": form, "formset": formset, "citizen": None, "self_edit": False},
    )


@user_passes_test(lambda u: u.is_staff)
def citizen_edit(request, pk):
    citizen = get_object_or_404(Citizen, pk=pk)
    muni = _user_municipality(request.user)
    if muni and citizen.municipality != muni:
        return HttpResponse(status=403)
    old_status = citizen.profile_status
    initial_extra = [
        {"field_name": val.field_def.name, "field_value": val.value}
        for val in citizen.extra_values.select_related("field_def")
    ]
    form = CitizenForm(request.POST or None, instance=citizen, user=request.user)
    formset = ExtraFieldFormSet(
        request.POST or None, prefix="extra", initial=initial_extra
    )

    if request.method == "POST" and form.is_valid() and formset.is_valid():
        citizen_obj = form.save(commit=False)
        if muni:
            citizen_obj.municipality = muni
        citizen_obj.save()
        citizen = citizen_obj
        password = form.cleaned_data.get("password1") or None
        _sync_user_account(citizen, password)
        _process_extra_fields(citizen, formset)
        if old_status != citizen.profile_status:
            _notify_citizen(
                citizen,
                "Status profil actualizat",
                f"Statusul profilului tau este acum: {citizen.get_profile_status_display()}",
            )
        _notify_citizen(
            citizen,
            "Date actualizate",
            "Profilul tau a fost actualizat de operator.",
        )
        messages.success(request, "Cetatean actualizat.")
        return redirect("citizen_list")

    return render(
        request,
        "core/citizen_form.html",
        {"form": form, "formset": formset, "citizen": citizen, "self_edit": False},
    )


@user_passes_test(lambda u: u.is_staff)
def citizen_delete(request, pk):
    citizen = get_object_or_404(Citizen, pk=pk)
    muni = _user_municipality(request.user)
    if muni and citizen.municipality != muni:
        return HttpResponse(status=403)
    if request.method == "POST":
        if citizen.user:
            citizen.user.delete()
        citizen.delete()
        messages.success(request, "Cetatean sters.")
        return redirect("citizen_list")
    return render(request, "core/confirm_delete.html", {"object": citizen})


# ---- Autentificare cetatean --------------------------------------------

def citizen_login(request):
    fails = request.session.get("login_fails", 0)
    if fails >= 3:
        messages.error(request, "Prea multe incercari. Incearca mai tarziu sau foloseste 'Am uitat parola'.")
        return render(request, "core/login.html", {"form": CitizenLoginForm()})

    if request.user.is_authenticated and hasattr(request.user, "citizen_profile"):
        return redirect("citizen_dashboard")

    form = CitizenLoginForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        cnp = form.cleaned_data["cnp"]
        password = form.cleaned_data["password"]
        user = authenticate(request, username=cnp, password=password)
        if user:
            login(request, user)
            request.session["login_fails"] = 0
            return redirect("citizen_dashboard")
        fails += 1
        request.session["login_fails"] = fails
        if fails >= 3:
            messages.error(request, "Prea multe incercari. Incearca mai tarziu sau foloseste 'Am uitat parola'.")
        else:
            messages.error(request, "CNP sau parola incorecte.")

    return render(request, "core/login.html", {"form": form})


def citizen_logout(request):
    logout(request)
    return redirect("citizen_login")


def superadmin_request_code(request):
    form = SuperAdminRequestCodeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"]
        if email.lower() != settings.SUPER_ADMIN_EMAIL.lower():
            messages.error(request, "Email invalid pentru super admin.")
        else:
            code = f"{secrets.randbelow(10**6):06d}"
            SuperAdminCode.objects.create(code=code)
            send_mail(
                "Cod de autentificare",
                f"Codul tau este: {code}",
                settings.DEFAULT_FROM_EMAIL,
                [email],
                fail_silently=False,
            )
            messages.success(request, "Cod trimis pe email.")
            return redirect("superadmin_verify_code")
    return render(request, "core/superadmin_request.html", {"form": form})


def superadmin_verify_code(request):
    form = SuperAdminVerifyCodeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        code = form.cleaned_data["code"]
        obj = SuperAdminCode.objects.filter(is_used=False).order_by("-created_at").first()
        if obj and obj.code == code:
            obj.is_used = True
            obj.save(update_fields=["is_used"])
            user, _ = User.objects.get_or_create(
                username=settings.SUPER_ADMIN_EMAIL,
                defaults={"email": settings.SUPER_ADMIN_EMAIL},
            )
            user.is_staff = True
            user.is_superuser = True
            user.set_unusable_password()
            user.save()
            login(request, user)
            messages.success(request, "Autentificat ca super admin.")
            return redirect("citizen_list")
        messages.error(request, "Cod invalid.")
    return render(request, "core/superadmin_verify.html", {"form": form})


@user_passes_test(lambda u: u.is_superuser)
def admin_invite_create(request):
    form = AdminInviteForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"]
        municipality = form.cleaned_data["municipality"]
        token = secrets.token_hex(16)
        AdminInvite.objects.create(email=email, municipality=municipality, token=token)
        link = _absolute_url(reverse("admin_invite_accept", args=[token]), request)
        send_mail(
            "Invitatie administrator primarie",
            f"Ai fost invitat ca administrator pentru {municipality.name}. Seteaza parola aici: {link}",
            settings.DEFAULT_FROM_EMAIL,
            [email],
            fail_silently=False,
        )
        messages.success(request, "Invitatie trimisa.")
        return redirect("citizen_list")
    return render(request, "core/admin_invite_form.html", {"form": form})


def admin_invite_accept(request, token):
    invite = get_object_or_404(AdminInvite, token=token, used=False)
    form = AdminAcceptForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        password = form.cleaned_data["password1"]
        user, created = User.objects.get_or_create(
            username=invite.email,
            defaults={"email": invite.email},
        )
        user.is_staff = True
        user.set_password(password)
        user.save()
        MunicipalityAdmin.objects.update_or_create(
            user=user, defaults={"municipality": invite.municipality}
        )
        invite.used = True
        invite.used_at = timezone.now()
        invite.save(update_fields=["used", "used_at"])
        messages.success(request, "Cont creat. Te poti autentifica.")
        return redirect("citizen_login")
    return render(
        request,
        "core/admin_invite_accept.html",
        {"form": form, "municipality": invite.municipality},
    )


@user_passes_test(lambda u: u.is_superuser)
def municipality_create(request):
    form = MunicipalityForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Primarie adaugata.")
        return redirect("superadmin_overview")
    return render(request, "core/municipality_form.html", {"form": form})


@user_passes_test(lambda u: u.is_superuser)
def superadmin_send_test_email(request):
    form = SendTestEmailForm(request.POST or None)
    sent = False
    if request.method == "POST" and form.is_valid():
        to_email = form.cleaned_data["to_email"]
        try:
            send_mail(
                "Test Citizen Docs",
                "Acesta este un email de test din platforma Citizen Docs.",
                settings.DEFAULT_FROM_EMAIL,
                [to_email],
                fail_silently=False,
            )
            messages.success(request, f"Trimis catre {to_email}")
            sent = True
        except Exception as exc:
            messages.error(request, f"Eroare la trimitere: {exc}")
    return render(request, "core/send_test_email.html", {"form": form, "sent": sent})


@user_passes_test(lambda u: u.is_superuser)
def superadmin_overview(request):
    municipalities = (
        Municipality.objects.all()
        .prefetch_related("citizens", "templates")
        .order_by("name")
    )
    total_citizens = Citizen.objects.count()
    total_templates = DocumentTemplate.objects.count()
    return render(
        request,
        "core/superadmin_overview.html",
        {
            "municipalities": municipalities,
            "total_citizens": total_citizens,
            "total_templates": total_templates,
        },
    )


@user_passes_test(lambda u: u.is_superuser)
def superadmin_admins(request):
    admins_qs = MunicipalityAdmin.objects.select_related("user", "municipality").order_by(
        "municipality__name", "user__username"
    )
    summary = Municipality.objects.annotate(admin_count=models.Count("admins")).order_by("name")
    total_admins = admins_qs.count()

    if request.method == "POST":
        action = request.POST.get("action")
        admin_id = request.POST.get("admin_id")
        admin_obj = get_object_or_404(MunicipalityAdmin, pk=admin_id)
        user = admin_obj.user

        if user.is_superuser and action in {"deactivate", "delete"}:
            messages.error(request, "Nu poti dezactiva sau sterge super admin.")
            return redirect("superadmin_admins")

        if action == "deactivate":
            user.is_active = False
            user.save(update_fields=["is_active"])
            messages.success(request, f"Contul {user.username} a fost dezactivat.")
        elif action == "activate":
            user.is_active = True
            user.save(update_fields=["is_active"])
            messages.success(request, f"Contul {user.username} a fost activat.")
        elif action == "delete":
            username = user.username
            admin_obj.delete()
            user.delete()
            messages.success(request, f"Contul {username} a fost sters.")
        return redirect("superadmin_admins")

    return render(
        request,
        "core/superadmin_admins.html",
        {"admins": admins_qs, "summary": summary, "total_admins": total_admins},
    )


@login_required
def admin_account(request):
    muni = _user_municipality(request.user)
    if not muni:
        messages.error(request, "Nu exista o primarie asociata contului.")
        return redirect("home")

    form = MunicipalityProfileForm(
        request.POST or None, request.FILES or None, instance=muni
    )
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Datele primariei au fost salvate.")
        return redirect("admin_account")

    citizens_count = muni.citizens.count()
    documents_count = (
        GeneratedDocument.objects.filter(citizen__municipality=muni)
        .values("id")
        .count()
    )
    recent_docs = (
        GeneratedDocument.objects.filter(citizen__municipality=muni)
        .select_related("citizen", "template")
        .order_by("-created_at")[:5]
    )

    return render(
        request,
        "core/admin_account.html",
        {
            "form": form,
            "municipality": muni,
            "citizens_count": citizens_count,
            "documents_count": documents_count,
            "recent_docs": recent_docs,
        },
    )


def _send_reset_code(user, request):
    # gaseste citizen pentru email recuperare
    citizen = getattr(user, "citizen_profile", None)
    target_email = None
    if citizen and citizen.email_recuperare:
        target_email = citizen.email_recuperare
    elif user.email:
        target_email = user.email
    if not target_email:
        return False
    code = f"{secrets.randbelow(10**6):06d}"
    expires = timezone.now() + timezone.timedelta(minutes=15)
    PasswordResetCode.objects.create(user=user, code=code, expires_at=expires)
    send_mail(
        "Cod resetare parola",
        f"Codul tau este: {code} (expira in 15 minute).",
        settings.DEFAULT_FROM_EMAIL,
        [target_email],
        fail_silently=False,
    )
    return True


def forgot_password_request(request):
    form = ForgotPasswordRequestForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        cnp = form.cleaned_data["cnp"]
        try:
            citizen = Citizen.objects.get(cnp=cnp)
            user = citizen.user
        except Citizen.DoesNotExist:
            user = None
        if user and _send_reset_code(user, request):
            messages.success(request, "Cod trimis pe email (daca exista email de recuperare).")
            return redirect("forgot_password_verify")
        messages.error(request, "Nu exista email de recuperare setat pentru acest CNP.")
    return render(request, "core/forgot_password_request.html", {"form": form})


def forgot_password_verify(request):
    form = ForgotPasswordVerifyForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        cnp = form.cleaned_data["cnp"]
        code = form.cleaned_data["code"]
        try:
            citizen = Citizen.objects.get(cnp=cnp)
            user = citizen.user
        except Citizen.DoesNotExist:
            user = None
        if not user:
            messages.error(request, "CNP invalid.")
            return render(request, "core/forgot_password_verify.html", {"form": form})

        reset_obj = (
            PasswordResetCode.objects.filter(user=user, code=code, used=False)
            .order_by("-created_at")
            .first()
        )
        if not reset_obj or not reset_obj.is_valid():
            messages.error(request, "Cod invalid sau expirat.")
            return render(request, "core/forgot_password_verify.html", {"form": form})

        pwd = form.cleaned_data["password1"]
        user.set_password(pwd)
        user.save()
        reset_obj.used = True
        reset_obj.save(update_fields=["used"])
        messages.success(request, "Parola a fost resetata. Te poti autentifica.")
        return redirect("citizen_login")

    return render(request, "core/forgot_password_verify.html", {"form": form})


@login_required
def citizen_send_email_code(request):
    citizen = getattr(request.user, "citizen_profile", None)
    if not citizen:
        return HttpResponse(status=403)
    email = request.POST.get("email_recuperare", "").strip()
    if not email:
        messages.error(request, "Completeaza emailul de recuperare.")
        return redirect("citizen_self_edit")
    code = f"{secrets.randbelow(10**6):06d}"
    expires = timezone.now() + timezone.timedelta(minutes=30)
    EmailVerificationCode.objects.create(
        citizen=citizen,
        email=email,
        code=code,
        expires_at=expires,
    )
    send_mail(
        "Confirmare email recuperare",
        f"Codul tau este: {code} (expira in 30 minute).",
        settings.DEFAULT_FROM_EMAIL,
        [email],
        fail_silently=False,
    )
    request.session[f"email_verify_requested_{citizen.id}"] = email
    messages.success(request, f"Cod trimis catre {email}. Introdu-l in pagina 'Confirma email'.")
    return redirect("citizen_self_edit")


def confirm_email(request):
    citizen = getattr(request.user, "citizen_profile", None)
    if not citizen:
        return HttpResponse(status=403)

    current_email = citizen.email_recuperare or citizen.email_recuperare_pending
    email_value = current_email
    form = ConfirmEmailForm(request.POST or None, initial={"cnp": citizen.cnp})

    if request.method == "POST":
        email_post = request.POST.get("email", "").strip()
        action_send = "send_code" in request.POST
        action_verify = "verify_code" in request.POST

        if action_send:
            if not email_post:
                messages.error(request, "Completeaza emailul.")
            else:
                code_val = f"{secrets.randbelow(10**6):06d}"
                expires = timezone.now() + timezone.timedelta(minutes=30)
                EmailVerificationCode.objects.create(
                    citizen=citizen,
                    email=email_post,
                    code=code_val,
                    expires_at=expires,
                )
                citizen.email_recuperare_pending = email_post
                citizen.save(update_fields=["email_recuperare_pending"])
                send_mail(
                    "Confirmare email recuperare",
                    f"Codul tau este: {code_val} (expira in 30 minute).",
                    settings.DEFAULT_FROM_EMAIL,
                    [email_post],
                    fail_silently=False,
                )
                messages.success(request, f"Cod trimis catre {email_post}.")
                email_value = email_post

        elif action_verify and form.is_valid():
            cnp = form.cleaned_data["cnp"]
            code = form.cleaned_data["code"]
            if cnp != citizen.cnp:
                messages.error(request, "CNP-ul nu corespunde contului.")
            else:
                evc = (
                    EmailVerificationCode.objects.filter(citizen=citizen, code=code, used=False)
                    .order_by("-created_at")
                    .first()
                )
                if not evc or not evc.is_valid():
                    messages.error(request, "Cod invalid sau expirat.")
                else:
                    citizen.email_recuperare = evc.email
                    citizen.email_recuperare_verified = True
                    citizen.email_recuperare_pending = ""
                    citizen.save(update_fields=["email_recuperare", "email_recuperare_verified", "email_recuperare_pending"])
                    evc.used = True
                    evc.save(update_fields=["used"])
                    messages.success(request, "Email validat.")
                    return redirect("citizen_dashboard")

        current_email = citizen.email_recuperare_pending or citizen.email_recuperare or email_value

    return render(request, "core/confirm_email.html", {"form": form, "current_email": current_email, "email_value": email_value})


@login_required
def chat_thread(request, citizen_id=None):
    DELETE_TOKEN = "__DELETE_REQUEST__"
    if request.user.is_staff:
        citizen = get_object_or_404(Citizen, id=citizen_id) if citizen_id else None
        muni = _user_municipality(request.user)
        if citizen and muni and citizen.municipality != muni:
            return HttpResponse(status=403)
    else:
        citizen = getattr(request.user, "citizen_profile", None)
        if not citizen:
            return HttpResponse(status=403)

    if citizen is None:
        return HttpResponse(status=404)

    msgs = Message.objects.filter(citizen=citizen).select_related("sender").order_by("created_at")
    pending_delete = msgs.filter(text=DELETE_TOKEN).exists()

    # marcheaza ca citite
    if request.user.is_staff:
        msgs.filter(sender__is_staff=False, read_by_staff=False).update(read_by_staff=True)
    else:
        msgs.filter(sender__is_staff=True, read_by_citizen=False).update(read_by_citizen=True)

    if request.method == "POST":
        # cerere stergere initiaza adminul: adaugam marcaj, cetateanul confirma
        if request.user.is_staff and request.POST.get("request_delete"):
            if not pending_delete:
                Message.objects.create(
                    citizen=citizen,
                    sender=request.user,
                    text=DELETE_TOKEN,
                )
                _notify_citizen(
                    citizen,
                    "Solicitare stergere chat",
                    "Administratorul a cerut stergerea conversatiei. Confirma din fereastra de chat.",
                )
                messages.success(request, "Solicitarea a fost trimisa cetateanului.")
            else:
                messages.info(request, "Exista deja o solicitare in asteptare.")
            return redirect(request.path)

        # cetateanul confirma stergere: trimitem email cu istoricul si stergem
        if not request.user.is_staff and request.POST.get("confirm_delete_chat"):
            if pending_delete:
                # pregatim email cu conversatia (fara token)
                history = []
                for m in msgs.exclude(text=DELETE_TOKEN):
                    history.append(f"{m.created_at} - {m.sender.username}: {m.text or ''}")
                history_text = "\n".join(history) if history else "Conversatie goala."
                target_email = citizen.email_recuperare or (citizen.user.email if citizen.user else None)
                if target_email:
                    send_mail(
                        "Copie conversatie chat",
                        history_text,
                        settings.DEFAULT_FROM_EMAIL,
                        [target_email],
                        fail_silently=True,
                    )
                msgs.delete()
                messages.success(request, "Conversatia a fost stearsa.")
                return redirect(request.path)
            else:
                messages.error(request, "Nu exista o solicitare de stergere.")
                return redirect(request.path)

        text = request.POST.get("text", "").strip()
        attachment = request.FILES.get("attachment")
        if text or attachment:
            Message.objects.create(citizen=citizen, sender=request.user, text=text, attachment=attachment)
            _notify_citizen(citizen, "Mesaj nou", "Ai primit un mesaj in chat.")
            return redirect(request.path)

    return render(
        request,
        "core/chat_thread.html",
        {"citizen": citizen, "chat_messages": msgs.exclude(text=DELETE_TOKEN), "pending_delete": pending_delete},
    )


@login_required
def citizen_dashboard(request):
    citizen = getattr(request.user, "citizen_profile", None)
    if not citizen:
        messages.error(request, "Nu exista un profil de cetatean asociat.")
        return redirect("home")

    documents = citizen.documents.order_by("-created_at")[:20]
    notifications = citizen.notifications.all()[:20]
    staff_msg_count = Message.objects.filter(
        citizen=citizen, sender__is_staff=True, read_by_citizen=False
    ).count()
    return render(
        request,
        "core/dashboard.html",
        {
            "citizen": citizen,
            "documents": documents,
            "notifications": notifications,
            "staff_msg_count": staff_msg_count,
        },
    )


@login_required
def citizen_self_edit(request):
    citizen = getattr(request.user, "citizen_profile", None)
    if not citizen:
        messages.error(request, "Nu exista un profil de cetatean asociat.")
        return redirect("home")

    initial_extra = [
        {"field_name": val.field_def.name, "field_value": val.value}
        for val in citizen.extra_values.select_related("field_def")
    ]
    form = CitizenSelfForm(request.POST or None, instance=citizen)
    formset = ExtraFieldFormSet(
        request.POST or None, prefix="extra", initial=initial_extra
    )

    if request.method == "POST" and form.is_valid() and formset.is_valid():
        old_email = citizen.email_recuperare
        new_email = form.cleaned_data.get("email_recuperare", "")
        # verifica daca s-a cerut trimiterea codului pentru acest email
        session_key = f"email_verify_requested_{citizen.id}"
        requested_email = request.session.get(session_key)
        if new_email and new_email != requested_email:
            messages.error(request, "Apasa butonul de verificare email inainte de a salva.")
            return render(
                request,
                "core/citizen_form.html",
                {"form": form, "formset": formset, "citizen": citizen, "self_edit": True},
            )

        citizen = form.save()
        _process_extra_fields(citizen, formset)
        citizen.profile_status = "pending_validation"
        citizen.save(update_fields=["profile_status"])
        # daca email recuperare s-a schimbat, trimitem cod de confirmare
        if citizen.email_recuperare and citizen.email_recuperare != old_email:
            code = f"{secrets.randbelow(10**6):06d}"
            expires = timezone.now() + timezone.timedelta(minutes=30)
            EmailVerificationCode.objects.create(
                citizen=citizen,
                email=citizen.email_recuperare,
                code=code,
                expires_at=expires,
            )
            send_mail(
                "Confirmare email recuperare",
                f"Codul tau este: {code} (expira in 30 minute).",
                settings.DEFAULT_FROM_EMAIL,
                [citizen.email_recuperare],
                fail_silently=False,
            )
            citizen.email_recuperare_verified = False
            citizen.save(update_fields=["email_recuperare_verified"])
            # resetam flag-ul de verificare
            request.session.pop(session_key, None)
        _notify_citizen(
            citizen,
            "Date actualizate",
            "Ti-ai actualizat datele de profil.",
        )
        messages.success(request, "Profil actualizat.")
        return redirect("citizen_dashboard")

    return render(
        request,
        "core/citizen_form.html",
        {"form": form, "formset": formset, "citizen": citizen, "self_edit": True},
    )


# ---- Template-uri -------------------------------------------------------

def _available_template_fields():
    exclude_citizen = {"id", "data", "user", "created_at", "updated_at", "municipality"}
    citizen_fields = [
        {"name": f.name, "placeholder": "{{ " + f.name + " }}"}
        for f in Citizen._meta.get_fields()
        if getattr(f, "concrete", False) and f.name not in exclude_citizen
    ]
    extra_defs = ExtraFieldDefinition.objects.all()
    extra_fields = [
        {"name": d.name, "placeholder": "{{ " + d.name + " }}"} for d in extra_defs
    ]
    exclude_muni = {
        "id",
        "slug",
        "created_at",
        "templates",
        "citizens",
        "admins",
    }
    muni_fields = [
        {"name": "municipality_name", "placeholder": "{{ municipality_name }}"},
        {"name": "municipality_cif", "placeholder": "{{ municipality_cif }}"},
        {"name": "municipality_email", "placeholder": "{{ municipality_email }}"},
        {"name": "municipality_phone", "placeholder": "{{ municipality_phone }}"},
        {"name": "municipality_mayor", "placeholder": "{{ municipality_mayor }}"},
        {"name": "municipality_address", "placeholder": "{{ municipality_address }}"},
        {"name": "municipality_header_logo", "placeholder": "{{ municipality_header_logo }}"},
        {"name": "municipality_header_banner", "placeholder": "{{ municipality_header_banner }}"},
    ]
    # includem si campuri brute din model pentru completare avansata
    muni_model_fields = [
        {"name": f.name, "placeholder": "{{ " + f.name + " }}"}
        for f in Municipality._meta.get_fields()
        if getattr(f, "concrete", False) and f.name not in exclude_muni
    ]
    muni_fields.extend(muni_model_fields)
    return citizen_fields + extra_fields, muni_fields


@user_passes_test(lambda u: u.is_staff)
def template_list(request):
    muni = _user_municipality(request.user)
    templates = DocumentTemplate.objects.all()
    if muni:
        templates = templates.filter(models.Q(municipalities=muni) | models.Q(municipalities__isnull=True)).distinct()
    return render(request, "core/template_list.html", {"templates": templates})


@user_passes_test(lambda u: u.is_staff)
def template_create(request):
    form = DocumentTemplateForm(request.POST or None, user=request.user)
    citizen_fields, muni_fields = _available_template_fields()
    header_logo_url = ""
    header_banner_url = ""
    user_muni = _user_municipality(request.user)
    if user_muni:
        if user_muni.header_logo:
            header_logo_url = _absolute_url(user_muni.header_logo.url)
        if user_muni.header_banner:
            header_banner_url = _absolute_url(user_muni.header_banner.url)

    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.created_by = request.user if request.user.is_authenticated else None
        obj.dynamic_fields = form.cleaned_data.get("dynamic_fields", [])
        obj.save()
        if request.user.is_superuser:
            form.save_m2m()
        else:
            muni = _user_municipality(request.user)
            if muni:
                obj.municipalities.set([muni])
        return redirect("template_list")

    return render(
        request,
        "core/template_form.html",
        {
            "form": form,
            "fields_citizen": citizen_fields,
            "fields_muni": muni_fields,
            "tmpl": None,
            "header_logo_url": header_logo_url,
            "header_banner_url": header_banner_url,
        },
    )


def template_edit(request, slug):
    tmpl = get_object_or_404(DocumentTemplate, slug=slug)
    muni = _user_municipality(request.user)
    if muni and not (tmpl.municipalities.filter(id=muni.id).exists() or tmpl.municipalities.count() == 0):
        return HttpResponse(status=403)
    form = DocumentTemplateForm(request.POST or None, instance=tmpl, user=request.user)
    citizen_fields, muni_fields = _available_template_fields()
    header_logo_url = ""
    header_banner_url = ""
    if muni:
        if muni.header_logo:
            header_logo_url = _absolute_url(muni.header_logo.url)
        if muni.header_banner:
            header_banner_url = _absolute_url(muni.header_banner.url)

    if request.method == "POST" and form.is_valid():
        tmpl = form.save(commit=False)
        tmpl.dynamic_fields = form.cleaned_data.get("dynamic_fields", [])
        tmpl.save()
        if request.user.is_superuser:
            form.save_m2m()
        else:
            if muni:
                tmpl.municipalities.set([muni])
        messages.success(request, "Template actualizat.")
        return redirect("template_list")

    return render(
        request,
        "core/template_form.html",
        {
            "form": form,
            "tmpl": tmpl,
            "fields_citizen": citizen_fields,
            "fields_muni": muni_fields,
            "header_logo_url": header_logo_url,
            "header_banner_url": header_banner_url,
        },
    )


def template_delete(request, slug):
    tmpl = get_object_or_404(DocumentTemplate, slug=slug)
    muni = _user_municipality(request.user)
    if muni and not (tmpl.municipalities.filter(id=muni.id).exists() or tmpl.municipalities.count() == 0):
        return HttpResponse(status=403)
    if request.method == "POST":
        tmpl.delete()
        messages.success(request, "Template sters.")
        return redirect("template_list")
    return render(request, "core/confirm_delete.html", {"object": tmpl})


# ---- Generare document --------------------------------------------------

@user_passes_test(lambda u: u.is_staff)
def export_citizens(request):
    muni = _user_municipality(request.user)
    qs = Citizen.objects.all()
    if muni:
        qs = qs.filter(municipality=muni)
    def row_iter():
        header = ["full_name","identifier","nume","prenume","cnp","strada","nr","localitate","judet","telefon","email_recuperare","beneficiar","emitent","tip_document","numar_document_extern","data_emitere"]
        yield ",".join(header) + "\n"
        for c in qs:
            vals = [
                c.full_name or "",
                c.identifier or "",
                c.nume or "",
                c.prenume or "",
                c.cnp or "",
                c.strada or "",
                c.nr or "",
                c.localitate or "",
                c.judet or "",
                c.telefon or "",
                c.email_recuperare or "",
                c.beneficiar or "",
                c.emitent or "",
                c.tip_document or "",
                c.numar_document_extern or "",
                c.data_emitere.isoformat() if c.data_emitere else "",
            ]
            yield ",".join([v.replace(",", " ") for v in vals]) + "\n"
    resp = StreamingHttpResponse(row_iter(), content_type="text/csv")
    resp["Content-Disposition"] = 'attachment; filename="citizens.csv"'
    return resp


@user_passes_test(lambda u: u.is_staff)
def import_citizens(request):
    form = ImportCitizensForm(request.POST or None, request.FILES or None, user=request.user)
    session_key = "import_citizens_data"

    # Daca suntem in pasul de confirmare (choice doar), refolosim continutul din sesiune
    if request.method == "POST" and not request.FILES and request.POST.get("choice") and request.session.get(session_key):
        cached = request.session.get(session_key)
        decoded = cached.get("csv_text", "")
        muni_id = cached.get("muni_id")
        choice = request.POST.get("choice")
        skip = choice == "skip"
        overwrite = choice == "overwrite"
        muni = _user_municipality(request.user)
        if request.user.is_superuser and muni_id:
            muni = Municipality.objects.filter(pk=muni_id).first() or muni
        reader = list(csv.DictReader(decoded.splitlines()))
        count = 0
        for row in reader:
            cnp = row.get("cnp") or None
            if cnp and Citizen.objects.filter(cnp=cnp).exists() and skip:
                continue
            date_emitere = row.get("data_emitere") or None
            if date_emitere:
                try:
                    date_emitere = timezone.datetime.fromisoformat(date_emitere).date()
                except Exception:
                    date_emitere = None
            Citizen.objects.update_or_create(
                cnp=cnp,
                defaults={
                    "full_name": row.get("full_name",""),
                    "identifier": row.get("identifier",""),
                    "nume": row.get("nume",""),
                    "prenume": row.get("prenume",""),
                    "strada": row.get("strada",""),
                    "nr": row.get("nr",""),
                    "localitate": row.get("localitate",""),
                    "judet": row.get("judet",""),
                    "telefon": row.get("telefon",""),
                    "email_recuperare": row.get("email_recuperare",""),
                    "beneficiar": row.get("beneficiar",""),
                    "emitent": row.get("emitent",""),
                    "tip_document": row.get("tip_document",""),
                    "numar_document_extern": row.get("numar_document_extern",""),
                    "data_emitere": date_emitere,
                    "municipality": muni,
                },
            )
            count += 1
        messages.success(request, f"Importat {count} cetateni.")
        request.session.pop(session_key, None)
        return redirect("citizen_list")

    if request.method == "POST" and form.is_valid():
        file = form.cleaned_data["file"]
        muni = _user_municipality(request.user)
        if request.user.is_superuser:
            muni = form.cleaned_data.get("municipality") or muni
        decoded_text = file.read().decode("utf-8")
        reader = list(csv.DictReader(decoded_text.splitlines()))
        existing_cnps = []
        for row in reader:
            cnp_val = (row.get("cnp") or "").strip()
            if cnp_val and Citizen.objects.filter(cnp=cnp_val).exists():
                existing_cnps.append(cnp_val)
        overwrite = request.POST.get("overwrite")
        skip = request.POST.get("skip")
        if existing_cnps and not overwrite and not skip:
            request.session[session_key] = {"csv_text": decoded_text, "muni_id": muni.id if muni else None}
            return render(
                request,
                "core/import_citizens_confirm.html",
                {
                    "form": form,
                    "duplicates": existing_cnps,
                    "file_name": file.name,
                },
            )

        count = 0
        for row in reader:
            cnp = row.get("cnp") or None
            if cnp and Citizen.objects.filter(cnp=cnp).exists() and skip:
                continue
            date_emitere = row.get("data_emitere") or None
            if date_emitere:
                try:
                    date_emitere = timezone.datetime.fromisoformat(date_emitere).date()
                except Exception:
                    date_emitere = None
            Citizen.objects.update_or_create(
                cnp=cnp,
                defaults={
                    "full_name": row.get("full_name",""),
                    "identifier": row.get("identifier",""),
                    "nume": row.get("nume",""),
                    "prenume": row.get("prenume",""),
                    "strada": row.get("strada",""),
                    "nr": row.get("nr",""),
                    "localitate": row.get("localitate",""),
                    "judet": row.get("judet",""),
                    "telefon": row.get("telefon",""),
                    "email_recuperare": row.get("email_recuperare",""),
                    "beneficiar": row.get("beneficiar",""),
                    "emitent": row.get("emitent",""),
                    "tip_document": row.get("tip_document",""),
                    "numar_document_extern": row.get("numar_document_extern",""),
                    "data_emitere": date_emitere,
                    "municipality": muni,
                },
            )
            count += 1
        messages.success(request, f"Importat {count} cetateni.")
        return redirect("citizen_list")
    return render(request, "core/import_citizens.html", {"form": form})


@user_passes_test(lambda u: u.is_staff)
def export_templates(request):
    muni = _user_municipality(request.user)
    qs = DocumentTemplate.objects.all()
    if muni:
        qs = qs.filter(models.Q(municipalities=muni) | models.Q(municipalities__isnull=True)).distinct()
    def row_iter():
        header = ["name","description","output_type","body_html","dynamic_fields"]
        yield ",".join(header) + "\n"
        for t in qs:
            dyn = ""
            if t.dynamic_fields:
                dyn = ";".join([f"{d.get('key','')}|{d.get('label','')}|{d.get('length',10)}" for d in t.dynamic_fields])
            vals = [
                t.name.replace(",", " "),
                (t.description or "").replace(",", " "),
                t.output_type,
                (t.body_html or "").replace("\\n"," ").replace(","," "),
                dyn,
            ]
            yield ",".join(vals) + "\n"
    resp = StreamingHttpResponse(row_iter(), content_type="text/csv")
    resp["Content-Disposition"] = 'attachment; filename="templates.csv"'
    return resp


@user_passes_test(lambda u: u.is_staff)
def import_templates(request):
    form = ImportTemplatesForm(request.POST or None, request.FILES or None, user=request.user)
    if request.method == "POST" and form.is_valid():
        file = form.cleaned_data["file"]
        muni = _user_municipality(request.user)
        if request.user.is_superuser:
            muni = form.cleaned_data.get("municipality") or muni
        decoded = file.read().decode("utf-8").splitlines()
        reader = csv.DictReader(decoded)
        count = 0
        for row in reader:
            name = row.get("name","")
            body = row.get("body_html","")
            dyn_raw = row.get("dynamic_fields","")
            dyn = []
            if dyn_raw:
                # dynamic stored as ; separated of key|label|length
                lines = dyn_raw.split(";")
                dyn = parse_dynamic_fields("\\n".join(lines))
            tmpl, _ = DocumentTemplate.objects.update_or_create(
                name=name,
                defaults={
                    "description": row.get("description",""),
                    "output_type": row.get("output_type","pdf"),
                    "body_html": body,
                    "dynamic_fields": dyn,
                },
            )
            if muni:
                tmpl.municipalities.set([muni])
            count += 1
        messages.success(request, f"Importat {count} template-uri.")
        return redirect("template_list")
    return render(request, "core/import_templates.html", {"form": form})

def generate_select(request):
    # doar staff poate genera pentru altii
    if not request.user.is_staff:
        return redirect("citizen_request_document")

    base_muni = _user_municipality(request.user)
    selected_muni = base_muni
    municipalities = None
    if request.user.is_superuser:
        municipalities = Municipality.objects.all().order_by("name")
        selected_muni_id = request.POST.get("municipality_id") or request.GET.get("municipality_id")
        if selected_muni_id:
            selected_muni = Municipality.objects.filter(pk=selected_muni_id).first()

    citizens = Citizen.objects.all()
    if selected_muni:
        citizens = citizens.filter(municipality=selected_muni)
    templates = DocumentTemplate.objects.all()
    if selected_muni:
        templates = templates.filter(models.Q(municipalities=selected_muni) | models.Q(municipalities__isnull=True)).distinct()
    if request.method == "POST":
        citizen_id = request.POST.get("citizen_id")
        template_slug = request.POST.get("template_slug")
        target = get_object_or_404(Citizen, id=citizen_id)
        if request.user.is_superuser and not selected_muni:
            messages.error(request, "Selecteaza mai intai institutia.")
            return redirect("generate_select")
        if target.profile_status == "pending_validation":
            messages.error(request, "Profilul acestui cetatean asteapta validare. Nu se pot genera documente.")
            return redirect("generate_select")
        url = reverse("generate_document", args=[citizen_id, template_slug])
        if request.user.is_superuser and selected_muni:
            url = f"{url}?municipality_id={selected_muni.id}"
        return redirect(url)
    return render(
        request,
        "core/generate_select.html",
        {
            "citizens": citizens,
            "templates": templates,
            "municipalities": municipalities,
            "selected_muni": selected_muni,
        },
    )


@login_required
def citizen_request_document(request):
    citizen = getattr(request.user, "citizen_profile", None)
    if not citizen:
        return redirect("home")
    if citizen.profile_status == "pending_validation":
        messages.error(request, "Profilul tau asteapta validarea administratorului. Nu poti genera documente acum.")
        return redirect("citizen_dashboard")
    templates = DocumentTemplate.objects.all()
    if citizen.municipality:
        templates = templates.filter(models.Q(municipalities=citizen.municipality) | models.Q(municipalities__isnull=True)).distinct()
    if request.method == "POST":
        template_slug = request.POST.get("template_slug")
        return redirect(reverse("generate_document", args=[citizen.id, template_slug]))
    return render(
        request,
        "core/generate_select.html",
        {"citizens": [citizen], "templates": templates, "citizen_mode": True},
    )


def generate_document(request, citizen_id, template_slug):
    citizen = get_object_or_404(Citizen, id=citizen_id)
    tmpl = get_object_or_404(DocumentTemplate, slug=template_slug)

    # restrict cetateanul sa genereze doar pentru el insusi
    if request.user.is_authenticated and not request.user.is_staff:
        prof = getattr(request.user, "citizen_profile", None)
        if not prof or prof.id != citizen.id:
            return HttpResponse(status=403)
        if citizen.profile_status == "pending_validation":
            messages.error(request, "Profilul tau asteapta validarea administratorului. Nu poti genera documente.")
            return redirect("citizen_dashboard")

    # restrict adminul de primarie la cetatenii proprii
    muni = _user_municipality(request.user)
    if muni and citizen.municipality != muni:
        return HttpResponse(status=403)

    context = citizen.build_data_payload(include_extra=True)
    context["full_name"] = citizen.full_name
    context["identifier"] = citizen.identifier
    # data curenta
    context["current_date"] = timezone.now().date()

    # replace lipsa cu underline
    safe_context = {}
    for k, v in context.items():
        val = v if v not in [None, "None"] else ""
        if val == "" or val is None:
            safe_context[k] = "____________________"
        else:
            safe_context[k] = val

    muni = citizen.municipality
    # superadmin poate specifica explicit primaria (query param) ca sa foloseasca antetul corect
    if request.user.is_superuser:
        override_muni_id = request.GET.get("municipality_id")
        if override_muni_id:
            muni = Municipality.objects.filter(pk=override_muni_id).first() or muni
    if muni:
        safe_context["municipality_name"] = muni.name
        safe_context["municipality_cif"] = muni.cif
        safe_context["municipality_email"] = muni.email
        safe_context["municipality_phone"] = muni.phone
        safe_context["municipality_mayor"] = muni.mayor_name
        safe_context["municipality_address"] = (
            f"{muni.street} {muni.number}, {muni.city}, {muni.county} {muni.postal_code}".strip()
        )
        # LOGO – folosim PATH LOCAL, nu URL
        if muni.header_logo:
            safe_context["municipality_header_logo"] = f"file://{muni.header_logo.path}"
        else:
            safe_context["municipality_header_logo"] = ""

        # BANNER – tot PATH LOCAL
        if muni.header_banner:
            safe_context["municipality_header_banner"] = f"file://{muni.header_banner.path}"
        else:
            safe_context["municipality_header_banner"] = ""


    # campuri dinamice: daca exista si e GET, cere completare; daca POST, aplica valori sau underline
    dyn_fields = getattr(tmpl, "dynamic_fields", []) or []
    if dyn_fields:
        if request.method == "GET":
            return render(
                request,
                "core/template_fill.html",
                {"dynamic_fields": dyn_fields, "citizen": citizen, "template": tmpl},
            )
        # POST: colectam valorile
        for item in dyn_fields:
            key = item.get("key")
            underline = "_" * int(item.get("length", 10))
            val = request.POST.get(key, "").strip()
            safe_context[key] = val if val else underline

    # inlocuieste eventualele placeholdere scapati cu backslash
    body = tmpl.body_html.replace("\\{\\{", "{{").replace("\\}\\}", "}}")
    template = Template(body)
    html_content = template.render(Context(safe_context))

    # adaugam automat antet cu pozele institutiei; daca nu exista imagini, pastram locul pentru etichete
    header_html = ""
    logo_src = safe_context.get("municipality_header_logo", "")
    banner_src = safe_context.get("municipality_header_banner", "")
    # dimensiuni: eticheta 52mm x 32mm (~5.2cm x 3.2cm); bara 40mm latime x 32mm inaltime
    label_style = "height:32mm; width:52mm; object-fit:contain; display:block; margin:0 auto;"
    bar_style = "height:32mm; width:40mm; background:#444; margin:0 auto; display:block;"
    header_html = f"""
    <table style="width:100%; margin-bottom:12px; table-layout:fixed;">
      <tr style="height:32mm;">
        <td style="width:35%; text-align:center; vertical-align:middle; overflow:hidden;">
          {f'<img src="{logo_src}" alt="Sigla" style="{label_style}">' if logo_src else f'<div style="{label_style} border:1px dashed #bbb;"></div>'}
        </td>
        <td style="width:30%; text-align:center; vertical-align:middle; overflow:hidden;">
          <div style="{bar_style}"></div>
        </td>
        <td style="width:35%; text-align:center; vertical-align:middle; overflow:hidden;">
          {f'<img src="{banner_src}" alt="Banner" style="{label_style}">' if banner_src else f'<div style="{label_style} border:1px dashed #bbb;"></div>'}
        </td>
      </tr>
    </table>
    """
    html_content = header_html + html_content

    filename = f"{tmpl.slug}_{citizen.id}"

    if tmpl.output_type == "pdf":
        result = io.BytesIO()
        pisa_status = pisa.CreatePDF(html_content, dest=result)
        if pisa_status.err:
            return HttpResponse("Eroare la generarea PDF", status=500)

        saved = GeneratedDocument.objects.create(
            citizen=citizen,
            template=tmpl,
            output_type="pdf",
        )
        saved.file.save(f"{filename}.pdf", ContentFile(result.getvalue()))
        _notify_citizen(
            citizen,
            "Document nou",
            f"A fost generat documentul {tmpl.name}.",
        )

        response = HttpResponse(result.getvalue(), content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{tmpl.slug}.pdf"'
        return response

    if tmpl.output_type == "word":
        saved = GeneratedDocument.objects.create(
            citizen=citizen,
            template=tmpl,
            output_type="word",
        )
        saved.file.save(f"{filename}.doc", ContentFile(html_content.encode("utf-8")))
        _notify_citizen(
            citizen,
            "Document nou",
            f"A fost generat documentul {tmpl.name}.",
        )

        response = HttpResponse(html_content, content_type="application/msword")
        response["Content-Disposition"] = f'attachment; filename="{tmpl.slug}.doc"'
        return response

    return HttpResponse("Tip document necunoscut.", status=400)

@login_required
def document_preview(request, doc_id):
    doc = get_object_or_404(GeneratedDocument, id=doc_id)
    # permisiuni
    if request.user.is_staff:
        muni = _user_municipality(request.user)
        if muni and doc.citizen.municipality != muni:
            return HttpResponse(status=403)
    else:
        citizen = getattr(request.user, "citizen_profile", None)
        if not citizen or citizen.id != doc.citizen_id:
            return HttpResponse(status=403)
    if not doc.file:
        return HttpResponse("Fisier indisponibil", status=404)
    doc.file.open("rb")
    data = doc.file.read()
    doc.file.close()
    content_type = "application/pdf" if doc.output_type == "pdf" else "application/msword"
    ext = "pdf" if doc.output_type == "pdf" else "doc"
    resp = HttpResponse(data, content_type=content_type)
    resp["Content-Disposition"] = f'inline; filename="{doc.template.slug}.{ext}"'
    return resp


# ---- Extra helpers ------------------------------------------------------

def _process_extra_fields(citizen: Citizen, formset: ExtraFieldFormSet):
    seen_ids = []
    for form in formset:
        if not form.cleaned_data or form.cleaned_data.get("DELETE"):
            continue
        name = form.cleaned_data["field_name"].strip()
        value = form.cleaned_data.get("field_value", "")
        if not name:
            continue
        field_def, _ = ExtraFieldDefinition.objects.get_or_create(
            name=name, defaults={"label": name}
        )
        val_obj, _ = ExtraFieldValue.objects.update_or_create(
            citizen=citizen, field_def=field_def, defaults={"value": value}
        )
        seen_ids.append(val_obj.field_def_id)

    ExtraFieldValue.objects.filter(citizen=citizen).exclude(
        field_def_id__in=seen_ids
    ).delete()
    citizen.refresh_data_cache()


def _notify_citizen(citizen: Citizen, title: str, message: str):
    Notification.objects.create(citizen=citizen, title=title, message=message)
    if citizen.user and citizen.user.email:
        send_mail(
            title,
            message,
            settings.DEFAULT_FROM_EMAIL,
            [citizen.user.email],
            fail_silently=False,
        )
def _absolute_url(path: str, request=None):
    base = settings.SITE_BASE_URL
    if base:
        return f"{base}{path}"
    if request:
        return request.build_absolute_uri(path)
    return path
