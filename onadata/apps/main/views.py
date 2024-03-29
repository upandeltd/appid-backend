# coding: utf-8
from __future__ import unicode_literals, print_function, division, absolute_import

import json
import os
from datetime import datetime

import requests
from bson import json_util
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.core.files.storage import default_storage
from django.core.files.storage import get_storage_class
from django.core.urlresolvers import reverse
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.http import HttpResponseBadRequest
from django.http import HttpResponseForbidden
from django.http import HttpResponseNotFound
from django.http import HttpResponseRedirect
from django.http import HttpResponseServerError
from django.shortcuts import get_object_or_404
from django.shortcuts import render
from django.template import loader, RequestContext
from django.utils.translation import ugettext as _
from django.views.decorators.http import require_GET
from django.views.decorators.http import require_POST
from django.views.decorators.http import require_http_methods
from guardian.shortcuts import assign_perm, remove_perm, get_users_with_perms
from rest_framework.authtoken.models import Token
from rest_framework import status
from ssrf_protect.ssrf_protect import SSRFProtect, SSRFProtectException

from onadata.apps.logger.models import Instance, XForm
from onadata.apps.logger.views import enter_data
from onadata.apps.main.forms import UserProfileForm, FormLicenseForm, \
    DataLicenseForm, SupportDocForm, QuickConverterFile, QuickConverterURL, \
    QuickConverter, SourceForm, PermissionForm, MediaForm, ActivateSMSSupportFom
from onadata.apps.main.models import AuditLog, UserProfile, MetaData
from onadata.apps.sms_support.autodoc import get_autodoc_for
from onadata.apps.sms_support.providers import providers_doc
from onadata.apps.sms_support.tools import check_form_sms_compatibility, \
    is_sms_related
from onadata.apps.viewer.models.data_dictionary import DataDictionary, \
    upload_to
from onadata.apps.viewer.models.parsed_instance import \
    DATETIME_FORMAT, ParsedInstance
from onadata.libs.constants import (
    CAN_CHANGE_XFORM,
    CAN_VALIDATE_XFORM,
    CAN_DELETE_DATA_XFORM,
    CAN_VIEW_XFORM,
    CAN_ADD_SUBMISSIONS,
)
from onadata.libs.utils.decorators import is_owner
from onadata.libs.utils.log import audit_log, Actions
from onadata.libs.utils.logger_tools import response_with_mimetype_and_name, \
    publish_form
from onadata.libs.utils.qrcode import generate_qrcode
from onadata.libs.utils.user_auth import (
    add_cors_headers,
    check_and_set_user,
    check_and_set_user_and_form,
    get_xform_and_perms,
    has_delete_data_permission,
    has_permission,
    helper_auth_helper,
    set_profile_data,
)
from onadata.libs.utils.viewer_tools import enketo_url


def home(request):
    if request.user.username:
        return HttpResponseRedirect(
            reverse(profile, kwargs={'username': request.user.username}))

    return render(request, 'home.html')


@login_required
def login_redirect(request):
    return HttpResponseRedirect(reverse(profile,
                                kwargs={'username': request.user.username}))


@require_POST
@login_required
def clone_xlsform(request, username):
    """
    Copy a public/Shared form to a users list of forms.
    Eliminates the need to download Excel File and upload again.
    """
    to_username = request.user.username
    message = {'type': None, 'text': '....'}
    message_list = []

    def set_form():
        form_owner = request.POST.get('username')
        id_string = request.POST.get('id_string')
        xform = XForm.objects.get(user__username__iexact=form_owner,
                                  id_string__exact=id_string)
        if len(id_string) > 0 and id_string[0].isdigit():
            id_string = '_' + id_string
        path = xform.xls.name
        if default_storage.exists(path):
            xls_file = upload_to(None, '%s%s.xls' % (
                                 id_string, XForm.CLONED_SUFFIX), to_username)
            xls_data = default_storage.open(path)
            xls_file = default_storage.save(xls_file, xls_data)
            survey = DataDictionary.objects.create(
                user=request.user,
                xls=xls_file
            ).survey
            # log to cloner's account
            audit = {}
            audit_log(
                Actions.FORM_CLONED, request.user, request.user,
                _("Cloned form '%(id_string)s'.") %
                {
                    'id_string': survey.id_string,
                }, audit, request)
            clone_form_url = reverse(
                show, kwargs={
                    'username': to_username,
                    'id_string': xform.id_string + XForm.CLONED_SUFFIX})
            return {
                'type': 'alert-success',
                'text': _('Successfully cloned to %(form_url)s into your '
                          '%(profile_url)s') %
                {'form_url': '<a href="%(url)s">%(id_string)s</a> ' % {
                 'id_string': survey.id_string,
                 'url': clone_form_url
                 },
                    'profile_url': '<a href="%s">profile</a>.' %
                    reverse(profile, kwargs={'username': to_username})}
            }
    form_result = publish_form(set_form)
    if form_result['type'] == 'alert-success':
        # comment the following condition (and else)
        # when we want to enable sms check for all.
        # until then, it checks if form barely related to sms
        if is_sms_related(form_result.get('form_o')):
            form_result_sms = check_form_sms_compatibility(form_result)
            message_list = [form_result, form_result_sms]
        else:
            message = form_result
    else:
        message = form_result

    context = RequestContext(request, {
        'message': message, 'message_list': message_list})

    if request.is_ajax():
        res = loader.render_to_string(
            'message.html',
            context_instance=context
        ).replace("'", r"\'").replace('\n', '')

        return HttpResponse(
            "$('#mfeedback').html('%s').show();" % res)
    else:
        return HttpResponse(message['text'])


def profile(request, username):
    content_user = get_object_or_404(User, username__iexact=username)
    form = QuickConverter()
    data = {'form': form}

    # xlsform submission...
    if request.method == 'POST' and request.user.is_authenticated():
        def set_form():
            form = QuickConverter(request.POST, request.FILES)
            survey = form.publish(request.user).survey
            audit = {}
            audit_log(
                Actions.FORM_PUBLISHED, request.user, content_user,
                _("Published form '%(id_string)s'.") %
                {
                    'id_string': survey.id_string,
                }, audit, request)
            enketo_webform_url = reverse(
                enter_data,
                kwargs={'username': username, 'id_string': survey.id_string}
            )
            return {
                'type': 'alert-success',
                'preview_url': reverse(enketo_preview, kwargs={
                    'username': username,
                    'id_string': survey.id_string
                }),
                'text': _('Successfully published %(form_id)s.'
                          ' <a href="%(form_url)s">Enter Web Form</a>'
                          ' or <a href="#preview-modal" data-toggle="modal">'
                          'Preview Web Form</a>')
                % {'form_id': survey.id_string,
                    'form_url': enketo_webform_url},
                'form_o': survey
            }
        with transaction.atomic():
            # publish_form must be wrapped into `transaction.atomic` because of
            # https://stackoverflow.com/a/23326971/1141214
            form_result = publish_form(set_form)

        if form_result['type'] == 'alert-success':
            # comment the following condition (and else)
            # when we want to enable sms check for all.
            # until then, it checks if form barely related to sms
            if is_sms_related(form_result.get('form_o')):
                form_result_sms = check_form_sms_compatibility(form_result)
                data['message_list'] = [form_result, form_result_sms]
            else:
                data['message'] = form_result
        else:
            data['message'] = form_result

    # If the "Sync forms" button is pressed in the UI, a call to KPI's
    # `/migrate` endpoint is made to sync kobocat and KPI.
    elif request.GET.get('sync_xforms') == 'true':
        migrate_response = _make_authenticated_request(request, content_user)
        message = {}
        if migrate_response.status_code == status.HTTP_200_OK:
            message['text'] = _(
                'The migration process has started and may take several '
                'minutes. Please check the project list in the '
                '<a href={}>new interface</a> and ensure your projects have '
                'synced.'
            ).format(settings.KOBOFORM_URL)
        else:
            message['text'] = _(
                'Something went wrong trying to migrate your forms. Please try '
                'again or reach out on the <a'
                'href="https://community.kobotoolbox.org/">community forum</a> '
                'for assistance.'
            )

        data['message'] = message

    # profile view...
    # for the same user -> dashboard
    if content_user == request.user:
        show_dashboard = True
        all_forms = content_user.xforms.count()
        form = QuickConverterFile()
        form_url = QuickConverterURL()

        request_url = request.build_absolute_uri(
            "/%s" % request.user.username)
        url = request_url.replace('http://', 'https://')
        xforms = XForm.objects.filter(user=content_user)\
            .select_related('user', 'instances')
        user_xforms = xforms
        # forms shared with user
        xfct = ContentType.objects.get(app_label='logger', model='xform')
        xfs = content_user.userobjectpermission_set.filter(content_type=xfct)
        shared_forms_pks = list(set([xf.object_pk for xf in xfs]))
        forms_shared_with = XForm.objects.filter(
            pk__in=shared_forms_pks).exclude(user=content_user)\
            .select_related('user')
        # all forms to which the user has access
        published_or_shared = XForm.objects.filter(
            pk__in=shared_forms_pks).select_related('user')
        xforms_list = [
            {
                'id': 'published',
                'xforms': user_xforms,
                'title': _("Published Forms"),
                'small': _("Export, map, and view submissions.")
            },
            {
                'id': 'shared',
                'xforms': forms_shared_with,
                'title': _("Shared Forms"),
                'small': _("List of forms shared with you.")
            },
            {
                'id': 'published_or_shared',
                'xforms': published_or_shared,
                'title': _("Published Forms"),
                'small': _("Export, map, and view submissions.")
            }
        ]
        data.update({
            'all_forms': all_forms,
            'show_dashboard': show_dashboard,
            'form': form,
            'form_url': form_url,
            'url': url,
            'user_xforms': user_xforms,
            'xforms_list': xforms_list,
            'forms_shared_with': forms_shared_with
        })
    # for any other user -> profile
    set_profile_data(data, content_user)

    return render(request, "profile.html", data)


def members_list(request):
    if not request.user.is_staff and not request.user.is_superuser:
        return HttpResponseForbidden(_('Forbidden.'))
    users = User.objects.all()
    template = 'people.html'

    return render(request, template, {'template': template, 'users': users})


@login_required
def profile_settings(request, username):
    content_user = check_and_set_user(request, username)
    profile, created = UserProfile.objects.get_or_create(user=content_user)
    if request.method == 'POST':
        form = UserProfileForm(request.POST, instance=profile)
        if form.is_valid():
            # get user
            # user.email = cleaned_email
            '''
            The email field has been removed from the profile form.
            form.instance.user.email = form.cleaned_data['email']
            form.instance.user.save()
            '''
            form.save()
            # todo: add string rep. of settings to see what changed
            audit = {}
            audit_log(
                Actions.PROFILE_SETTINGS_UPDATED, request.user, content_user,
                _("Profile settings updated."), audit, request)
            return HttpResponseRedirect(reverse(
                public_profile, kwargs={'username': request.user.username}
            ))
    else:
        form = UserProfileForm(
            instance=profile, initial={"email": content_user.email})

    return render(request, "settings.html",
                  {'content_user': content_user, 'form': form})


@require_GET
def public_profile(request, username):
    content_user = check_and_set_user(request, username)
    if isinstance(content_user, HttpResponseRedirect):
        return content_user
    data = {}
    set_profile_data(data, content_user)
    data['is_owner'] = request.user == content_user
    audit = {}
    audit_log(
        Actions.PUBLIC_PROFILE_ACCESSED, request.user, content_user,
        _("Public profile accessed."), audit, request)

    return render(request, "profile.html", data)


@login_required
def dashboard(request):
    content_user = request.user
    data = {
        'form': QuickConverter(),
        'content_user': content_user,
        'url': request.build_absolute_uri("/%s" % request.user.username)
    }
    set_profile_data(data, content_user)

    return render(request, "dashboard.html", data)


def redirect_to_public_link(request, uuid):
    xform = get_object_or_404(XForm, uuid=uuid)
    request.session['public_link'] = \
        xform.uuid if MetaData.public_link(xform) else False

    return HttpResponseRedirect(reverse(show, kwargs={
        'username': xform.user.username,
        'id_string': xform.id_string
    }))


def set_xform_owner_data(data, xform, request, username, id_string):
    data['sms_support_form'] = ActivateSMSSupportFom(
        initial={'enable_sms_support': xform.allows_sms,
                 'sms_id_string': xform.sms_id_string})
    if not xform.allows_sms:
        data['sms_compatible'] = check_form_sms_compatibility(
            None, json_survey=json.loads(xform.json))
    else:
        url_root = request.build_absolute_uri('/')[:-1]
        data['sms_providers_doc'] = providers_doc(
            url_root=url_root,
            username=username,
            id_string=id_string)
        data['url_root'] = url_root

    data['form_license_form'] = FormLicenseForm(
        initial={'value': data['form_license']})
    data['data_license_form'] = DataLicenseForm(
        initial={'value': data['data_license']})
    data['doc_form'] = SupportDocForm()
    data['source_form'] = SourceForm()
    data['media_form'] = MediaForm()
    users_with_perms = []

    for perm in get_users_with_perms(xform, attach_perms=True).items():
        has_perm = []
        if CAN_CHANGE_XFORM in perm[1]:
            has_perm.append(_("Can Edit"))
        if CAN_VIEW_XFORM in perm[1]:
            has_perm.append(_("Can View"))
        if CAN_ADD_SUBMISSIONS in perm[1]:
            has_perm.append(_("Can submit to"))
        if CAN_VALIDATE_XFORM in perm[1]:
            has_perm.append(_(u"Can Validate"))
        if CAN_DELETE_DATA_XFORM in perm[1]:
            has_perm.append(_(u"Can Delete Data"))
        users_with_perms.append((perm[0], " | ".join(has_perm)))

    data['users_with_perms'] = users_with_perms
    data['permission_form'] = PermissionForm(username)


@require_GET
def show(request, username=None, id_string=None, uuid=None):
    if uuid:
        return redirect_to_public_link(request, uuid)

    xform, is_owner, can_edit, can_view, can_delete_data = get_xform_and_perms(
        username, id_string, request)
    # no access
    if not (xform.shared or can_view or request.session.get('public_link')):
        return HttpResponseRedirect(reverse(home))

    data = {}
    data['cloned'] = len(
        XForm.objects.filter(user__username__iexact=request.user.username,
                             id_string__exact=id_string + XForm.CLONED_SUFFIX)
    ) > 0
    data['public_link'] = MetaData.public_link(xform)
    data['is_owner'] = is_owner
    data['can_edit'] = can_edit
    data['can_view'] = can_view or request.session.get('public_link')
    data['can_delete_data'] = can_delete_data
    data['xform'] = xform
    data['content_user'] = xform.user
    data['base_url'] = "https://%s" % request.get_host()
    data['source'] = MetaData.source(xform)
    data['form_license'] = MetaData.form_license(xform).data_value
    data['data_license'] = MetaData.data_license(xform).data_value
    data['supporting_docs'] = MetaData.supporting_docs(xform)
    data['media_upload'] = MetaData.media_upload(xform)

    if is_owner:
        set_xform_owner_data(data, xform, request, username, id_string)

    if xform.allows_sms:
        data['sms_support_doc'] = get_autodoc_for(xform)

    return render(request, "show.html", data)


# SETTINGS SCREEN FOR KPI, LOADED IN IFRAME 
@require_GET
def show_form_settings(request, username=None, id_string=None, uuid=None):
    if uuid:
        return redirect_to_public_link(request, uuid)

    xform, is_owner, can_edit, can_view, can_delete_data = get_xform_and_perms(
        username, id_string, request)
    # no access
    if not (xform.shared or can_view or request.session.get('public_link')):
        return HttpResponseRedirect(reverse(home))

    data = {}
    data['cloned'] = len(
        XForm.objects.filter(user__username__iexact=request.user.username,
                             id_string__exact=id_string + XForm.CLONED_SUFFIX)
    ) > 0
    data['public_link'] = MetaData.public_link(xform)
    data['is_owner'] = is_owner
    data['can_edit'] = can_edit
    data['can_view'] = can_view or request.session.get('public_link')
    data['can_delete_data'] = can_delete_data
    data['xform'] = xform
    data['content_user'] = xform.user
    data['base_url'] = "https://%s" % request.get_host()
    data['source'] = MetaData.source(xform)
    data['form_license'] = MetaData.form_license(xform).data_value
    data['data_license'] = MetaData.data_license(xform).data_value
    data['supporting_docs'] = MetaData.supporting_docs(xform)
    data['media_upload'] = MetaData.media_upload(xform)
    # https://html.spec.whatwg.org/multipage/input.html#attr-input-accept
    # e.g. .csv,.xml,text/csv,text/xml
    media_upload_types = []
    for supported_type in settings.SUPPORTED_MEDIA_UPLOAD_TYPES:
        extension = '.{}'.format(supported_type.split('/')[-1])
        media_upload_types.append(extension)
        media_upload_types.append(supported_type)
    data['media_upload_types'] = ','.join(media_upload_types)

    if is_owner:
        set_xform_owner_data(data, xform, request, username, id_string)

    if xform.allows_sms:
        data['sms_support_doc'] = get_autodoc_for(xform)

    return render(request, "show_form_settings.html", data)


@login_required
@require_GET
def api_token(request, username=None):
    if request.user.username == username:
        user = get_object_or_404(User, username=username)
        data = {}
        data['token_key'], created = Token.objects.get_or_create(user=user)

        return render(request, "api_token.html", data)

    return HttpResponseForbidden(_('Permission denied.'))


@require_http_methods(["GET", "OPTIONS"])
def api(request, username=None, id_string=None):
    """
    Returns all results as JSON.  If a parameter string is passed,
    it takes the 'query' parameter, converts this string to a dictionary, an
    that is then used as a MongoDB query string.

    NOTE: only a specific set of operators are allow, currently $or and $and.
    Please send a request if you'd like another operator to be enabled.

    NOTE: Your query must be valid JSON, double check it here,
    http://json.parser.online.fr/

    E.g. api?query='{"last_name": "Smith"}'
    """
    if request.method == "OPTIONS":
        response = HttpResponse()
        add_cors_headers(response)

        return response
    helper_auth_helper(request)
    helper_auth_helper(request)
    xform, owner = check_and_set_user_and_form(username, id_string, request)

    if not xform:
        return HttpResponseForbidden(_('Not shared.'))

    try:
        args = {
            'username': username,
            'id_string': id_string,
            'query': request.GET.get('query'),
            'fields': request.GET.get('fields'),
            'sort': request.GET.get('sort')
        }
        if 'start' in request.GET:
            args["start"] = int(request.GET.get('start'))
        if 'limit' in request.GET:
            args["limit"] = int(request.GET.get('limit'))
        if 'count' in request.GET:
            args["count"] = True if int(request.GET.get('count')) > 0\
                else False
        cursor = ParsedInstance.query_mongo(**args)
    except ValueError as e:
        return HttpResponseBadRequest(e.__str__())

    records = list(record for record in cursor)
    response_text = json_util.dumps(records)

    if 'callback' in request.GET and request.GET.get('callback') != '':
        callback = request.GET.get('callback')
        response_text = ("%s(%s)" % (callback, response_text))

    response = HttpResponse(response_text, content_type='application/json')
    add_cors_headers(response)

    return response


@require_GET
def public_api(request, username, id_string):
    """
    Returns public information about the form as JSON
    """

    xform = get_object_or_404(XForm,
                              user__username__iexact=username,
                              id_string__exact=id_string)

    _DATETIME_FORMAT = '%Y-%m-%d %H:%M:%S'
    exports = {'username': xform.user.username,
               'id_string': xform.id_string,
               'shared': xform.shared,
               'shared_data': xform.shared_data,
               'downloadable': xform.downloadable,
               'title': xform.title,
               'date_created': xform.date_created.strftime(_DATETIME_FORMAT),
               'date_modified': xform.date_modified.strftime(_DATETIME_FORMAT),
               'uuid': xform.uuid,
               }
    response_text = json.dumps(exports)

    return HttpResponse(response_text, content_type='application/json')


@login_required
def edit(request, username, id_string):
    xform = XForm.objects.get(user__username__iexact=username,
                              id_string__exact=id_string)
    owner = xform.user

    if username == request.user.username or\
            request.user.has_perm('logger.change_xform', xform):
        if request.POST.get('description'):
            audit = {
                'xform': xform.id_string
            }
            audit_log(
                Actions.FORM_UPDATED, request.user, owner,
                _("Description for '%(id_string)s' updated from "
                    "'%(old_description)s' to '%(new_description)s'.") %
                {
                    'id_string': xform.id_string,
                    'old_description': xform.description,
                    'new_description': request.POST['description']
                }, audit, request)
            xform.description = request.POST['description']
        elif request.POST.get('title'):
            audit = {
                'xform': xform.id_string
            }
            audit_log(
                Actions.FORM_UPDATED, request.user, owner,
                _("Title for '%(id_string)s' updated from "
                    "'%(old_title)s' to '%(new_title)s'.") %
                {
                    'id_string': xform.id_string,
                    'old_title': xform.title,
                    'new_title': request.POST.get('title')
                }, audit, request)
            xform.title = request.POST['title']
        elif request.POST.get('toggle_shared'):
            if request.POST['toggle_shared'] == 'data':
                audit = {
                    'xform': xform.id_string
                }
                audit_log(
                    Actions.FORM_UPDATED, request.user, owner,
                    _("Data sharing updated for '%(id_string)s' from "
                        "'%(old_shared)s' to '%(new_shared)s'.") %
                    {
                        'id_string': xform.id_string,
                        'old_shared': _("shared")
                        if xform.shared_data else _("not shared"),
                        'new_shared': _("shared")
                        if not xform.shared_data else _("not shared")
                    }, audit, request)
                xform.shared_data = not xform.shared_data
            elif request.POST['toggle_shared'] == 'form':
                audit = {
                    'xform': xform.id_string
                }
                audit_log(
                    Actions.FORM_UPDATED, request.user, owner,
                    _("Form sharing for '%(id_string)s' updated "
                        "from '%(old_shared)s' to '%(new_shared)s'.") %
                    {
                        'id_string': xform.id_string,
                        'old_shared': _("shared")
                        if xform.shared else _("not shared"),
                        'new_shared': _("shared")
                        if not xform.shared else _("not shared")
                    }, audit, request)
                xform.shared = not xform.shared
            elif request.POST['toggle_shared'] == 'active':
                audit = {
                    'xform': xform.id_string
                }
                audit_log(
                    Actions.FORM_UPDATED, request.user, owner,
                    _("Active status for '%(id_string)s' updated from "
                        "'%(old_shared)s' to '%(new_shared)s'.") %
                    {
                        'id_string': xform.id_string,
                        'old_shared': _("shared")
                        if xform.downloadable else _("not shared"),
                        'new_shared': _("shared")
                        if not xform.downloadable else _("not shared")
                    }, audit, request)
                xform.downloadable = not xform.downloadable
        elif request.POST.get('form-license'):
            audit = {
                'xform': xform.id_string
            }
            audit_log(
                Actions.FORM_UPDATED, request.user, owner,
                _("Form License for '%(id_string)s' updated to "
                    "'%(form_license)s'.") %
                {
                    'id_string': xform.id_string,
                    'form_license': request.POST['form-license'],
                }, audit, request)
            MetaData.form_license(xform, request.POST['form-license'])
        elif request.POST.get('data-license'):
            audit = {
                'xform': xform.id_string
            }
            audit_log(
                Actions.FORM_UPDATED, request.user, owner,
                _("Data license for '%(id_string)s' updated to "
                    "'%(data_license)s'.") %
                {
                    'id_string': xform.id_string,
                    'data_license': request.POST['data-license'],
                }, audit, request)
            MetaData.data_license(xform, request.POST['data-license'])
        elif request.POST.get('source') or request.FILES.get('source'):
            audit = {
                'xform': xform.id_string
            }
            audit_log(
                Actions.FORM_UPDATED, request.user, owner,
                _("Source for '%(id_string)s' updated to '%(source)s'.") %
                {
                    'id_string': xform.id_string,
                    'source': request.POST.get('source'),
                }, audit, request)
            MetaData.source(xform, request.POST.get('source'),
                            request.FILES.get('source'))
        elif request.POST.get('enable_sms_support_trigger') is not None:
            sms_support_form = ActivateSMSSupportFom(request.POST)
            if sms_support_form.is_valid():
                audit = {
                    'xform': xform.id_string
                }
                enabled = \
                    sms_support_form.cleaned_data.get('enable_sms_support')
                if enabled:
                    audit_action = Actions.SMS_SUPPORT_ACTIVATED
                    audit_message = _("SMS Support Activated on")
                else:
                    audit_action = Actions.SMS_SUPPORT_DEACTIVATED
                    audit_message = _("SMS Support Deactivated on")
                audit_log(
                    audit_action, request.user, owner,
                    audit_message
                    % {'id_string': xform.id_string}, audit, request)
                # stored previous states to be able to rollback form status
                # in case we can't save.
                pe = xform.allows_sms
                pid = xform.sms_id_string
                xform.allows_sms = enabled
                xform.sms_id_string = \
                    sms_support_form.cleaned_data.get('sms_id_string')
                compat = check_form_sms_compatibility(None,
                                                      json.loads(xform.json))
                if compat['type'] == 'alert-error':
                    xform.allows_sms = False
                    xform.sms_id_string = pid
                try:
                    xform.save()
                except IntegrityError:
                    # unfortunately, there's no feedback mechanism here
                    xform.allows_sms = pe
                    xform.sms_id_string = pid

        elif request.POST.get('media_url'):
            uri = request.POST.get('media_url')
            try:
                SSRFProtect.validate(uri)
            except SSRFProtectException:
                return HttpResponseForbidden(_('URL {uri} is forbidden.').format(
                    uri=uri))
            MetaData.media_add_uri(xform, uri)
        elif request.FILES.get('media'):
            audit = {
                'xform': xform.id_string
            }
            audit_log(
                Actions.FORM_UPDATED, request.user, owner,
                _("Media added to '%(id_string)s'.") %
                {
                    'id_string': xform.id_string
                }, audit, request)
            for aFile in request.FILES.getlist("media"):
                MetaData.media_upload(xform, aFile)
        elif request.FILES.get('doc'):
            audit = {
                'xform': xform.id_string
            }
            audit_log(
                Actions.FORM_UPDATED, request.user, owner,
                _("Supporting document added to '%(id_string)s'.") %
                {
                    'id_string': xform.id_string
                }, audit, request)
            MetaData.supporting_docs(xform, request.FILES.get('doc'))

        xform.update()

        if request.is_ajax():
            return HttpResponse(_('Updated succeeded.'))
        else:
            if 'HTTP_REFERER' in request.META and request.META['HTTP_REFERER'].strip(): 
                return HttpResponseRedirect(request.META['HTTP_REFERER'])               

            return HttpResponseRedirect(reverse(show, kwargs={
                'username': username,
                'id_string': id_string
            }))

    return HttpResponseForbidden(_('Update failed.'))


def getting_started(request):
    template = 'getting_started.html'

    return render(request, 'base.html', {'template': template})


def support(request):
    template = 'support.html'

    return render(request, 'base.html', {'template': template})


def faq(request):
    template = 'faq.html'

    return render(request, 'base.html', {'template': template})


def xls2xform(request):
    template = 'xls2xform.html'

    return render(request, 'base.html', {'template': template})


def tutorial(request):
    template = 'tutorial.html'
    username = request.user.username if request.user.username else \
        'your-user-name'
    url = request.build_absolute_uri("/%s" % username)

    return render(request, 'base.html', {'template': template, 'url': url})


def resources(request):
    if 'fr' in request.LANGUAGE_CODE.lower():
        deck_id = 'a351f6b0a3730130c98b12e3c5740641'
    else:
        deck_id = '1a33a070416b01307b8022000a1de118'

    return render(request, 'resources.html', {'deck_id': deck_id})


def about_us(request):
    a_flatpage = '/about-us/'
    username = request.user.username if request.user.username else \
        'your-user-name'
    url = request.build_absolute_uri("/%s" % username)

    return render(request, 'base.html', {'a_flatpage': a_flatpage, 'url': url})


def privacy(request):
    template = 'privacy.html'

    return render(request, 'base.html', {'template': template})


def tos(request):
    template = 'tos.html'

    return render(request, 'base.html', {'template': template})


def syntax(request):
    template = 'syntax.html'

    return render(request, 'base.html', {'template': template})


def form_gallery(request):
    """
    Return a list of urls for all the shared xls files. This could be
    made a lot prettier.
    """
    data = {}
    if request.user.is_authenticated():
        data['loggedin_user'] = request.user
    data['shared_forms'] = XForm.objects.filter(shared=True)
    # build list of shared forms with cloned suffix
    id_strings_with_cloned_suffix = [
        x.id_string + XForm.CLONED_SUFFIX for x in data['shared_forms']
    ]
    # build list of id_strings for forms this user has cloned
    data['cloned'] = [
        x.id_string.split(XForm.CLONED_SUFFIX)[0]
        for x in XForm.objects.filter(
            user__username__iexact=request.user.username,
            id_string__in=id_strings_with_cloned_suffix
        )
    ]

    return render(request, 'form_gallery.html', data)


def download_metadata(request, username, id_string, data_id):
    xform = get_object_or_404(XForm,
                              user__username__iexact=username,
                              id_string__exact=id_string)
    owner = xform.user
    if username == request.user.username or xform.shared:
        data = get_object_or_404(MetaData, pk=data_id)
        file_path = data.data_file.name
        filename, extension = os.path.splitext(file_path.split('/')[-1])
        extension = extension.strip('.')
        dfs = get_storage_class()()
        if dfs.exists(file_path):
            audit = {
                'xform': xform.id_string
            }
            audit_log(
                Actions.FORM_UPDATED, request.user, owner,
                _("Document '%(filename)s' for '%(id_string)s' downloaded.") %
                {
                    'id_string': xform.id_string,
                    'filename': "%s.%s" % (filename, extension)
                }, audit, request)
            response = response_with_mimetype_and_name(
                data.data_file_type,
                filename, extension=extension, show_date=False,
                file_path=file_path)
            return response
        else:
            return HttpResponseNotFound()

    return HttpResponseForbidden(_('Permission denied.'))


@login_required()
def delete_metadata(request, username, id_string, data_id):
    xform = get_object_or_404(XForm,
                              user__username__iexact=username,
                              id_string__exact=id_string)
    owner = xform.user
    data = get_object_or_404(MetaData, pk=data_id)
    dfs = get_storage_class()()
    req_username = request.user.username
    if request.GET.get('del', False) and username == req_username:
        try:
            dfs.delete(data.data_file.name)
            data.delete()
            audit = {
                'xform': xform.id_string
            }
            audit_log(
                Actions.FORM_UPDATED, request.user, owner,
                _("Document '%(filename)s' deleted from '%(id_string)s'.") %
                {
                    'id_string': xform.id_string,
                    'filename': os.path.basename(data.data_file.name)
                }, audit, request)
            return HttpResponseRedirect(reverse(show, kwargs={
                'username': username,
                'id_string': id_string
            }))
        except Exception:
            return HttpResponseServerError()
    elif request.GET.get('map_name_del', False) and username == req_username:
        data.delete()
        audit = {
            'xform': xform.id_string
        }
        audit_log(
            Actions.FORM_UPDATED, request.user, owner,
            _("Map layer deleted from '%(id_string)s'.") %
            {
                'id_string': xform.id_string,
            }, audit, request)
        return HttpResponseRedirect(reverse(show, kwargs={
            'username': username,
            'id_string': id_string
        }))

    return HttpResponseForbidden(_('Permission denied.'))


def download_media_data(request, username, id_string, data_id):
    xform = get_object_or_404(
        XForm, user__username__iexact=username,
        id_string__exact=id_string)
    owner = xform.user
    data = get_object_or_404(MetaData, id=data_id)
    dfs = get_storage_class()()
    if request.GET.get('del', False):
        if username == request.user.username:
            try:
                # ensure filename is not an empty string
                if data.data_file.name != '':
                    dfs.delete(data.data_file.name)

                data.delete()
                audit = {
                    'xform': xform.id_string
                }
                audit_log(
                    Actions.FORM_UPDATED, request.user, owner,
                    _("Media download '%(filename)s' deleted from "
                        "'%(id_string)s'.") %
                    {
                        'id_string': xform.id_string,
                        'filename': os.path.basename(data.data_file.name)
                    }, audit, request)
                if 'HTTP_REFERER' in request.META and request.META['HTTP_REFERER'].strip(): 
                    return HttpResponseRedirect(request.META['HTTP_REFERER'])               

                return HttpResponseRedirect(reverse(show, kwargs={
                    'username': username,
                    'id_string': id_string
                }))
            except Exception as e:
                return HttpResponseServerError(e)
    else:
        if username:  # == request.user.username or xform.shared:
            if data.data_file.name == '' and data.data_value is not None:
                return HttpResponseRedirect(data.data_value)

            file_path = data.data_file.name
            filename, extension = os.path.splitext(file_path.split('/')[-1])
            extension = extension.strip('.')
            if dfs.exists(file_path):
                audit = {
                    'xform': xform.id_string
                }
                audit_log(
                    Actions.FORM_UPDATED, request.user, owner,
                    _("Media '%(filename)s' downloaded from "
                        "'%(id_string)s'.") %
                    {
                        'id_string': xform.id_string,
                        'filename': os.path.basename(file_path)
                    }, audit, request)
                response = response_with_mimetype_and_name(
                    data.data_file_type,
                    filename, extension=extension, show_date=False,
                    file_path=file_path)
                return response
            else:
                return HttpResponseNotFound()

    return HttpResponseForbidden(_('Permission denied.'))


def form_photos(request, username, id_string):
    GALLERY_IMAGE_COUNT_LIMIT = 2500
    GALLERY_THUMBNAIL_CHUNK_SIZE = 25
    GALLERY_THUMBNAIL_CHUNK_DELAY = 5000 # ms

    xform, owner = check_and_set_user_and_form(username, id_string, request)

    if not xform:
        return HttpResponseForbidden(_('Not shared.'))

    data = {}
    data['form_view'] = True
    data['content_user'] = owner
    data['xform'] = xform
    image_urls = []
    too_many_images = False

    # Show the most recent images first
    for instance in xform.instances.all().order_by('-pk'):
        attachments = instance.attachments.all()
        # If we have to truncate, don't include a partial instance
        if len(image_urls) + attachments.count() > GALLERY_IMAGE_COUNT_LIMIT:
            too_many_images = True
            break
        for attachment in attachments:
            # skip if not image e.g video or file
            if not attachment.mimetype.startswith('image'):
                continue

            data = {}
            data['original'] = attachment.secure_url()
            for suffix in settings.THUMB_CONF.keys():
                data[suffix] = attachment.secure_url(suffix)

            image_urls.append(data)

    data['images'] = image_urls
    data['too_many_images'] = too_many_images
    data['thumbnail_chunk_size'] = GALLERY_THUMBNAIL_CHUNK_SIZE
    data['thumbnail_chunk_delay'] = GALLERY_THUMBNAIL_CHUNK_DELAY
    data['profilei'], created = UserProfile.objects.get_or_create(user=owner)

    return render(request, 'form_photos.html', data)


@require_POST
def set_perm(request, username, id_string):
    xform = get_object_or_404(XForm,
                              user__username__iexact=username,
                              id_string__exact=id_string)
    owner = xform.user
    if username != request.user.username\
            and not has_permission(xform, username, request):
        return HttpResponseForbidden(_('Permission denied.'))

    try:
        perm_type = request.POST['perm_type']
        for_user = request.POST['for_user']
    except KeyError:
        return HttpResponseBadRequest()

    perms_by_type = {
        'edit': CAN_CHANGE_XFORM,
        'view': CAN_VIEW_XFORM,
        'report': CAN_ADD_SUBMISSIONS,
        'validate': CAN_VALIDATE_XFORM,
        'delete_data': CAN_DELETE_DATA_XFORM
    }

    if perm_type in perms_by_type.keys() or perm_type == 'remove':
        try:
            user = User.objects.get(username=for_user)
        except User.DoesNotExist:
            messages.add_message(
                request, messages.INFO,
                _("Wrong username <b>%s</b>." % for_user),
                extra_tags='alert-error')
        else:
            if perm_type == 'remove':
                audit = {
                    'xform': xform.id_string
                }
                audit_message = _("All permissions on '{id_string}' "
                                  "removed from '{for_user}'.")
                audit_log(
                    Actions.FORM_PERMISSIONS_UPDATED,
                    request.user,
                    owner,
                    audit_message.format(
                        id_string=xform.id_string,
                        for_user=for_user
                    ),
                    audit,
                    request
                )
                remove_perm(CAN_CHANGE_XFORM, user, xform)
                remove_perm(CAN_VIEW_XFORM, user, xform)
                remove_perm(CAN_ADD_SUBMISSIONS, user, xform)
                remove_perm(CAN_VALIDATE_XFORM, user, xform)
                remove_perm(CAN_DELETE_DATA_XFORM, user, xform)

            elif not user.has_perm(perms_by_type[perm_type], xform):
                audit = {
                    'xform': xform.id_string
                }
                audit_message = _("'{codename}' permission on '{id_string}' "
                                  "assigned to '{for_user}'.")
                audit_log(
                    Actions.FORM_PERMISSIONS_UPDATED,
                    request.user,
                    owner,
                    audit_message.format(
                        codename=perms_by_type[perm_type],
                        id_string=xform.id_string,
                        for_user=for_user
                    ),
                    audit,
                    request
                )

                assign_perm(perms_by_type[perm_type], user, xform)

    elif perm_type == 'link':
        current = MetaData.public_link(xform)
        if for_user == 'all':
            MetaData.public_link(xform, True)
        elif for_user == 'none':
            MetaData.public_link(xform, False)
        elif for_user == 'toggle':
            MetaData.public_link(xform, not current)
        audit = {
            'xform': xform.id_string
        }
        audit_log(
            Actions.FORM_PERMISSIONS_UPDATED, request.user, owner,
            _("Public link on '%(id_string)s' %(action)s.") %
            {
                'id_string': xform.id_string,
                'action': "created"
                if for_user == "all" or
                (for_user == "toggle" and not current) else "removed"
            }, audit, request)

    if request.is_ajax():
        return HttpResponse(
            json.dumps(
                {'status': 'success'}), content_type='application/json')
    if 'HTTP_REFERER' in request.META and request.META['HTTP_REFERER'].strip(): 
        return HttpResponseRedirect(request.META['HTTP_REFERER'])               

    return HttpResponseRedirect(reverse(show, kwargs={
        'username': username,
        'id_string': id_string
    }))


@require_POST
@login_required
def delete_data(request, username=None, id_string=None):
    xform, owner = check_and_set_user_and_form(username, id_string, request)
    response_text = ''
    if not xform or not has_delete_data_permission(xform, owner, request):
        return HttpResponseForbidden(_('Not shared.'))

    data_id = request.POST.get('id')
    if not data_id:
        return HttpResponseBadRequest(_("id must be specified"))

    Instance.set_deleted_at(data_id)
    audit = {
        'xform': xform.id_string
    }
    audit_log(
        Actions.SUBMISSION_DELETED, request.user, owner,
        _("Deleted submission with id '%(record_id)s' "
            "on '%(id_string)s'.") %
        {
            'id_string': xform.id_string,
            'record_id': data_id
        }, audit, request)
    response_text = json.dumps({"success": "Deleted data %s" % data_id})
    if 'callback' in request.GET and request.GET.get('callback') != '':
        callback = request.GET.get('callback')
        response_text = ("%s(%s)" % (callback, response_text))

    return HttpResponse(response_text, content_type='application/json')


@require_POST
@is_owner
def update_xform(request, username, id_string):
    xform = get_object_or_404(
        XForm, user__username__iexact=username, id_string__exact=id_string)
    owner = xform.user

    def set_form():
        form = QuickConverter(request.POST, request.FILES)
        survey = form.publish(request.user, id_string).survey
        enketo_webform_url = reverse(
            enter_data,
            kwargs={'username': username, 'id_string': survey.id_string}
        )
        audit = {
            'xform': xform.id_string
        }
        audit_log(
            Actions.FORM_XLS_UPDATED, request.user, owner,
            _("XLS for '%(id_string)s' updated.") %
            {
                'id_string': xform.id_string,
            }, audit, request)
        return {
            'type': 'alert-success',
            'text': _('Successfully published %(form_id)s.'
                      ' <a href="%(form_url)s">Enter Web Form</a>'
                      ' or <a href="#preview-modal" data-toggle="modal">'
                      'Preview Web Form</a>')
                    % {'form_id': survey.id_string,
                       'form_url': enketo_webform_url}
        }
    message = publish_form(set_form)
    messages.add_message(
        request, messages.INFO, message['text'], extra_tags=message['type'])

    return HttpResponseRedirect(reverse(show, kwargs={
        'username': username,
        'id_string': id_string
    }))


@is_owner
def activity(request, username):
    owner = get_object_or_404(User, username=username)

    return render(request, 'activity.html', {'user': owner})


def activity_fields(request):
    fields = [
        {
            'id': 'created_on',
            'label': _('Performed On'),
            'type': 'datetime',
            'searchable': False
        },
        {
            'id': 'action',
            'label': _('Action'),
            'type': 'string',
            'searchable': True,
            'options': sorted([Actions[e] for e in Actions.enums])
        },
        {
            'id': 'user',
            'label': 'Performed By',
            'type': 'string',
            'searchable': True
        },
        {
            'id': 'msg',
            'label': 'Description',
            'type': 'string',
            'searchable': True
        },
    ]
    response_text = json.dumps(fields)

    return HttpResponse(response_text, content_type='application/json')


@is_owner
def activity_api(request, username):
    from bson.objectid import ObjectId

    def stringify_unknowns(obj):
        if isinstance(obj, ObjectId):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.strftime(DATETIME_FORMAT)
        return None
    try:
        query_args = {
            'username': username,
            'query': json.loads(request.GET.get('query'))
            if request.GET.get('query') else {},
            'fields': json.loads(request.GET.get('fields'))
            if request.GET.get('fields') else [],
            'sort': json.loads(request.GET.get('sort'))
            if request.GET.get('sort') else {}
        }
        if 'start' in request.GET:
            query_args["start"] = int(request.GET.get('start'))
        if 'limit' in request.GET:
            query_args["limit"] = int(request.GET.get('limit'))
        if 'count' in request.GET:
            query_args["count"] = True \
                if int(request.GET.get('count')) > 0 else False
        cursor = AuditLog.query_mongo(**query_args)
    except ValueError as e:
        return HttpResponseBadRequest(e.__str__())

    records = list(record for record in cursor)
    response_text = json.dumps(records, default=stringify_unknowns)
    if 'callback' in request.GET and request.GET.get('callback') != '':
        callback = request.GET.get('callback')
        response_text = ("%s(%s)" % (callback, response_text))

    return HttpResponse(response_text, content_type='application/json')


def qrcode(request, username, id_string):
    try:
        formhub_url = "http://%s/" % request.get_host()
    except:
        formhub_url = "http://formhub.org/"
    formhub_url = formhub_url + username

    if settings.TESTING_MODE:
        formhub_url = "https://{}/{}".format(settings.TEST_HTTP_HOST,
                                             settings.TEST_USERNAME)

    results = _("Unexpected Error occured: No QRCODE generated")
    status = 200
    try:
        url = enketo_url(formhub_url, id_string)
    except Exception as e:
        error_msg = _("Error Generating QRCODE: %s" % e)
        results = """<div class="alert alert-error">%s</div>""" % error_msg
        status = 400
    else:
        if url:
            image = generate_qrcode(url)
            results = """<img class="qrcode" src="%s" alt="%s" />
                    </br><a href="%s" target="_blank">%s</a>""" \
                % (image, url, url, url)
        else:
            status = 400

    return HttpResponse(results, content_type='text/html', status=status)


def enketo_preview(request, username, id_string):
    xform = get_object_or_404(
        XForm, user__username__iexact=username, id_string__exact=id_string)
    owner = xform.user
    if not has_permission(xform, owner, request, xform.shared):
        return HttpResponseForbidden(_('Not shared.'))
    enekto_preview_url = \
        "%(enketo_url)s?server=%(profile_url)s&id=%(id_string)s" % {
            'enketo_url': settings.ENKETO_PREVIEW_URL,
            'profile_url': request.build_absolute_uri(
                reverse(profile, kwargs={'username': owner.username})),
            'id_string': xform.id_string
        }
    return HttpResponseRedirect(enekto_preview_url)


@require_GET
@login_required
def username_list(request):
    data = []
    query = request.GET.get('query', None)
    if query:
        users = User.objects.values('username')\
            .filter(username__startswith=query, is_active=True, pk__gte=0)
        data = [user['username'] for user in users]

    return HttpResponse(json.dumps(data), content_type='application/json')

def _make_authenticated_request(request, user):
    """
    Make an authenticated request to KPI using the current session.
    Returns response from KPI.
    """
    return requests.get(
        url=_get_migrate_url(user.username),
        cookies={settings.SESSION_COOKIE_NAME: request.session.session_key}
    )


def _get_migrate_url(username):
    return '{kf_url}/api/v2/users/{username}/migrate/'.format(
        kf_url=settings.KOBOFORM_URL, username=username
    )
