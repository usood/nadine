import os
import traceback
import logging
import uuid
from datetime import date, datetime, time, timedelta

from django.conf import settings
from django.template import RequestContext
from django.contrib.auth.models import User
from django.core.urlresolvers import reverse, resolve
from django.contrib.auth.decorators import login_required
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.sites.models import Site
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import render_to_response, get_object_or_404
from django.template.loader import render_to_string
from django.utils import timezone

from nadine import mailgun
from nadine.models.core import Member, DailyLog, FileUpload
from nadine.models.payment import Bill
from members.models import MOTD
from staff.forms import NewUserForm, MemberSearchForm
from arpwatch import arp
from staff import email
from forms import SignatureForm

from easy_pdf.rendering import render_to_pdf, render_to_pdf_response

logger = logging.getLogger(__name__)


def members(request):
    members = None
    list_members = request.GET.has_key("startswith")
    if list_members:
        sw = request.GET.get('startswith')
        members = Member.objects.active_members().filter(user__first_name__startswith=sw).order_by('user__first_name')
    return render_to_response('tablet/members.html', {'members': members, 'list_members': list_members}, context_instance=RequestContext(request))


def here_today(request):
    members = arp.users_for_day()
    return render_to_response('tablet/here_today.html', {'members': members}, context_instance=RequestContext(request))


def visitors(request):
    page_message = None
    if request.method == "POST":
        form = NewUserForm(request.POST)
        try:
            if form.is_valid():
                user = form.save()
                return HttpResponseRedirect(reverse('tablet.views.post_create', kwargs={'username': user.username}))
        except Exception as e:
            page_message = str(e)[3:len(str(e)) - 2]
            logger.error(str(e))
            #page_message = str(e)
    else:
        form = NewUserForm()
    return render_to_response('tablet/visitors.html', {'new_user_form': form, 'page_message': page_message}, context_instance=RequestContext(request))


def search(request):
    search_results = None
    if request.method == "POST":
        member_search_form = MemberSearchForm(request.POST)
        if member_search_form.is_valid():
            search_results = Member.objects.search(member_search_form.cleaned_data['terms'])
    else:
        member_search_form = MemberSearchForm()
    return render_to_response('tablet/search.html', {'member_search_form': member_search_form, 'search_results': search_results}, context_instance=RequestContext(request))


def user_profile(request, username):
    user = get_object_or_404(User, username=username)
    member = get_object_or_404(Member, user=user)
    membership = member.active_membership()
    tags = member.tags.order_by('name')
    return render_to_response('tablet/user_profile.html', {'user': user, 'member': member, 'membership': membership, 'tags': tags}, context_instance=RequestContext(request))


def user_signin(request, username):
    user = get_object_or_404(User, username=username)
    member = get_object_or_404(Member, user=user)
    membership = member.active_membership()

    can_signin = False
    active_membership = member.active_membership()
    if not active_membership or active_membership.end_date or not active_membership.has_desk:
        if not DailyLog.objects.filter(member=member, visit_date=timezone.localtime(timezone.now()).date()):
            can_signin = True

    search_results = None
    if request.method == "POST":
        member_search_form = MemberSearchForm(request.POST)
        if member_search_form.is_valid():
            search_results = Member.objects.search(member_search_form.cleaned_data['terms'], active_only=True)
    else:
        member_search_form = MemberSearchForm()

    # Look up previous hosts for his member
    guest_days = DailyLog.objects.filter(member__user__username=username, guest_of__isnull=False).values("guest_of")
    previous_hosts = Member.objects.active_members().filter(id__in=guest_days)

    return render_to_response('tablet/user_signin.html', {'user': user, 'member': member, 'can_signin': can_signin,
                                                          'membership': membership, 'member': member, 'previous_hosts':previous_hosts,
                                                          'member_search_form': member_search_form, 'search_results': search_results}, context_instance=RequestContext(request))


def post_create(request, username):
    user = get_object_or_404(User, username=username)
    if request.POST.has_key("work_today"):
        work_today = request.POST.get('work_today')
        if work_today == "Yes":
            # Send them over to the sign-in page.  This will trigger the Free Trial logic down the line.
            return HttpResponseRedirect(reverse('tablet.views.signin_user', kwargs={'username': user.username}))
        else:
            try:
                email.announce_new_user(user)
            except:
                logger.error("Could not send introduction email to %s" % user.email)
            return HttpResponseRedirect(reverse('tablet.views.members', kwargs={}))

    search_results = None
    if request.method == "POST":
        member_search_form = MemberSearchForm(request.POST)
        if member_search_form.is_valid():
            search_results = Member.objects.search(member_search_form.cleaned_data['terms'], active_only=True)

    return render_to_response('tablet/post_create.html', {'user': user, 'search_results': search_results}, context_instance=RequestContext(request))


def signin_user(request, username):
    return signin_user_guest(request, username, None)


def signin_user_guest(request, username, guestof):
    user = get_object_or_404(User, username=username)
    member = get_object_or_404(Member, user=user)
    daily_log = DailyLog()
    daily_log.member = member
    daily_log.visit_date = timezone.localtime(timezone.now()).date()
    # Only proceed if they haven't signed in already
    if DailyLog.objects.filter(member=member, visit_date=daily_log.visit_date).count() == 0:
        if guestof:
            guestof_user = get_object_or_404(User, username=guestof)
            guestof_member = get_object_or_404(Member, user=guestof_user)
            daily_log.guest_of = guestof_member
        if DailyLog.objects.filter(member=member).count() == 0:
            daily_log.payment = 'Trial'
        else:
            daily_log.payment = 'Bill'
        daily_log.save()

        if daily_log.payment == 'Trial':
            try:
                email.announce_free_trial(user)
                email.send_introduction(user)
                email.subscribe_to_newsletter(user)
            except:
                logger.error("Could not send introduction email to %s" % user.email)
        else:
            if len(member.open_alerts()) > 0:
                mailgun.send_manage_member(user)
    return HttpResponseRedirect(reverse('tablet.views.welcome', kwargs={'username': username}))


def welcome(request, username):
    usage_color = "black"
    user = get_object_or_404(User, username=username)
    member = user.get_profile()
    membership = None
    if member:
        membership = member.active_membership()
        if membership:
            days = len(member.activity_this_month())
            allowed = membership.get_allowance()
            if days > allowed:
                usage_color = "red"
            elif days == allowed:
                usage_color = "orange"
            else:
                usage_color = "green"
    motd = MOTD.objects.for_today()
    print motd
    return render_to_response('tablet/welcome.html', {'user': user, 'member': member, 'membership': membership,
                                                      'motd': motd, 'usage_color': usage_color}, context_instance=RequestContext(request))


def document_list(request, username):
    user = get_object_or_404(User, username=username)
    # Should be a more elegent way to remove the first element but this works too!
    doc_types = (FileUpload.DOC_TYPES[1], FileUpload.DOC_TYPES[2], FileUpload.DOC_TYPES[3])
    signed_docs = {}
    for doc in FileUpload.objects.filter(user=user):
        signed_docs[doc.document_type] = doc
    return render_to_response('tablet/document_list.html', {'user': user, 'signed_docs': signed_docs, 'document_types': doc_types}, context_instance=RequestContext(request))


def document_view(request, username, doc_type):
    user = get_object_or_404(User, username=username)
    file_upload = get_object_or_404(FileUpload, user=user, document_type=doc_type)
    # return render_to_response('tablet/documents_view.html', {'user':user, 'file_upload':file_upload}, context_instance=RequestContext(request))
    media_url = settings.MEDIA_URL + str(file_upload.file)
    return HttpResponseRedirect(media_url)


def signature_capture(request, username, doc_type):
    user = get_object_or_404(User, username=username)
    today = timezone.localtime(timezone.now()).date()
    form = SignatureForm(request.POST or None)
    if form and form.has_signature():
        signature_file = form.save_signature()
        render_url = reverse('tablet.views.signature_render', kwargs={'username': user.username, 'doc_type': doc_type, 'signature_file': signature_file}) + "?save_file=True"
        return HttpResponseRedirect(render_url)
    return render_to_response('tablet/signature_capture.html', {'user': user, 'form': form, 'today': today, 'doc_type': doc_type}, context_instance=RequestContext(request))


def signature_render(request, username, doc_type, signature_file):
    user = get_object_or_404(User, username=username)
    today = timezone.localtime(timezone.now()).date()
    pdf_args = {'name': user.get_full_name, 'date': today, 'doc_type': doc_type, 'signature_file': signature_file}
    if 'save_file' in request.GET:
        # Save the PDF as a file and redirect them back to the document list
        pdf_data = render_to_pdf('tablet/signature_render.html', pdf_args)
        pdf_file = FileUpload.objects.pdf_from_string(user, pdf_data, doc_type, user)
        os.remove(os.path.join(settings.MEDIA_ROOT, "signatures/%s" % signature_file))
        return HttpResponseRedirect(reverse('tablet.views.document_list', kwargs={'username': user.username}))
    return render_to_pdf_response(request, 'tablet/signature_render.html', pdf_args)

# Copyright 2011 Office Nomads LLC (http://www.officenomads.com/) Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance with the License. You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0 Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the specific language governing permissions and limitations under the License.
